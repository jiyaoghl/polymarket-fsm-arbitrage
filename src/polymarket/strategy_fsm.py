import json
import time
import threading
import asyncio
from typing import Dict, Any, Optional, List

from polymarket.base_strategy import BaseStrategy
from polymarket import risk_logger
from polymarket.logger import logger
from polymarket.kline_analyzer import is_asset_choppy, get_asset_status
from polymarket.streamer import MarketDataStreamer
from polymarket.config import SUPPORTED_ASSETS, DB_PATH

from polymarket.domain.models import TradeContext, LegPosition
from polymarket.domain.fsm import TradeFSM, TradeState
from polymarket.services.pricing import PricingEngine
from polymarket.services.execution import OrderExecutionService
from polymarket.services.liquidator import AdaptiveLiquidatorService
from polymarket.services.pegging import MakerPeggingService
from polymarket.services.repository import TradeRepository
from polymarket.risk_manager import RiskManager

class ArbitrageBotFSM(BaseStrategy):
    """
    轻量化 FSM 策略编排器 (Strategy Orchestrator)。
    
    已完成代码分层解耦重构：
    1. 定价与收益计算 -> PricingEngine
    2. 订单执行与对账 -> OrderExecutionService
    3. 自适应强平与止损 -> AdaptiveLiquidatorService
    4. 智能盯盘与反卷 -> MakerPeggingService
    5. SQLite 缓存与归档 -> TradeRepository
    """
    
    def __init__(self, strategy_config: Dict[str, Any]):
        super().__init__(strategy_config)
        
        # 基础设施与服务装配
        self.risk_manager = RiskManager()
        self.repository = TradeRepository(DB_PATH)
        
        # 状态机映射: market_id -> TradeFSM
        self.fsms: Dict[str, TradeFSM] = {}
        self._last_silent_filter_log: Dict[str, float] = {}
        
        # 做市双挂配置 (Dual-GTC Bracket)
        self.dual_bracket_entry = strategy_config.get(
            "dual_bracket_entry",
            (self.leg1_order_type == "GTC" and self.leg2_order_type == "GTC")
        )
        
        # 恢复活跃未对冲敞口
        self._recover_active_trades()
        
        # 启动自适应超时强平守护线程
        self._timer_thread = threading.Thread(target=self._fsm_timeout_daemon, daemon=True, name=f"Liquidator_{self.strategy_id}")
        self._timer_thread.start()

    def _recover_active_trades(self):
        """开机从 SQLite 仓储层恢复未对冲敞口"""
        recovered_contexts = self.repository.recover_unhedged_trades(self.strategy_id, self.is_live)
        for ctx in recovered_contexts:
            market_id = ctx.market_id
            self._set_trade(market_id, ctx.to_dict())
            
            fsm = self._init_fsm_for_market(market_id)
            fsm._state = TradeState(ctx.status)
            
            market_mock = {
                "id": market_id,
                "tokens": ctx.tokens,
                "__asset_type": ctx.asset
            }
            threading.Thread(
                target=lambda m=market_mock, f=fsm: asyncio.run(self._fsm_ws_listener(m, f)),
                daemon=True,
            ).start()

    def _update_trade_status(self, market_id: str, status: Optional[str], **kwargs) -> None:
        """更新交易状态并同步到仓储层"""
        super()._update_trade_status(market_id, status, **kwargs)
        trade = self._get_trade(market_id)
        if not trade:
            return

        ctx = TradeContext.from_dict(trade)
        current_status = ctx.status
        
        # 若进入终态，核算净 EV 并归档
        if current_status in (TradeState.LOCKED.value, TradeState.SETTLED.value, TradeState.FAILED.value):
            if current_status == TradeState.LOCKED.value and ctx.leg1 and ctx.leg2:
                gross, fee, net = PricingEngine.calculate_net_ev(
                    ctx.leg1.cost, ctx.leg1.size,
                    ctx.leg2.cost, ctx.leg2.size,
                    self.leg1_order_type, self.leg2_order_type
                )
                ctx.gross_profit_usdc = gross
                ctx.fee_usdc = fee
                ctx.profit_usdc = net
                self._set_trade(market_id, ctx.to_dict())
                
            self.repository.archive_trade(self.strategy_id, ctx)
        else:
            self.repository.save_active_trade(self.strategy_id, ctx)

    def _init_fsm_for_market(self, market_id: str) -> TradeFSM:
        """为单个市场初始化 FSM 并绑定所有流转钩子"""
        fsm = TradeFSM(market_id, initial_state=TradeState.IDLE)
        
        fsm.register_transition_hook(TradeState.PENDING_LEG1, self.on_pending_leg1)
        fsm.register_transition_hook(TradeState.PENDING_BOTH_LEGS, self.on_pending_both_legs)
        fsm.register_transition_hook(TradeState.LEG1_ONLY, self.on_leg1_only)
        fsm.register_transition_hook(TradeState.PENDING_LEG2, self.on_pending_leg2)
        fsm.register_transition_hook(TradeState.LOCKED, self.on_locked)
        fsm.register_transition_hook(TradeState.SETTLED, self.on_settled)
        fsm.register_transition_hook(TradeState.FAILED, self.on_failed)
        
        self.fsms[market_id] = fsm
        return fsm

    def execute_strategy(self, market: Dict[str, Any]) -> None:
        """入口调度方法：校验行情单边防爆盾并拉起市场监听"""
        market_id = market["id"]
        if self._is_market_processed(market_id):
            return

        # 动态 K 线防爆盾校验
        asset = market.get("__asset_type")
        if asset and asset in SUPPORTED_ASSETS:
            if not is_asset_choppy(asset, limit=10):
                now_ts = time.time()
                if now_ts - self._last_silent_filter_log.get(f"{market_id}_choppy", 0) > 30:
                    logger.info(f"[策略FSM：{self.strategy_id}] {asset} 拒绝入场：检测到单边行情。")
                    self._last_silent_filter_log[f"{market_id}_choppy"] = now_ts
                
                status = get_asset_status(asset)
                amp = status.get("amplitude", 0.0)
                net = status.get("net_change", 0.0)
                err_msg = status.get("error") or f"单边波幅过大 (振幅 {amp:.2f}%, 净变 {net:.2f}%)"
                
                risk_logger.push_risk_event(
                    market_id=market_id, asset=asset, strategy=self.strategy_id,
                    reason=f"币安 K 线防爆盾: {err_msg}", level="error"
                )
                return

        logger.info(f"[策略FSM：{self.strategy_id}] 开始基于 FSM 监控市场：{market_id}")
        fsm = self._init_fsm_for_market(market_id)
        
        ctx = TradeContext(
            market_id=market_id,
            status=TradeState.IDLE.value,
            asset=asset or "",
            tokens=market.get("tokens", {}),
            end_time=float(market.get("expiry") or market.get("endDate") or (time.time() + 300))
        )
        ctx.add_event(TradeState.IDLE.value, "开始监听此市场的盘口流动性。")
        self._set_trade(market_id, ctx.to_dict())

        threading.Thread(
            target=lambda: asyncio.run(self._fsm_ws_listener(market, fsm)),
            daemon=True,
        ).start()

    # =========================================================
    # FSM 核心流转回调钩子 (Hooks)
    # =========================================================

    def on_pending_leg1(self, fsm: TradeFSM, **kwargs):
        order = kwargs.get("order_info", {})
        msg = f"首腿发单：{order.get('order_id', 'unknown')}"
        logger.info(f"[策略FSM：{self.strategy_id}] [FSM Hook] {fsm.market_id} 进入 PENDING_LEG1 状态。{msg}")
        self._add_trade_event(fsm.market_id, TradeState.PENDING_LEG1.value, msg)
        
        leg1 = LegPosition.from_dict(order)
        self._update_trade_status(fsm.market_id, TradeState.PENDING_LEG1.value, leg1=leg1.to_dict() if leg1 else None)

    def on_pending_both_legs(self, fsm: TradeFSM, **kwargs):
        orders = kwargs.get("orders", [])
        msg = f"双腿并发挂单：YES={orders[0].get('order_id') if len(orders)>0 else 'N/A'}, NO={orders[1].get('order_id') if len(orders)>1 else 'N/A'}"
        logger.info(f"[策略FSM：{self.strategy_id}] [FSM Hook] {fsm.market_id} 进入 PENDING_BOTH_LEGS 状态。{msg}")
        self._add_trade_event(fsm.market_id, TradeState.PENDING_BOTH_LEGS.value, msg)
        self._update_trade_status(
            fsm.market_id, TradeState.PENDING_BOTH_LEGS.value,
            dual_orders=orders, dual_issued_time=time.time()
        )

    def on_leg1_only(self, fsm: TradeFSM, **kwargs):
        msg = "单边敞口倒计时启动，等待另一侧回落锁单。"
        logger.info(f"[策略FSM：{self.strategy_id}] [FSM Hook] {fsm.market_id} 进入 LEG1_ONLY 状态。{msg}")
        self._add_trade_event(fsm.market_id, TradeState.LEG1_ONLY.value, msg)
        self._update_trade_status(fsm.market_id, TradeState.LEG1_ONLY.value, leg1_filled_time=time.time())

    def on_pending_leg2(self, fsm: TradeFSM, **kwargs):
        is_stop = kwargs.get("is_stop_loss", False)
        order = kwargs.get("order_info", {})
        msg = f"二腿发单({'止损' if is_stop else '对冲'})：{order.get('order_id', 'unknown')}"
        logger.info(f"[策略FSM：{self.strategy_id}] [FSM Hook] {fsm.market_id} 进入 PENDING_LEG2 状态。{msg}")
        self._add_trade_event(fsm.market_id, TradeState.PENDING_LEG2.value, msg)
        
        leg2 = LegPosition.from_dict(order)
        order_id = order.get("order_id") if order else kwargs.get("order_id")
        self._update_trade_status(
            fsm.market_id, TradeState.PENDING_LEG2.value,
            leg2_order_id=order_id, leg2=leg2.to_dict() if leg2 else None, leg2_issued_time=time.time()
        )

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
        self.risk_manager.release_market_lock(self.strategy_id, fsm.market_id, is_live=self.is_live)
        if self.is_live:
            self.risk_manager.refresh_balance_from_chain(self.client, min_interval=15.0)

    def on_failed(self, fsm: TradeFSM, **kwargs):
        reason = kwargs.get('reason', '未知')
        msg = f"操作失败或中断，原因：{reason}"
        logger.warning(f"[策略FSM：{self.strategy_id}] [FSM Hook] {fsm.market_id} 进入 FAILED 状态。{msg}")
        self._add_trade_event(fsm.market_id, TradeState.FAILED.value, msg)
        self._update_trade_status(fsm.market_id, TradeState.FAILED.value)
        self.risk_manager.release_market_lock(self.strategy_id, fsm.market_id, is_live=self.is_live)
        if self.is_live:
            self.risk_manager.refresh_balance_from_chain(self.client, min_interval=15.0)

    # =========================================================
    # 事件流驱动与盘口监听
    # =========================================================

    async def _fsm_ws_listener(self, market: Dict[str, Any], fsm: TradeFSM):
        """接入单例事件总线 (Streamer) 的异步事件监听器"""
        market_id = market["id"]
        tokens = market.get("tokens", {})
        yes_token = tokens.get("YES")
        no_token = tokens.get("NO")
        
        if not yes_token or not no_token:
            return

        streamer = MarketDataStreamer()
        loop = asyncio.get_running_loop()
        queue = asyncio.Queue()
        streamer.subscribe(market_id, [yes_token, no_token], queue, loop)
        
        market_prices_cache: Dict[str, Dict[str, float]] = {}
        last_ws_msg_time = time.time()
        last_ws_timeout_log = 0

        market_expiry = float(market.get("expiry") or market.get("endDate") or 0)

        try:
            while fsm.current_state not in (TradeState.SETTLED, TradeState.FAILED, TradeState.LOCKED):
                now_ts = time.time()
                # 市场已到期，安全退出并清理
                if market_expiry > 0 and now_ts >= market_expiry:
                    if fsm.current_state == TradeState.IDLE:
                        logger.info(f"[策略FSM：{self.strategy_id}] 市场 {market_id} 已到期且未开仓，安全退出监听。")
                    else:
                        logger.info(f"[策略FSM：{self.strategy_id}] 市场 {market_id} 已到期，退出 WS 监听（当前状态: {fsm.current_state.value}）。")
                    break

                try:
                    bundle = await asyncio.wait_for(queue.get(), timeout=5)
                    # 排空旧数据队列，保持最新盘口
                    while not queue.empty():
                        bundle = queue.get_nowait()
                except asyncio.TimeoutError:
                    now_ts = time.time()
                    if market_expiry > 0 and now_ts >= market_expiry:
                        break

                    # 仅在市场尚未到期时才记录断流警告
                    if now_ts - last_ws_msg_time > 60 and now_ts - last_ws_timeout_log > 60:
                        logger.warning(f"[策略FSM：{self.strategy_id}] 市场 {market_id} 超过 60 秒未收到行情推送。")
                        last_ws_timeout_log = now_ts
                    continue

                last_ws_msg_time = time.time()
                for asset_id, info in bundle.get("prices", {}).items():
                    market_prices_cache.setdefault(asset_id, {}).update(info)

                yes_info = market_prices_cache.get(str(yes_token), {})
                no_info = market_prices_cache.get(str(no_token), {})
                best_ask_yes, best_bid_yes = yes_info.get("ask"), yes_info.get("bid")
                best_ask_no, best_bid_no = no_info.get("ask"), no_info.get("bid")

                if best_ask_yes is None or best_ask_no is None:
                    continue

                # 根据 FSM 状态流转处理
                await self._process_market_tick(
                    market, fsm, yes_token, no_token,
                    best_ask_yes, best_bid_yes, best_ask_no, best_bid_no
                )

        except Exception as e:
            logger.error(f"[策略FSM：{self.strategy_id}] 监听循环异常 ({market_id}): {e}")
        finally:
            streamer.unsubscribe(market_id, queue)
            # 若从未开仓且已结束，清理活跃交易字典避免内存泄漏
            if fsm.current_state == TradeState.IDLE:
                self._delete_trade(market_id)


    async def _process_market_tick(
        self, market: Dict[str, Any], fsm: TradeFSM,
        yes_token: str, no_token: str,
        best_ask_yes: float, best_bid_yes: Optional[float],
        best_ask_no: float, best_bid_no: Optional[float]
    ):
        """处理单次盘口变动 tick"""
        market_id = market["id"]
        trade = self._get_trade(market_id)
        if not trade:
            return
            
        ctx = TradeContext.from_dict(trade)
        now_ts = time.time()

        def filter_and_log(reason: str):
            ctx.filter_reason = reason
            self._set_trade(market_id, ctx.to_dict())
            if now_ts - self._last_silent_filter_log.get(market_id, 0) > 30:
                logger.info(f"[{self.strategy_id}] [静默拦截] {market_id} {reason}")
                self._add_trade_event(market_id, ctx.status, f"静默拦截: {reason}")
                self._last_silent_filter_log[market_id] = now_ts
            risk_logger.push_risk_event(
                market_id=market_id, asset=market.get("__asset_type", "UNKNOWN"),
                strategy=self.strategy_id, reason=reason, level="warning"
            )

        # ── 1. IDLE 状态开仓决策 ──────────────────────────────
        if fsm.current_state == TradeState.IDLE:
            # 临期交割拦截
            time_to_expiry = ctx.end_time - now_ts if ctx.end_time > 0 else 999.0
            if ctx.end_time > 0 and time_to_expiry < self.min_time_to_expiry_entry:
                filter_and_log(f"临近交割 (剩余 {time_to_expiry:.1f}s < {self.min_time_to_expiry_entry}s)，禁止开仓")
                return

            # 买卖价差过大拦截 (流动性真空)
            if best_bid_yes and (best_ask_yes - best_bid_yes) > 0.05:
                filter_and_log(f"YES 买卖价差 {(best_ask_yes - best_bid_yes):.4f} > 0.05")
                return
            if best_bid_no and (best_ask_no - best_bid_no) > 0.05:
                filter_and_log(f"NO 买卖价差 {(best_ask_no - best_bid_no):.4f} > 0.05")
                return

            # [模式 A] Dual-GTC Bracket 双挂做市
            if self.dual_bracket_entry and best_bid_yes is not None and best_bid_no is not None:
                if self._get_unhedged_trade_count() >= self.max_concurrent_unhedged_trades:
                    filter_and_log(f"达到最大并发敞口数 ({self.max_concurrent_unhedged_trades})")
                    return

                yes_p, no_p, err = PricingEngine.calculate_dual_bracket_prices(
                    best_bid_yes, best_bid_no, self.entry_max_price, self.entry_min_price
                )
                if err:
                    filter_and_log(err)
                    return

                is_prof, net_ev, p_msg = PricingEngine.verify_hedged_profitability(
                    yes_p, self.order_amount, no_p, self.order_amount,
                    leg1_order_type="GTC", leg2_order_type="GTC"
                )
                if not is_prof:
                    filter_and_log(f"双挂锁利校验未通过: {p_msg}")
                    return

                safe_p_yes, shares = OrderExecutionService.sanitize_order_params(yes_p, self.order_amount)
                safe_p_no, _ = OrderExecutionService.sanitize_order_params(no_p, self.order_amount)
                lock_amount = round(shares * (safe_p_yes + safe_p_no), 2)

                if not self.risk_manager.acquire_trade_lock(self.strategy_id, market_id, lock_amount, is_live=self.is_live):
                    filter_and_log(f"风控敞口超限 (双挂要求 ${lock_amount:.2f})")
                    return

                # 原子双挂发单
                order_yes = {"token_id": str(yes_token), "price": safe_p_yes, "size": shares, "side": "BUY", "order_type": "GTC"}
                order_no = {"token_id": str(no_token), "price": safe_p_no, "size": shares, "side": "BUY", "order_type": "GTC"}
                batch_res = await self.client.post_batch_orders_async([order_yes, order_no])
                
                if batch_res and batch_res.get("status") != "ERROR":
                    fsm.transition_to(TradeState.PENDING_BOTH_LEGS, orders=batch_res.get("orders", []))
                else:
                    self.risk_manager.release_market_lock(self.strategy_id, market_id, is_live=self.is_live)
                return

            # [模式 B] Taker-Maker 吃单开首腿
            min_ask, target_token, target_side = (best_ask_yes, yes_token, "YES") if best_ask_yes <= best_ask_no else (best_ask_no, no_token, "NO")
            if min_ask <= self.entry_max_price and min_ask >= self.entry_min_price:
                safe_p, safe_s = OrderExecutionService.sanitize_order_params(min_ask, self.order_amount)
                lock_amount = round(safe_p * safe_s, 2)
                
                if not self.risk_manager.acquire_trade_lock(self.strategy_id, market_id, lock_amount, is_live=self.is_live):
                    filter_and_log(f"风控敞口超限 (首腿要求 ${lock_amount:.2f})")
                    return

                order = await OrderExecutionService.adaptive_post_order(
                    self.client, self.risk_manager, str(target_token), safe_p, safe_s,
                    side="BUY", initial_order_type=self.leg1_order_type, strategy_id=self.strategy_id
                )
                if order:
                    ctx.leg1_dir = target_side
                    fsm.transition_to(TradeState.PENDING_LEG1, order_info=order)
                    # 优先通过私有 WebSocket 极速捕获成交 (超时自动降级至 Data API 终极对账)
                    is_fill, pos = await OrderExecutionService.async_reconcile_phantom_fill(
                        self.client, order.get("orderID") or order.get("order_id"), str(target_token), safe_s, self.strategy_id
                    )
                    if is_fill:
                        fsm.transition_to(TradeState.LEG1_ONLY)
                    else:
                        fsm.transition_to(TradeState.FAILED, reason="首腿成交确认超时")
                else:
                    self.risk_manager.release_market_lock(self.strategy_id, market_id, is_live=self.is_live)

        # ── 2. LEG1_ONLY 状态对冲追单 ──────────────────────────
        elif fsm.current_state == TradeState.LEG1_ONLY:
            leg1 = ctx.leg1
            if not leg1:
                return
            
            # 确定二腿 token 与方向
            is_leg1_yes = (str(leg1.token) == str(yes_token))
            opp_token = no_token if is_leg1_yes else yes_token
            opp_ask = best_ask_no if is_leg1_yes else best_ask_yes
            opp_bid = best_bid_no if is_leg1_yes else best_bid_yes

            target_leg2_price = opp_bid if self.leg2_price_mode == "bid" and opp_bid else opp_ask
            if not target_leg2_price:
                return

            # 对冲盈利数学校验
            is_prof, net_ev, p_msg = PricingEngine.verify_hedged_profitability(
                leg1_cost=leg1.cost, leg1_size=leg1.size,
                leg2_cost=target_leg2_price, leg2_size=leg1.size,
                leg1_order_type=self.leg1_order_type, leg2_order_type=self.leg2_order_type
            )
            if is_prof:
                safe_p, safe_s = OrderExecutionService.sanitize_order_params(target_leg2_price, target_leg2_price * leg1.size)
                # 申请二腿额度
                if self.risk_manager.acquire_trade_lock(self.strategy_id, market_id, round(safe_p * safe_s, 2), is_live=self.is_live):
                    order = await self.client.post_order_async(str(opp_token), safe_p, safe_s, "BUY", self.leg2_order_type)
                    if order and order.get("status") not in ("ERROR", None):
                        fsm.transition_to(TradeState.PENDING_LEG2, order_info=order)
                    else:
                        self.risk_manager.release_trade_lock(self.strategy_id, market_id, round(safe_p * safe_s, 2), is_live=self.is_live)

    # =========================================================
    # 后台自适应强平守护守护线程
    # =========================================================

    def _fsm_timeout_daemon(self):
        """顶层 try-except 保护的强平守护线程"""
        logger.info(f"[策略FSM：{self.strategy_id}] 启动动态自适应强平守护线程。")
        while True:
            try:
                time.sleep(2.0)
                trades_snapshot = self._get_all_active_trades()
                
                for market_id, trade_dict in trades_snapshot.items():
                    status = trade_dict.get("status")
                    if status not in (TradeState.LEG1_ONLY.value, TradeState.PENDING_LEG2.value):
                        continue

                    ctx = TradeContext.from_dict(trade_dict)
                    
                    # 获取资产波动率
                    asset = ctx.asset
                    amp_pct = 0.0
                    if asset:
                        st = get_asset_status(asset)
                        amp_pct = st.get("amplitude", 0.0)

                    is_timed_out, elapsed, cur_ttl = AdaptiveLiquidatorService.evaluate_timeout(
                        ctx, base_ttl=float(self.leg1_max_unhedged_seconds),
                        asset_amplitude_pct=amp_pct
                    )
                    # 更新当前动态 TTL 到内存与看板
                    self._update_trade_status(market_id, None, dynamic_ttl=cur_ttl)

                    if is_timed_out:
                        fsm = self.fsms.get(market_id)
                        if fsm and fsm.current_state in (TradeState.LEG1_ONLY, TradeState.PENDING_LEG2):
                            success, close_price, close_size, close_order_id = AdaptiveLiquidatorService.execute_force_close(self.client, ctx, self.strategy_id)
                            if success and close_price is not None and ctx.leg1:
                                leg1_is_taker = (self.leg1_order_type == "FOK")
                                realized_pnl, gross_pnl, fee = AdaptiveLiquidatorService.calculate_realized_pnl(
                                    leg1_cost=ctx.leg1.cost, leg1_size=ctx.leg1.size,
                                    close_price=close_price, leg1_is_taker=leg1_is_taker, close_is_taker=True
                                )
                                ctx.profit_usdc = realized_pnl
                                ctx.realized_pnl = realized_pnl
                                ctx.gross_profit_usdc = gross_pnl
                                ctx.fee_usdc = fee
                                ctx.settlement_type = "FORCE_CLOSED"
                                # 明确将 leg2 标记为平仓卖出明细 (SELL)，消除误导性 BUY 挂单残留
                                ctx.leg2 = LegPosition(
                                    order_id=close_order_id or "force_close",
                                    token=ctx.leg1.token,
                                    side="SELL",
                                    cost=close_price,
                                    size=close_size
                                )
                                self._set_trade(market_id, ctx.to_dict())
                                fsm.transition_to(TradeState.SETTLED, reason=f"自适应 TTL 强平完成 (平仓卖出价: {close_price}, PnL: ${realized_pnl:.4f})")
                            else:
                                # 二腿市价平仓失败 (如临期流动性枯竭或直接进入交割锁定)
                                # 评估最终到期交割结算价格 (Settlement Price)
                                leg1 = ctx.leg1
                                if leg1 and leg1.token:
                                    try:
                                        p_info = self.client.get_market_price(leg1.token)
                                        # 最终结算价优先取盘口最后有效价格，若无买盘则按 0.0 (全额归零) 兜底
                                        settle_price = float(p_info.get("bid") or p_info.get("price") or 0.0)
                                    except Exception:
                                        settle_price = 0.0

                                    leg1_is_taker = (self.leg1_order_type == "FOK")
                                    settled_pnl, gross_pnl, fee = AdaptiveLiquidatorService.calculate_expiry_settled_pnl(
                                        leg1_cost=leg1.cost, leg1_size=leg1.size,
                                        settlement_price=settle_price, leg1_is_taker=leg1_is_taker
                                    )
                                    ctx.profit_usdc = settled_pnl
                                    ctx.realized_pnl = settled_pnl
                                    ctx.gross_profit_usdc = gross_pnl
                                    ctx.fee_usdc = fee
                                    ctx.settlement_price = settle_price
                                    ctx.settlement_type = "EXPIRY_RESOLVED"
                                    # 到期结算将 leg2 明确更新为交割卖出明细
                                    ctx.leg2 = LegPosition(
                                        order_id="expiry_settle",
                                        token=leg1.token,
                                        side="SELL",
                                        cost=settle_price,
                                        size=leg1.size
                                    )
                                    self._set_trade(market_id, ctx.to_dict())
                                    fsm.transition_to(TradeState.SETTLED, reason=f"市价平仓失败，按到期结算价 {settle_price} 结算 (PnL: ${settled_pnl:.4f})")
                                else:
                                    fsm.transition_to(TradeState.FAILED, reason="自适应 TTL 平仓失败且无首腿持仓")
                            
            except Exception as e:
                logger.critical(f"[策略FSM：{self.strategy_id}] 强平守护线程发生严重异常: {e}", exc_info=True)
                risk_logger.push_risk_event(strategy=self.strategy_id, reason=f"强平守护异常: {e}", level="critical")
                continue

