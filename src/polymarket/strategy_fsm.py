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
from polymarket.services.notifier import DiscordNotifier
from polymarket.risk_manager import RiskManager
from polymarket.services.handlers import (
    StrategyParams,
    StrategyDependencies,
    TickBundle,
    TickFilterLogger,
    MarketTickDispatcher,
)
from polymarket.runtime import AsyncRuntime, BoundedDropOldestQueue
from polymarket.metrics import metrics

class ArbitrageBotFSM(BaseStrategy):
    """
    轻量化 FSM 策略编排器 (Strategy Orchestrator)。
    
    已完成代码分层解耦重构：
    1. 定价与收益计算 -> PricingEngine
    2. 订单执行与对账 -> OrderExecutionService
    3. 自适应强平与止损 -> AdaptiveLiquidatorService
    4. 智能盯盘与反卷 -> MakerPeggingService
    5. SQLite 缓存与归档 -> TradeRepository
    6. 状态机 Tick 分发与处理 -> MarketTickDispatcher (Handlers Pattern)
    7. 异步运行时与事件循环 -> AsyncRuntime (Unified Event Loop)
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

        # 状态处理器体系装配
        self.filter_logger = TickFilterLogger(self.strategy_id)
        self.dispatcher = MarketTickDispatcher()
        self.params = StrategyParams(
            strategy_id=self.strategy_id,
            amount=self.order_amount,
            entry_max_price=self.entry_max_price,
            entry_min_price=self.entry_min_price,
            reentry_trigger=self.reentry_trigger,
            is_live=self.is_live,
            leg1_order_type=self.leg1_order_type,
            leg2_order_type=self.leg2_order_type,
            leg2_price_mode=self.leg2_price_mode,
            dual_bracket_entry=self.dual_bracket_entry,
            max_slippage_tolerance=self.max_slippage_tolerance,
            leg1_max_unhedged_seconds=self.leg1_max_unhedged_seconds,
            max_concurrent_unhedged_trades=self.max_concurrent_unhedged_trades,
            exit_mode=self.exit_mode,
            initial_margin=self.initial_margin,
            breakeven_margin=self.breakeven_margin,
            flip_timeout_sec=self.flip_timeout_sec,
            min_time_to_expiry_entry=self.min_time_to_expiry_entry,
            open_silence_sec=float(strategy_config.get("open_silence_sec", 15.0)),
            max_spread=float(strategy_config.get("max_spread", 0.05)),
            mm_min_bid=float(strategy_config.get("mm_min_bid", 0.38)),
            obi_floor=float(strategy_config.get("obi_floor", -0.40)),
            base_opp_depth=float(strategy_config.get("base_opp_depth", 20.0)),
            opp_depth_amp_mult=float(strategy_config.get("opp_depth_amp_mult", 1.5)),
        )
        self.dependencies = StrategyDependencies(
            client=self.client,
            risk_manager=self.risk_manager,
            repository=self.repository,
            get_trade=self._get_trade,
            set_trade=self._set_trade,
            add_trade_event=self._add_trade_event,
            update_trade_status=self._update_trade_status,
            get_unhedged_count=self._get_unhedged_trade_count,
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
            AsyncRuntime.get_instance().spawn_task(
                self._fsm_ws_listener(market_mock, fsm),
                key=f"recover_{self.strategy_id}_{market_id}",
                strategy_id=self.strategy_id,
                market_id=market_id
            )

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

            # 仅对真正发生过开仓成交 (含 leg1 或 leg2) 的有效交易进行历史归档
            if ctx.leg1 is not None or ctx.leg2 is not None:
                self.repository.archive_trade(self.strategy_id, ctx)
            else:
                self.repository.delete_active_trade(self.strategy_id, market_id)
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
        
        # 策略熔断校验
        cb_config = self.config.get("circuit_breaker", {})
        if not self.risk_manager.is_strategy_allowed(self.strategy_id, cb_config):
            return
            
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
                DiscordNotifier.get_instance().notify_risk_alert(
                    market_id=market_id, asset=asset, strategy_name=self.config.get("name", self.strategy_id),
                    reason=f"K 线防爆盾拦截: {err_msg}", level="warning"
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

        AsyncRuntime.get_instance().spawn_task(
            self._fsm_ws_listener(market, fsm),
            key=f"fsm_{self.strategy_id}_{market_id}",
            strategy_id=self.strategy_id,
            market_id=market_id
        )

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

        # 触发首腿开仓成功战报通知
        trade = self._get_trade(fsm.market_id) or {}
        leg1 = trade.get("leg1") or {}
        strat_name = self.config.get("name", self.strategy_id)
        if leg1:
            DiscordNotifier.get_instance().notify_entry(
                market_id=fsm.market_id,
                asset=trade.get("asset", "crypto"),
                strategy_name=strat_name,
                side=trade.get("leg1_dir", "BUY"),
                price=float(leg1.get("cost", 0.0)),
                shares=float(leg1.get("size", 0.0)),
                is_live=self.is_live,
                order_type=getattr(self, "leg1_order_type", "FOK")
            )

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
        metrics.trades_locked_total.inc(labels={"strategy": self.strategy_id, "asset": "crypto"})

        # 触发双腿锁仓达成战报通知
        trade = self._get_trade(fsm.market_id) or {}
        leg1 = trade.get("leg1") or {}
        leg2 = trade.get("leg2") or {}
        strat_name = self.config.get("name", self.strategy_id)
        if leg1 and leg2:
            DiscordNotifier.get_instance().notify_hedged_lock(
                market_id=fsm.market_id,
                asset=trade.get("asset", "crypto"),
                strategy_name=strat_name,
                leg1_cost=float(leg1.get("cost", 0.0)),
                leg2_cost=float(leg2.get("cost", 0.0)),
                shares=float(leg1.get("size", 0.0)),
                net_ev=float(trade.get("profit_usdc", 0.0)),
                gross_profit=float(trade.get("gross_profit_usdc", 0.0)),
                fee_usdc=float(trade.get("fee_usdc", 0.0)),
                is_live=self.is_live
            )

    def on_settled(self, fsm: TradeFSM, **kwargs):
        msg = "市场到期或清盘结算。"
        logger.info(f"[策略FSM：{self.strategy_id}] [FSM Hook] {fsm.market_id} 进入 SETTLED 状态。{msg}")
        self._add_trade_event(fsm.market_id, TradeState.SETTLED.value, msg)
        self._update_trade_status(fsm.market_id, TradeState.SETTLED.value)
        self.risk_manager.release_market_lock(self.strategy_id, fsm.market_id, is_live=self.is_live)
        
        # 上报策略级熔断统计
        trade = self._get_trade(fsm.market_id) or {}
        pnl = float(trade.get("profit_usdc", 0.0))
        is_fc = trade.get("settlement_type") == "FORCE_CLOSED"
        self.risk_manager.record_trade_result(self.strategy_id, pnl, is_fc)

        if self.is_live:
            self.risk_manager.refresh_balance_from_chain(self.client, min_interval=15.0)

    def settle_market(self, market_id: str) -> None:
        """
        显式结算指定市场并释放风控额度（通常由 Manager 在链上赎回后调用）。
        具备强幂等性与 finally 强制释放保底。
        """
        trade = self._get_trade(market_id)
        if not trade:
            return

        cur_status = trade.get("status")
        # 若已经处于终态则仅做额度确认释放
        if cur_status == TradeState.SETTLED.value:
            self.risk_manager.release_market_lock(self.strategy_id, market_id, is_live=self.is_live)
            return

        try:
            fsm = self.fsms.get(market_id)
            if fsm and fsm.current_state not in (TradeState.SETTLED, TradeState.FAILED):
                fsm.transition_to(TradeState.SETTLED, reason="链上已到期赎回清盘")
            else:
                self._update_trade_status(market_id, TradeState.SETTLED.value)
                self._add_trade_event(market_id, TradeState.SETTLED.value, "链上已到期赎回清盘。")
        except Exception as e:
            logger.warning(f"[策略FSM：{self.strategy_id}] 市场 {market_id} 结算流转异常: {e}")
            self._update_trade_status(market_id, TradeState.SETTLED.value)
        finally:
            # 无论 FSM 状态如何，无条件强制归还风控额度
            self.risk_manager.release_market_lock(self.strategy_id, market_id, is_live=self.is_live)
            if self.is_live:
                self.risk_manager.refresh_balance_from_chain(self.client, min_interval=15.0)

    def on_failed(self, fsm: TradeFSM, **kwargs):
        reason = kwargs.get('reason', '未知')
        msg = f"操作失败或中断，原因：{reason}"
        logger.warning(f"[策略FSM：{self.strategy_id}] [FSM Hook] {fsm.market_id} 进入 FAILED 状态。{msg}")
        self._add_trade_event(fsm.market_id, TradeState.FAILED.value, msg)
        self._update_trade_status(fsm.market_id, TradeState.FAILED.value)
        metrics.liquidations_total.inc(labels={"strategy": self.strategy_id, "reason": str(reason)[:15]})
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
        queue = BoundedDropOldestQueue(maxsize=50)
        streamer.subscribe(market_id, [yes_token, no_token], queue)
        
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

                # 根据 FSM 状态流转处理 (带局部异常隔离，防止单次 Tick 异常导致 WS 监听循环意外崩溃退出)
                try:
                    await self._process_market_tick(
                        market, fsm, yes_token, no_token,
                        best_ask_yes, best_bid_yes, best_ask_no, best_bid_no
                    )
                except Exception as tick_err:
                    logger.error(f"[策略FSM：{self.strategy_id}] 处理市场 {market_id} 单次 Tick 异常: {tick_err}", exc_info=True)

        except Exception as e:
            logger.error(f"[策略FSM：{self.strategy_id}] 监听循环致命异常 ({market_id}): {e}", exc_info=True)
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
        """处理单次盘口变动 tick (通过 MarketTickDispatcher 状态处理器分发)"""
        market_id = market["id"]
        trade = self._get_trade(market_id)
        if not trade:
            return
            
        ctx = TradeContext.from_dict(trade)
        tick = TickBundle(
            yes_token=str(yes_token),
            no_token=str(no_token),
            best_ask_yes=best_ask_yes,
            best_bid_yes=best_bid_yes,
            best_ask_no=best_ask_no,
            best_bid_no=best_bid_no,
            now_ts=time.time(),
        )

        await self.dispatcher.dispatch(
            market=market,
            fsm=fsm,
            ctx=ctx,
            tick=tick,
            params=self.params,
            deps=self.dependencies,
            filter_logger=self.filter_logger,
        )

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
                            time_to_exp = max(0.0, ctx.end_time - time.time()) if ctx.end_time > 0 else 999.0
                            prev_grace = getattr(ctx, "ttl_grace_extended", False)
                            
                            success, close_price, close_size, close_order_id = AdaptiveLiquidatorService.execute_force_close(
                                self.client, ctx, self.strategy_id, allow_grace=True
                            )

                            # 场景 1: 触发了弹性延期缓冲 (非失败，继续等待下一轮)
                            if not success and not prev_grace and getattr(ctx, "ttl_grace_extended", False):
                                self._set_trade(market_id, ctx.to_dict())
                                continue

                            # 场景 2: 强平平仓单成交成功
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

                                # 触发单边敞口强平平仓战报通知
                                DiscordNotifier.get_instance().notify_force_close(
                                    market_id=market_id,
                                    asset=ctx.asset or "crypto",
                                    strategy_name=self.config.get("name", self.strategy_id),
                                    leg1_cost=ctx.leg1.cost,
                                    vwap_close_price=close_price,
                                    shares=ctx.leg1.size,
                                    realized_pnl=realized_pnl,
                                    hold_seconds=max(0.0, elapsed),
                                    is_live=self.is_live
                                )

                                fsm.transition_to(TradeState.SETTLED, reason=f"自适应 TTL 强平完成 (平仓卖出价: {close_price}, PnL: ${realized_pnl:.4f})")
                            
                            # 场景 3: 平仓单暂时未成交，但距离交割还有时间 (>5s)，保持持仓继续重试
                            elif time_to_exp > 5.0:
                                logger.warning(f"[策略FSM：{self.strategy_id}] 强平单暂未撮合，剩余 {time_to_exp:.1f}s，下一轮守护继续重试平仓")
                                continue

                            # 场景 4: 真正临期交割 (<5s) 且平仓未果，执行最终交割清算
                            else:
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

                                    from polymarket.metrics import metrics
                                    metrics.expiry_resolved_total.inc()

                                    fsm.transition_to(TradeState.FAILED, reason=f"单边敞口持有至到期交割 (交割价: {settle_price}, PnL: ${settled_pnl:.4f})")
                                else:
                                    fsm.transition_to(TradeState.FAILED, reason="自适应 TTL 平仓失败且无首腿持仓")
                            
            except Exception as e:
                logger.critical(f"[策略FSM：{self.strategy_id}] 强平守护线程发生严重异常: {e}", exc_info=True)
                risk_logger.push_risk_event(strategy=self.strategy_id, reason=f"强平守护异常: {e}", level="critical")
                continue

