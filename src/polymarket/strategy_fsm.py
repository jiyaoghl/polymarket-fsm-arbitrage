import json
import time
import threading
import asyncio
import logging
from typing import Dict, Any, Optional

import websockets

from polymarket.base_strategy import BaseStrategy
from polymarket.utils import record_latency
from polymarket.risk import RiskManager
from polymarket import risk_logger
from polymarket.fsm import TradeFSM, TradeState
from polymarket.logger import logger
from polymarket.kline_analyzer import is_asset_choppy
from polymarket.streamer import MarketDataStreamer

class ArbitrageBotFSM(BaseStrategy):
    """
    全量 FSM (Finite State Machine) 架构的套利机器人。
    继承自旧的 ArbitrageBot 以复用配置和 client，但彻底重写了核心的订单状态生命周期。
    所有 while 轮询将被事件驱动的回调 (Callbacks) 取代。
    """
    
    def __init__(self, strategy_config: Dict[str, Any]):
        super().__init__(strategy_config)
        
        # [风控] 引入全局风控单例
        from polymarket.risk_manager import RiskManager
        self.risk_manager = RiskManager()
        
        # market_id -> TradeFSM
        self.fsms: Dict[str, TradeFSM] = {}
        self._last_silent_filter_log: Dict[str, float] = {}
        # 负责触发超时的后台守护线程
        self._timer_thread = threading.Thread(target=self._fsm_timeout_daemon, daemon=True)
        self._timer_thread.start()
        
        self._recover_active_trades()

    def _recover_active_trades(self):
        try:
            from polymarket import db as _db
            from polymarket.config import DB_PATH
            caches = _db.get_all_trade_caches(self.strategy_id, DB_PATH)
            
            # 模拟盘环境下，历史崩溃的单腿毫无意义，直接清空 DB 缓存
            if not self.is_live:
                for cache in caches:
                    _db.delete_trade_cache(cache["market_id"], self.strategy_id, DB_PATH)
                if caches:
                    logger.info(f"[策略FSM：{self.strategy_id}] (模拟模式) 已清理 {len(caches)} 个无意义的历史未对冲缓存。")
                return

            for cache in caches:
                market_id = cache["market_id"]
                try:
                    trade_data = json.loads(cache["trade_json"])
                    status_str = trade_data.get("status")
                    
                    if status_str in (TradeState.LOCKED.value, TradeState.SETTLED.value, TradeState.FAILED.value):
                        _db.delete_trade_cache(market_id, self.strategy_id, DB_PATH)
                        continue
                        
                    logger.info(f"[策略FSM：{self.strategy_id}] 从 DB 恢复未对冲敞口: {market_id}, 状态: {status_str}")
                    
                    self._set_trade(market_id, trade_data)
                    fsm = self._init_fsm_for_market(market_id)
                    fsm._state = TradeState(status_str)
                    
                    market_mock = {
                        "id": market_id,
                        "tokens": trade_data.get("tokens", {})
                    }
                    threading.Thread(
                        target=lambda m=market_mock, f=fsm: asyncio.run(self._fsm_ws_listener(m, f)),
                        daemon=True,
                    ).start()
                    
                except Exception as e:
                    logger.warning(f"[策略FSM：{self.strategy_id}] 恢复敞口 {market_id} 失败: {e}")
                    
        except Exception as e:
            logger.warning(f"[策略FSM：{self.strategy_id}] _recover_active_trades 失败: {e}")

    def _update_trade_status(self, market_id: str, status: Optional[str], **kwargs) -> None:
        super()._update_trade_status(market_id, status, **kwargs)
        trade = self._get_trade(market_id)
        if trade:
            try:
                from polymarket import db as _db
                from polymarket.config import DB_PATH
                current_status = trade.get("status")
                if current_status in (TradeState.LOCKED.value, TradeState.SETTLED.value, TradeState.FAILED.value):
                    ev = 0.0
                    if current_status == TradeState.LOCKED.value:
                        leg1 = trade.get("leg1")
                        leg2 = trade.get("leg2")
                        if leg1 and leg2:
                            try:
                                c1 = float(leg1.get("cost", 0.0))
                                s1 = float(leg1.get("size", 0.0))
                                c2 = float(leg2.get("cost", 0.0))
                                s2 = float(leg2.get("size", 0.0))
                                if s1 > 0 and s2 > 0:
                                    ev = min(s1, s2) - (c1 * s1 + c2 * s2)
                                    trade["profit_usdc"] = ev
                            except Exception:
                                pass
                    _db.archive_trade(market_id, self.strategy_id, json.dumps(trade), ev, DB_PATH)
                    _db.delete_trade_cache(market_id, self.strategy_id, DB_PATH)
                else:
                    _db.upsert_trade_cache(market_id, self.strategy_id, json.dumps(trade), DB_PATH)
            except Exception as e:
                logger.warning(f"[策略FSM：{self.strategy_id}] 持久化 trade cache 失败: {e}")

    def _init_fsm_for_market(self, market_id: str) -> TradeFSM:
        """为单个市场初始化 FSM 并绑定所有流转钩子"""
        fsm = TradeFSM(market_id, initial_state=TradeState.IDLE)
        
        # 注册事件触发器
        fsm.register_transition_hook(TradeState.PENDING_LEG1, self.on_pending_leg1)
        fsm.register_transition_hook(TradeState.LEG1_ONLY, self.on_leg1_only)
        fsm.register_transition_hook(TradeState.PENDING_LEG2, self.on_pending_leg2)
        fsm.register_transition_hook(TradeState.LOCKED, self.on_locked)
        fsm.register_transition_hook(TradeState.SETTLED, self.on_settled)
        fsm.register_transition_hook(TradeState.FAILED, self.on_failed)
        
        self.fsms[market_id] = fsm
        return fsm

    def execute_strategy(self, market: Dict[str, Any]) -> None:
        """入口方法：创建 FSM 并触发首条边。"""
        market_id = market["id"]
        
        if self._is_market_processed(market_id):
            return

        # [风控] 动态资产单边行情拦截
        asset = market.get("__asset_type")
        from polymarket.config import SUPPORTED_ASSETS
        if asset and asset in SUPPORTED_ASSETS:
            from polymarket.kline_analyzer import is_asset_choppy, get_asset_status
            if not is_asset_choppy(asset, limit=10):
                now_ts = time.time()
                if now_ts - self._last_silent_filter_log.get(f"{market_id}_choppy", 0) > 30:
                    logger.info(f"[策略FSM：{self.strategy_id}] {asset} 拒绝入场：检测到单边行情。")
                    self._last_silent_filter_log[f"{market_id}_choppy"] = now_ts
                else:
                    logger.debug(f"[策略FSM：{self.strategy_id}] {asset} 拒绝入场：检测到单边行情。")
                
                # 透传到 Dashboard
                status = get_asset_status(asset)
                err_msg = status.get("error", "单边行情或防瀑布过滤")
                
                risk_logger.push_risk_event(
                    market_id=market_id,
                    asset=asset,
                    strategy=self.strategy_name,
                    reason=f"币安 K 线拦截: {err_msg}",
                    level="error"
                )
                
                self.processed_markets.add(market_id)
                db.mark_market_processed(market_id, self.strategy_id)
                return

        logger.info(f"[策略FSM：{self.strategy_id}] 开始基于 FSM 监控市场：{market_id}")
        
        fsm = self._init_fsm_for_market(market_id)
        
        # 将原有 trade 对象初始化并存入 active_trades，此时状态为 IDLE
        self._set_trade(market_id, {
            "market_id": market_id,
            "status": TradeState.IDLE.value,
            "leg1": None,
            "leg2": None,
            "tokens": market.get("tokens", {}),
            "end_time": float(market.get("endDate", time.time() + 300)), 
            "start_time": time.time(),
        })
        self._add_trade_event(market_id, TradeState.IDLE.value, "开始监听此市场的盘口流动性。")

        # 启动事件循环专门监听这个市场的 WebSocket
        threading.Thread(
            target=lambda: asyncio.run(self._fsm_ws_listener(market, fsm)),
            daemon=True,
        ).start()

    # =========================================================
    # FSM 核心流转回调钩子 (Hooks)
    # =========================================================

    def on_pending_leg1(self, fsm: TradeFSM, **kwargs):
        msg = f"首腿发单：{kwargs.get('order_info', {}).get('order_id', 'unknown')}"
        logger.info(f"[策略FSM：{self.strategy_id}] [FSM Hook] {fsm.market_id} 进入 PENDING_LEG1 状态。{msg}")
        self._add_trade_event(fsm.market_id, TradeState.PENDING_LEG1.value, msg)
        order = kwargs.get("order_info", {})
        leg1_data = {
            "order_id": order.get("order_id"),
            "token": order.get("token_id"),
            "side": order.get("side"),
            "cost": order.get("price"),
            "size": order.get("amount")
        } if order else None
        self._update_trade_status(fsm.market_id, TradeState.PENDING_LEG1.value, leg1=leg1_data)

    def on_leg1_only(self, fsm: TradeFSM, **kwargs):
        msg = "单边敞口倒计时启动，等待另一侧回落锁单。"
        logger.info(f"[策略FSM：{self.strategy_id}] [FSM Hook] {fsm.market_id} 进入 LEG1_ONLY 状态。{msg}")
        self._add_trade_event(fsm.market_id, TradeState.LEG1_ONLY.value, msg)
        self._update_trade_status(
            fsm.market_id, 
            TradeState.LEG1_ONLY.value, 
            leg1_filled_time=time.time()
        )

    def on_pending_leg2(self, fsm: TradeFSM, **kwargs):
        is_stop_loss = kwargs.get("is_stop_loss", False)
        msg = f"二腿发单({'止损' if is_stop_loss else '对冲'})：{kwargs.get('order_info', {}).get('order_id', 'unknown')}"
        logger.info(f"[策略FSM：{self.strategy_id}] [FSM Hook] {fsm.market_id} 进入 PENDING_LEG2 状态。{msg}")
        self._add_trade_event(fsm.market_id, TradeState.PENDING_LEG2.value, msg)
        order = kwargs.get("order_info", {})
        leg2_data = {
            "order_id": order.get("order_id"),
            "token": order.get("token_id"),
            "side": order.get("side"),
            "cost": order.get("price"),
            "size": order.get("amount")
        } if order else None
        
        # 兼容旧逻辑，如果没有传 order_info 而是传了 order_id，也至少写进去
        order_id = order.get("order_id") if order else kwargs.get("order_id")
        self._update_trade_status(fsm.market_id, TradeState.PENDING_LEG2.value, leg2_order_id=order_id, leg2=leg2_data, leg2_issued_time=time.time())

    def on_locked(self, fsm: TradeFSM, **kwargs):
        msg = "双腿全量成交，成功套利锁仓！"
        logger.info(f"[策略FSM：{self.strategy_id}] [FSM Hook] {fsm.market_id} 进入 LOCKED 状态。{msg}")
        self._add_trade_event(fsm.market_id, TradeState.LOCKED.value, msg)
        self._update_trade_status(fsm.market_id, TradeState.LOCKED.value)

    def on_settled(self, fsm: TradeFSM, **kwargs):
        msg = "市场到期或清盘结算。"
        logger.info(f"[策略FSM：{self.strategy_id}] [FSM Hook] {fsm.market_id} 进入 SETTLED 状态。{msg}")
        self._add_trade_event(fsm.market_id, TradeState.SETTLED.value, msg)
        self._update_trade_status(fsm.market_id, TradeState.SETTLED.value)
        self.risk_manager.release_trade_lock(self.strategy_id, fsm.market_id, self.order_amount)

    def on_failed(self, fsm: TradeFSM, **kwargs):
        reason = kwargs.get('reason', '未知')
        msg = f"操作失败或中断，原因：{reason}"
        logger.warning(f"[策略FSM：{self.strategy_id}] [FSM Hook] {fsm.market_id} 进入 FAILED 状态。{msg}")
        self._add_trade_event(fsm.market_id, TradeState.FAILED.value, msg)
        self._update_trade_status(fsm.market_id, TradeState.FAILED.value)
        self.risk_manager.release_trade_lock(self.strategy_id, fsm.market_id, self.order_amount)

    # =========================================================
    # 事件触发源
    # =========================================================

    async def _adaptive_post_order(self, token_id, initial_price, amount, side, initial_order_type, max_slippage=0.005, max_retries=3):
        # 第一次冲击
        order = await self.client.post_order_async(token_id, initial_price, amount, side, initial_order_type)
        if order and not order.get("error"):
            return order
            
        logger.warning(f"[策略FSM：{self.strategy_id}] 首发 FOK 失败，启动滑点微重试 (max_slippage={max_slippage})")
        # 开始重试
        for i in range(max_retries):
            await asyncio.sleep(0.05) # 50ms 后重新发
            
            # 重新拿盘口
            price_info = await self.client.get_market_price_async(token_id)
            if not price_info:
                continue
                
            new_price = price_info["ask"] if side == "BUY" else price_info["bid"]
            
            if side == "BUY" and new_price > initial_price + max_slippage:
                logger.warning(f"[策略FSM：{self.strategy_id}] 重试失败：当前卖一价 {new_price} 已超出滑点上限 {initial_price + max_slippage}")
                break
            if side == "SELL" and new_price < initial_price - max_slippage:
                logger.warning(f"[策略FSM：{self.strategy_id}] 重试失败：当前买一价 {new_price} 已低于滑点下限 {initial_price - max_slippage}")
                break
                
            # 价格仍可接受，重新发起！
            new_price = round(new_price, 4)
            logger.info(f"[策略FSM：{self.strategy_id}] 第 {i+1} 次微重试，以新价格 {new_price} 下单")
            order = await self.client.post_order_async(token_id, new_price, amount, side, initial_order_type)
            if order and not order.get("error"):
                self.risk_manager.record_adaptive_retry(True)
                return order
                
        self.risk_manager.record_adaptive_retry(False)
        return None

    async def _fsm_ws_listener(self, market: Dict[str, Any], fsm: TradeFSM):
        """完全解耦的单一 WebSocket 监听器，现在已重构为接入单例事件总线 (Streamer)。"""
        market_id = market["id"]
        
        if not market.get("tokens") or len(market["tokens"]) < 2:
            return
            
        yes_token = market["tokens"].get("YES")
        no_token = market["tokens"].get("NO")
        
        if not yes_token or not no_token:
            return
        
        # 预先存下 token，方便 timeout daemon 中使用
        asset = market.get("__asset_type", "")
        self._update_trade_status(market_id, None, yes_token=yes_token, no_token=no_token, asset=asset)
        
        streamer = MarketDataStreamer()
        loop = asyncio.get_running_loop()
        queue = asyncio.Queue()
        
        # 注册订阅
        streamer.subscribe(market_id, [yes_token, no_token], queue, loop)
        
        # 【BugFix】由于 Polymarket WS 推送的数据可能是单边或增量更新的
        # 必须在本地缓存该市场内各个 token 的最新盘口数据，否则会导致凑不齐双边价格而无限跳过拦截
        market_prices_cache: Dict[str, Dict[str, float]] = {}
        
        try:
            while fsm.current_state not in (TradeState.SETTLED, TradeState.FAILED, TradeState.LOCKED):
                try:
                    # 从单例数据总线获取深度数据 (Zero-Copy JSON parsing)
                    bundle = await asyncio.wait_for(queue.get(), timeout=5)
                    
                    # ⚠️ 致命 Bug 修复：清空积压的旧数据，只取最新的一帧！
                    # 因为网络请求（比如刚才的下单）可能会阻塞 FSM 零点几秒，这期间总线塞进来了几十条旧价格。
                    # 如果不排空队列，我们会用过期的盘口去对冲，导致血亏！
                    while not queue.empty():
                        bundle = queue.get_nowait()
                        
                except asyncio.TimeoutError:
                    continue
                    
                data = bundle["data"]
                prices = bundle["prices"]
                
                # 累加合并接收到的各个 token 的价格信息
                for asset_id, info in prices.items():
                    if asset_id not in market_prices_cache:
                        market_prices_cache[asset_id] = {}
                    market_prices_cache[asset_id].update(info)
                
                # 模拟核心的事件逻辑
                trade = self.active_trades.get(market_id)

                if fsm.current_state == TradeState.IDLE:
                    yes_info = market_prices_cache.get(str(yes_token), {})
                    no_info = market_prices_cache.get(str(no_token), {})
                    
                    best_ask_yes = yes_info.get("ask")
                    best_ask_no = no_info.get("ask")
                    best_bid_yes = yes_info.get("bid")
                    best_bid_no = no_info.get("bid")

                    if best_ask_yes is None or best_ask_no is None:
                        continue
                        
                    now_ts = time.time()
                    
                    def record_silent_filter(reason: str):
                        t = self.active_trades.get(market_id)
                        if not t:
                            t = {
                                "status": TradeState.IDLE.value,
                                "start_time": now_ts,
                                "end_time": now_ts + 60,
                                "events": []
                            }
                            self._set_trade(market_id, t)
                        t["filter_reason"] = reason

                        if now_ts - self._last_silent_filter_log.get(market_id, 0) > 30:
                            logger.info(f"[{self.strategy_id}] [静默拦截] {market_id} {reason}")
                            self._add_trade_event(market_id, TradeState.IDLE.value, f"静默拦截: {reason}")
                            self._last_silent_filter_log[market_id] = now_ts
                            
                        # 同步压入前端展示面板队列
                        risk_logger.push_risk_event(
                            market_id=market_id,
                            asset=self.market.get("__asset_type", "UNKNOWN"),
                            strategy=self.strategy_name,
                            reason=reason,
                            level="warning"
                        )

                    # 【波动率盾牌】：买卖价差过大说明流动性真空，直接拦截首腿开仓
                    if best_bid_yes is not None and (best_ask_yes - best_bid_yes) > 0.05:
                        record_silent_filter(f"YES 买卖价差 {(best_ask_yes - best_bid_yes):.4f} > 0.05")
                        continue
                    if best_bid_no is not None and (best_ask_no - best_bid_no) > 0.05:
                        record_silent_filter(f"NO 买卖价差 {(best_ask_no - best_bid_no):.4f} > 0.05")
                        continue
                        
                    if best_ask_yes <= best_ask_no:
                        choice = "YES"
                        entry_price = best_ask_yes
                        token_id = yes_token
                    else:
                        choice = "NO"
                        entry_price = best_ask_no
                        token_id = no_token
                        
                    if entry_price > self.entry_max_price:
                        record_silent_filter(f"最优价 {choice} = {entry_price:.4f} > {self.entry_max_price}")
                        continue
                        
                    if entry_price < self.entry_min_price:
                        record_silent_filter(f"极度偏斜盘口 {choice} = {entry_price:.4f} < {self.entry_min_price}")
                        continue
                        
                    if entry_price <= self.entry_max_price:
                        # 1. 检查全账户当前未对冲单腿数量上限
                        unhedged_count = self._get_unhedged_trade_count()
                        if unhedged_count >= self.max_concurrent_unhedged_trades:
                            record_silent_filter(f"达到最大并发敞口数 ({unhedged_count})")
                            continue
                            
                        # 2. 从原始 WS 深度中提取深度并计算微观结构与 VWAP
                        asks_list, bids_list = self._extract_token_depth(data, str(token_id))
                        obi, micro_price = self._calculate_micro_structure(bids_list, asks_list)
                        
                        # 【OBI 极端防守】：如果我们准备做 Taker 买入 (Buy Ask)，但 OBI 极度不利 (比如 < -0.8) 说明有巨大抛压诱多
                        if obi < -0.8:
                            record_silent_filter(f"极端盘口抛压 (OBI: {obi:.2f})，防诱多拦截")
                            continue

                        is_valid, vwap_price, filled_usdc, reason_msg = self._check_orderbook_depth_and_vwap(
                            asks=asks_list,
                            target_usdc_amount=self.order_amount,
                            max_price_threshold=self.entry_max_price,
                            max_slippage_tolerance=self.max_slippage_tolerance,
                        )
                        if not is_valid:
                            record_silent_filter(f"首腿深度不足: {reason_msg}")
                            continue
                        
                        # 使用实际加权均价 VWAP 作为成交/出价基准
                        entry_price = vwap_price

                        entry_price = round(entry_price, 4)
                        amount = round(self.order_amount, 2)
                        
                        # 【风控拦截】申请预扣可用敞口！
                        if not self.risk_manager.acquire_trade_lock(self.strategy_id, market_id, amount):
                            record_silent_filter(f"风控敞口超限 (要求 {amount} USDC)")
                            continue
                            
                        logger.info(f"[FSM 引擎] {market_id} 触发首腿: {choice} @ VWAP={entry_price:.4f}")
                        order = await self._adaptive_post_order(
                            token_id, entry_price, amount, side="BUY", initial_order_type=self.leg1_order_type, max_slippage=self.max_slippage_tolerance
                        )
                        if order:
                            fsm.transition_to(TradeState.PENDING_LEG1, order_info=order)
                            if self._confirm_order_filled(order.get("order_id", "")):
                                fsm.transition_to(TradeState.LEG1_ONLY, order_info=order)
                            else:
                                fsm.transition_to(TradeState.FAILED, reason="首腿 FOK 未成交")
                        else:
                            fsm.transition_to(TradeState.FAILED, reason="首腿 API 请求失败")

                elif fsm.current_state == TradeState.LEG1_ONLY and trade:
                    # 监控二腿补仓与对冲逻辑
                    leg1_info = trade.get("leg1")
                    if not leg1_info:
                        continue
                        
                    leg1_cost = float(leg1_info.get("cost", 0.0))
                    leg1_size = float(leg1_info.get("size", self.order_amount))
                    if leg1_cost <= 0.0:
                        leg1_cost = self.entry_max_price

                    other_token_id = no_token if str(leg1_info.get("token") or leg1_info.get("token_id")) == str(yes_token) else yes_token
                    other_info = market_prices_cache.get(str(other_token_id), {})
                    other_ask = other_info.get("ask")
                    other_bid = other_info.get("bid", 0.0)
                    
                    # 理论最高允许对冲上限（扣除 1% 最低保底利润空间）
                    dynamic_reentry_max = 1.0 - leg1_cost - 0.01
                    amount = round(self.order_amount, 2)
                    other_asks, other_bids = self._extract_token_depth(data, str(other_token_id))

                    # ──────────────────────────────────────────────────────────
                    # 模式 1：二腿 Taker 吃单对冲
                    # ──────────────────────────────────────────────────────────
                    if self.leg2_order_type == "FOK" or self.leg2_price_mode == "ask":
                        if other_ask and other_ask <= dynamic_reentry_max:
                            # 深度与 VWAP 穿透保护
                            is_valid, vwap_est, _, reason_msg = self._check_orderbook_depth_and_vwap(
                                asks=other_asks,
                                target_usdc_amount=amount,
                                max_price_threshold=dynamic_reentry_max,
                                max_slippage_tolerance=self.max_slippage_tolerance,
                            )
                            if not is_valid:
                                logger.debug(f"[二腿深度拦截] {market_id}: {reason_msg}")
                                continue
                            vwap_leg2 = vwap_est

                            # 双腿净收益严格数学检验 (Net EV Check)
                            is_profit, est_ev, profit_msg = self._verify_hedged_profitability(
                                leg1_cost=leg1_cost,
                                leg1_size=leg1_size,
                                leg2_cost=vwap_leg2,
                                leg2_size=amount,
                                min_profit_margin=0.01
                            )
                            if not is_profit:
                                logger.warning(f"[二腿锁利拦截] {market_id}: {profit_msg}")
                                continue

                            final_order_price = round(vwap_leg2, 4)
                            logger.info(f"[FSM 引擎] {market_id} 满足二腿 Taker 条件: @ VWAP={final_order_price:.4f} (预期 EV: {est_ev:.4f})")
                            order = await self.client.post_order_async(
                                other_token_id, final_order_price, amount, side="BUY", order_type="FOK"
                            )
                            if order and not order.get("error"):
                                fsm.transition_to(TradeState.PENDING_LEG2, order_info=order)
                            else:
                                if self.leg2_fallback_to_maker:
                                    logger.warning(f"[FSM 引擎] {market_id} 二腿 Taker 未完全成交，智能降级为 Maker (GTC) 挂单")
                                    # 挂在买一前沿
                                    pegged_bid = min(other_bid + 0.001, dynamic_reentry_max) if other_bid > 0 else (dynamic_reentry_max - 0.02)
                                    fallback_price = round(pegged_bid, 4)
                                    fallback_order = await self.client.post_order_async(
                                        other_token_id, fallback_price, amount, side="BUY", order_type="GTC"
                                    )
                                    if fallback_order and not fallback_order.get("error"):
                                        logger.info(f"[FSM 引擎] 降级 Maker 挂单成功: {fallback_order.get('order_id')} @ {fallback_price}")
                                        trade["pegged_price"] = fallback_price
                                        trade["last_peg_time"] = time.time()
                                        fsm.transition_to(TradeState.PENDING_LEG2, order_info=fallback_order)

                    # ──────────────────────────────────────────────────────────
                    # 模式 2：二腿 Maker 智能挂单对冲 (Order Pegging)
                    # ──────────────────────────────────────────────────────────
                    else:
                        best_bid_avail = other_bid if other_bid > 0 else (dynamic_reentry_max - 0.05)
                        pegged_bid = min(best_bid_avail + 0.001, dynamic_reentry_max)
                        pegged_bid = round(pegged_bid, 4)

                        is_profit, est_ev, profit_msg = self._verify_hedged_profitability(
                            leg1_cost=leg1_cost,
                            leg1_size=leg1_size,
                            leg2_cost=pegged_bid,
                            leg2_size=amount,
                            min_profit_margin=0.01
                        )
                        if is_profit and pegged_bid <= dynamic_reentry_max:
                            logger.info(f"[FSM 引擎] {market_id} 发起智能 Maker 挂单: @ {pegged_bid:.4f}")
                            maker_order = await self.client.post_order_async(
                                other_token_id, pegged_bid, amount, side="BUY", order_type="GTC"
                            )
                            if maker_order and not maker_order.get("error"):
                                trade["pegged_price"] = pegged_bid
                                trade["last_peg_time"] = time.time()
                                fsm.transition_to(TradeState.PENDING_LEG2, order_info=maker_order)

                elif fsm.current_state == TradeState.PENDING_LEG2 and trade:
                    # ──────────────────────────────────────────────────────────
                    # 实时 Maker 动态钉盘追单处理 (Active Pegging Monitor)
                    # ──────────────────────────────────────────────────────────
                    leg2_order_id = trade.get("leg2_order_id")
                    pegged_price = trade.get("pegged_price")
                    last_peg_time = trade.get("last_peg_time", 0.0)
                    now_ts = time.time()

                    if leg2_order_id and pegged_price and (now_ts - last_peg_time) >= 1.0:
                        leg1_info = trade.get("leg1", {})
                        leg1_cost = float(leg1_info.get("cost", self.entry_max_price))
                        dynamic_reentry_max = 1.0 - leg1_cost - 0.01

                        other_token_id = trade.get("leg2", {}).get("token") or trade.get("no_token")
                        other_info = market_prices_cache.get(str(other_token_id), {})
                        current_best_bid = other_info.get("bid", 0.0)

                        # 防一分钱互卷 (Anti-Pennying) 与阶梯跃迁机制
                        if current_best_bid <= pegged_price:
                            # 已经夺回买一或价格回落，清除装死计时
                            if "peg_wait_until" in trade:
                                del trade["peg_wait_until"]
                        else:
                            # 发现落后，启动装死随机延迟
                            if "peg_wait_until" not in trade:
                                import random
                                delay = random.uniform(1.5, 3.5)
                                trade["peg_wait_until"] = now_ts + delay
                                logger.debug(f"[Maker 防护] {market_id} 挂单被超，启动随机迟滞 {delay:.1f}s")
                                
                            if now_ts < trade["peg_wait_until"]:
                                continue
                                
                            # 迟滞期满，对手还在，我们发起阶梯跃迁 (越过 0.002~0.004)
                            del trade["peg_wait_until"]
                            import random
                            step = round(random.uniform(0.002, 0.004), 4)
                            
                            new_pegged = min(current_best_bid + step, dynamic_reentry_max)
                            new_pegged = round(new_pegged, 4)

                            if new_pegged > pegged_price and (new_pegged - pegged_price) >= 0.002 and new_pegged <= dynamic_reentry_max:
                                logger.info(
                                    f"[Maker 动态钉盘] {market_id} 盘口上移 ({pegged_price} -> {new_pegged})，撤销旧单并追挂买一！"
                                )
                                if self.is_live:
                                    try:
                                        self.client.cancel_order(leg2_order_id)
                                    except Exception as e:
                                        logger.warning(f"[Maker 钉盘] 撤销旧挂单异常: {e}")

                                amount = round(self.order_amount, 2)
                                new_order = await self.client.post_order_async(
                                    other_token_id, new_pegged, amount, side="BUY", order_type="GTC"
                                )
                                if new_order and not new_order.get("error"):
                                    trade["leg2_order_id"] = new_order.get("order_id")
                                    trade["pegged_price"] = new_pegged
                                    trade["last_peg_time"] = now_ts
                                    trade["leg2"] = {
                                        "order_id": new_order.get("order_id"),
                                        "token": other_token_id,
                                        "side": "BUY",
                                        "cost": new_pegged,
                                        "size": amount
                                    }

        except Exception as e:
            logger.error(f"[策略FSM：{self.strategy_id}] 数据总线分发异常 {market_id}: {e}")
            if fsm.current_state not in (TradeState.LOCKED, TradeState.SETTLED):
                fsm.transition_to(TradeState.FAILED, reason=str(e))
        finally:
            streamer.unsubscribe(market_id, queue)
            if fsm.current_state == TradeState.IDLE:
                logger.info(f"[FSM WS] 市场 {market_id} 取消订阅，正常结束。")
                
    def _fsm_timeout_daemon(self):
        """全局守护线程，专门处理所有处于耗时的轮询和超时事件"""
        while True:
            time.sleep(1)
            for market_id, fsm in list(self.fsms.items()):
                trade = self.active_trades.get(market_id)
                if not trade:
                    continue
                    
                # 【清理机制】回收历史无效订单与内存
                # 规则：已过期 (>60s) 且 (完全没建仓 或 已进入终态)
                is_expired = time.time() > trade.get("end_time", 0) + 60
                has_no_leg1 = not trade.get("leg1")
                is_terminal = fsm.current_state in (TradeState.SETTLED, TradeState.FAILED, TradeState.LOCKED)
                
                if is_expired and (has_no_leg1 or is_terminal):
                    logger.info(f"[策略FSM：{self.strategy_id}] 清理历史过期/无效订单内存: {market_id}")
                    with self._trades_lock:
                        self.active_trades.pop(market_id, None)
                    self.fsms.pop(market_id, None)
                    continue
                    
                if fsm.current_state == TradeState.LEG1_ONLY:
                    filled_time = trade.get("leg1_filled_time", time.time())
                    elapsed = time.time() - filled_time
                    
                    if elapsed > self.leg1_max_unhedged_seconds:
                        leg1 = trade.get("leg1", {})
                        logger.warning(f"[策略FSM：{self.strategy_id}] [FSM Timer] 触发超时止损: {market_id} 已持仓 {elapsed:.1f}s, 当前持有 token={leg1.get('token')} size={leg1.get('size')}")
                        
                        leg1_token = leg1.get("token")
                        if not leg1_token:
                            fsm.transition_to(TradeState.FAILED, reason="leg1_token为空，无法止损")
                            continue
                            
                        yes_token = trade.get("yes_token")
                        no_token = trade.get("no_token")
                        other_token_id = no_token if str(leg1_token) == str(yes_token) else yes_token
                        
                        if not other_token_id:
                            fsm.transition_to(TradeState.FAILED, reason="找不到二腿token")
                            continue
                            
                        # 【强制逃命止损】为了确保 FOK 能够填满规模并绝对离场，直接设置允许的最大滑点为 0.99（平台撮合会自动用最优价）。
                        stop_price = 0.99
                        amount = round(self.order_amount, 2)
                        logger.info(f"[策略FSM：{self.strategy_id}] [FSM Timer] 执行强制穿透止损二腿: @ {stop_price:.4f} FOK")
                        
                        order = self.client.post_order(
                            other_token_id, stop_price, amount, side="BUY", order_type="FOK"
                        )
                        
                        if order and not order.get("error"):
                            fsm.transition_to(TradeState.PENDING_LEG2, is_stop_loss=True, order_info=order)
                        else:
                            fsm.transition_to(TradeState.FAILED, reason="强制止损市价单发送失败，可能遇到系统级熔断")
                        
                elif fsm.current_state == TradeState.PENDING_LEG2:
                    leg2_order_id = trade.get("leg2_order_id")
                    if not leg2_order_id:
                        continue
                        
                    # 非阻塞查询成交状态
                    status = self._check_order_filled_once(leg2_order_id)
                    if status == "FILLED":
                        logger.info(f"[FSM Timer] 挂单已确认成交: {market_id}")
                        fsm.transition_to(TradeState.LOCKED)
                        continue
                    elif status == "FAILED":
                        logger.warning(f"[策略FSM：{self.strategy_id}] [FSM Timer] 二腿挂单失效，回退到 LEG1_ONLY")
                        trade.pop("leg2_order_id", None)
                        trade.pop("leg2", None)
                        fsm.transition_to(TradeState.LEG1_ONLY)
                        continue
                        
                    # 判断挂单过期时间
                    now = time.time()
                    
                    # 挂单深度跟随 (Pegged Maker)
                    leg2_issued_time = trade.get("leg2_issued_time", now)
                    if (now - leg2_issued_time) > 15:
                        logger.warning(f"[FSM Timer] {market_id} 二腿挂单超过 15 秒未成交，启动 Pegging 撤单重发！")
                        if self.is_live:
                            self.client.cancel_order(leg2_order_id)
                        
                        # 撤单后清除记录，重置为 LEG1_ONLY 以便基于最新盘口动态重新计算对冲价
                        trade.pop("leg2_order_id", None)
                        trade.pop("leg2", None)
                        # 更新一下时间，防止刚刚回退到 LEG1_ONLY 且还未发单就触发首腿 TTL
                        trade["leg1_filled_time"] = now
                        fsm.transition_to(TradeState.LEG1_ONLY)
                        continue
                        
                    time_to_expiry = trade.get("end_time", 0) - now
                    if time_to_expiry > 0 and time_to_expiry <= self.leg2_cancel_before_expiry:
                        logger.warning(
                            f"[FSM Timer] {market_id} 挂单即将到期，剩余 {time_to_expiry:.1f}s，强制取消"
                        )
                        if self.is_live:
                            self.client.cancel_order(leg2_order_id)
                            
                        if self.leg2_fallback_to_maker:
                            logger.info(f"[FSM Timer] {market_id} 回退到吃单强平模式，准备平掉敞口 token={trade.get('leg1', {}).get('token')}")
                            # 重置回 LEG1_ONLY 状态，使下一个 WS push 判断回吃单价
                            fsm.transition_to(TradeState.LEG1_ONLY)
                        else:
                            fsm.transition_to(TradeState.FAILED, reason=f"二腿挂单过期且未配置回退, 锁定敞口 market_id={market_id}")
