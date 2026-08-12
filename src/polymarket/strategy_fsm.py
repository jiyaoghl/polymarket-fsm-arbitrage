import json
import time
import threading
import asyncio
from typing import Dict, Any, Optional

import websockets

from polymarket.base_strategy import BaseStrategy
from polymarket.fsm import TradeFSM, TradeState
from polymarket.logger import logger
from polymarket.kline_analyzer import is_btc_choppy

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
        # 负责触发超时的后台守护线程
        self._timer_thread = threading.Thread(target=self._fsm_timeout_daemon, daemon=True)
        self._timer_thread.start()
        
        self._recover_active_trades()

    def _recover_active_trades(self):
        try:
            from polymarket import db as _db
            from polymarket.config import DB_PATH
            caches = _db.get_all_trade_caches(self.strategy_id, DB_PATH)
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

        # [风控] BTC 单边行情拦截
        market_desc = market.get("description", "").lower()
        if "btc" in market_desc or "bitcoin" in market_desc:
            if not is_btc_choppy():
                logger.info(f"[策略FSM：{self.strategy_id}] 拒绝入场：检测到单边行情。")
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

        # 启动事件循环专门监听这个市场的 WebSocket
        threading.Thread(
            target=lambda: asyncio.run(self._fsm_ws_listener(market, fsm)),
            daemon=True,
        ).start()

    # =========================================================
    # FSM 核心流转回调钩子 (Hooks)
    # =========================================================

    def on_pending_leg1(self, fsm: TradeFSM, **kwargs):
        logger.info(f"[策略FSM：{self.strategy_id}] [FSM Hook] {fsm.market_id} 进入 PENDING_LEG1 状态。首腿发单：{kwargs}")
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
        logger.info(f"[策略FSM：{self.strategy_id}] [FSM Hook] {fsm.market_id} 进入 LEG1_ONLY 状态。启动单边敞口倒计时。")
        self._update_trade_status(
            fsm.market_id, 
            TradeState.LEG1_ONLY.value, 
            leg1_filled_time=time.time()
        )

    def on_pending_leg2(self, fsm: TradeFSM, **kwargs):
        logger.info(f"[策略FSM：{self.strategy_id}] [FSM Hook] {fsm.market_id} 进入 PENDING_LEG2 状态。执行对冲或止损发单。")
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
        logger.info(f"[策略FSM：{self.strategy_id}] [FSM Hook] {fsm.market_id} 进入 LOCKED 状态。双腿安全对冲。")
        self._update_trade_status(fsm.market_id, TradeState.LOCKED.value)

    def on_settled(self, fsm: TradeFSM, **kwargs):
        logger.info(f"[策略FSM：{self.strategy_id}] [FSM Hook] {fsm.market_id} 进入 SETTLED 状态。流程结束。")
        self._update_trade_status(fsm.market_id, TradeState.SETTLED.value)
        self.risk_manager.release_trade_lock(self.strategy_id, fsm.market_id, self.order_amount)

    def on_failed(self, fsm: TradeFSM, **kwargs):
        logger.warning(f"[策略FSM：{self.strategy_id}] [FSM Hook] {fsm.market_id} 进入 FAILED 状态。异常原因：{kwargs.get('reason')}")
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
        """完全解耦的单一 WebSocket 监听器，收到消息后向 FSM 派发事件。"""
        market_id = market["id"]
        
        if not market.get("tokens") or len(market["tokens"]) < 2:
            return
            
        yes_token = market["tokens"].get("YES")
        no_token = market["tokens"].get("NO")
        
        if not yes_token or not no_token:
            return
        
        # 预先存下 token，方便 timeout daemon 中使用
        self._update_trade_status(market_id, None, yes_token=yes_token, no_token=no_token)
        
        try:
            ws = await self._ws_connect_with_retry(self.WS_URI)
            if not ws:
                fsm.transition_to(TradeState.FAILED, reason="WS 连接失败")
                return

            await ws.send(json.dumps({
                "type": "market",
                "assets_ids": [yes_token, no_token],
                "custom_feature_enabled": True,
            }))
            
            while True:
                # 假如状态已经抵达终态，退出监听
                if fsm.current_state in (TradeState.SETTLED, TradeState.FAILED, TradeState.LOCKED):
                    break
                    
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    if ws:
                        try:
                            await ws.ping()
                        except Exception as e:
                            logger.warning(f"[策略FSM：{self.strategy_id}] WS 心跳失败，连接已断开: {e}")
                            break
                    continue
                    
                data = json.loads(msg)
                prices = self._parse_ws_prices_full(data)
                if not prices:
                    continue

                # 模拟核心的事件逻辑（不再有嵌套死循环，只根据 state 执行对应的行为）
                trade = self.active_trades.get(market_id)

                if fsm.current_state == TradeState.IDLE:
                    yes_info = prices.get(str(yes_token), {})
                    no_info = prices.get(str(no_token), {})
                    
                    best_ask_yes = yes_info.get("ask")
                    best_ask_no = no_info.get("ask")
                    best_bid_yes = yes_info.get("bid")
                    best_bid_no = no_info.get("bid")

                    if best_ask_yes is None or best_ask_no is None:
                        continue
                        
                    # 【波动率盾牌】：价差过大直接拦截首腿开仓
                    if best_bid_yes is not None and (best_ask_yes - best_bid_yes) > 0.05:
                        continue
                    if best_bid_no is not None and (best_ask_no - best_bid_no) > 0.05:
                        continue
                        
                    if best_ask_yes <= best_ask_no:
                        choice = "YES"
                        entry_price = best_ask_yes
                        token_id = yes_token
                    else:
                        choice = "NO"
                        entry_price = best_ask_no
                        token_id = no_token
                        
                    if entry_price <= self.entry_max_price:
                        # 1. 检查全账户当前未对冲单腿数量上限
                        unhedged_count = self._get_unhedged_trade_count()
                        if unhedged_count >= self.max_concurrent_unhedged_trades:
                            continue
                            
                        # 2. 从原始 WS 消息中提取 Ask 深度并校验 VWAP 与滑点
                        asks_list = []
                        items = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
                        for item in items:
                            if isinstance(item, dict) and (item.get("asset_id") == str(token_id) or item.get("token_id") == str(token_id)):
                                asks_list = item.get("asks", [])
                                break

                        if asks_list:
                            is_valid, vwap_price, filled_usdc, reason_msg = self._check_orderbook_depth_and_vwap(
                                asks=asks_list,
                                target_usdc_amount=self.order_amount,
                                max_price_threshold=self.entry_max_price,
                                max_slippage_tolerance=self.max_slippage_tolerance,
                            )
                            if not is_valid:
                                logger.warning(
                                    f"[策略FSM：{self.strategy_id}] [WS 首腿] 价格触发但 VWAP 校验拒绝: {reason_msg}"
                                )
                                continue
                            # 使用 VWAP 作为实际下单价格
                            entry_price = vwap_price

                        entry_price = round(entry_price, 4)
                        amount = round(self.order_amount, 2)
                        
                        # 【风控拦截】申请预扣可用敞口！
                        if not self.risk_manager.acquire_trade_lock(self.strategy_id, market_id, amount):
                            continue
                            
                        logger.info(f"[FSM 引擎] {market_id} 触发首腿: {choice} @ {entry_price:.4f}")
                        order = await self._adaptive_post_order(
                            token_id, entry_price, amount, side="BUY", initial_order_type=self.leg1_order_type, max_slippage=self.max_slippage_tolerance
                        )
                        if order:
                            fsm.transition_to(TradeState.PENDING_LEG1, order_info=order)
                            # 对于 FOK 来说，如果成交就是直接有仓位了，我们简化处理直接进入 LEG1_ONLY
                            # 在实际中应该靠 PENDING_LEG1 去通过 REST 轮询校验，这里为展示模型直接进入
                            if self._confirm_order_filled(order.get("order_id", "")):
                                fsm.transition_to(TradeState.LEG1_ONLY, order_info=order)
                            else:
                                fsm.transition_to(TradeState.FAILED, reason="首腿 FOK 未成交")
                        else:
                            fsm.transition_to(TradeState.FAILED, reason="首腿 API 请求失败")

                elif fsm.current_state == TradeState.LEG1_ONLY and trade:
                    # 监控二腿逻辑
                    leg1_info = trade.get("leg1")
                    if not leg1_info:
                        continue
                        
                    leg1_cost = float(leg1_info.get("cost", 0.0))
                    if leg1_cost <= 0.0:
                        # 兜底情况，找不到精准 cost 则用挂单配置替代
                        leg1_cost = self.entry_max_price

                    other_token_id = no_token if str(leg1_info.get("token_id")) == str(yes_token) else yes_token
                    other_info = prices.get(str(other_token_id), {})
                    other_ask = other_info.get("ask")
                    
                    # 动态 Delta 对冲阈值 (预留 0.01 滑点和摩擦手续费)
                    # 比如首腿 0.36 拿到，二腿最高可接受 1.0 - 0.36 - 0.01 = 0.63！这极大提高了胜率！
                    dynamic_reentry_max = 1.0 - leg1_cost - 0.01
                    
                    if other_ask and other_ask <= dynamic_reentry_max:
                        other_ask = round(other_ask, 4)
                        amount = round(self.order_amount, 2)
                        logger.info(f"[FSM 引擎] {market_id} 动态阈值触发({other_ask} <= {dynamic_reentry_max:.4f}) 锁利二腿!")
                        order = await self.client.post_order_async(
                            other_token_id, other_ask, amount, side="BUY", order_type=self.leg2_order_type
                        )
                        if order and not order.get("error"):
                            fsm.transition_to(TradeState.PENDING_LEG2, order_info=order)
                        else:
                            # 触发 GTC 降级
                            logger.warning(f"[FSM 引擎] {market_id} 二腿 Taker 遭遇拒单或滑点过大，开启降级至 GTC (Maker)")
                            # 降级 Maker 时，我们可以选择使用稍微便宜点的挂单价 (比如贴着 dynamic threshold 挂)
                            fallback_price = round(dynamic_reentry_max, 4)
                            fallback_order = await self.client.post_order_async(
                                other_token_id, fallback_price, amount, side="BUY", order_type="GTC"
                            )
                            if fallback_order and not fallback_order.get("error"):
                                logger.info(f"[FSM 引擎] 成功降级为 Maker (GTC) 挂单: {fallback_order.get('order_id')}")
                                fsm.transition_to(TradeState.PENDING_LEG2, order_info=fallback_order)

        except Exception as e:
            logger.error(f"[FSM WS] 监听异常 {market_id}: {e}")
            if fsm.current_state not in (TradeState.LOCKED, TradeState.SETTLED):
                fsm.transition_to(TradeState.FAILED, reason=str(e))
                
    def _fsm_timeout_daemon(self):
        """全局守护线程，专门处理所有处于耗时的轮询和超时事件"""
        while True:
            time.sleep(1)
            for market_id, fsm in list(self.fsms.items()):
                trade = self.active_trades.get(market_id)
                if not trade:
                    continue
                    
                if fsm.current_state == TradeState.LEG1_ONLY:
                    filled_time = trade.get("leg1_filled_time", time.time())
                    elapsed = time.time() - filled_time
                    
                    if elapsed > self.leg1_max_unhedged_seconds:
                        logger.warning(f"[策略FSM：{self.strategy_id}] [FSM Timer] 触发超时止损: {market_id} 已持仓 {elapsed:.1f}s")
                        
                        leg1 = trade.get("leg1", {})
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
                            
                        stop_price = round(self._calculate_dynamic_stop_price(other_token_id), 4)
                        amount = round(self.order_amount, 2)
                        logger.info(f"[策略FSM：{self.strategy_id}] [FSM Timer] 执行止损二腿: @ {stop_price:.4f}")
                        
                        order = self.client.post_order(
                            other_token_id, stop_price, amount, side="BUY", order_type="FOK"
                        )
                        
                        if order:
                            fsm.transition_to(TradeState.PENDING_LEG2, is_stop_loss=True, order_info=order)
                        else:
                            fsm.transition_to(TradeState.FAILED, reason="止损市价单发送失败")
                        
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
                            
                        if self.leg2_fallback_to_taker:
                            logger.info(f"[FSM Timer] 回退到吃单强平模式")
                            # 重置回 LEG1_ONLY 状态，使下一个 WS push 判断回吃单价
                            fsm.transition_to(TradeState.LEG1_ONLY)
                        else:
                            fsm.transition_to(TradeState.FAILED, reason="二腿挂单过期且未配置回退")
