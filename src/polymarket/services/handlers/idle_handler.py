from typing import Dict, Any

from polymarket.domain.fsm import TradeFSM, TradeState
from polymarket.domain.models import TradeContext
from polymarket.services.pricing import PricingEngine
from polymarket.services.execution import OrderExecutionService
from polymarket.services.handlers.base import BaseTickHandler
from polymarket.services.handlers.context import StrategyParams, StrategyDependencies, TickBundle, TickFilterLogger

class IdleTickHandler(BaseTickHandler):
    """
    IDLE 状态处理器 (Idle State Handler)。
    
    职责：
    1. 临期交割与流动性真空（买卖价差过大）过滤；
    2. Dual-GTC Bracket 双挂做市开仓与风控预扣；
    3. Taker-Maker 吃单开首腿、自适应滑点微重试与私有 WS/Data API 对账。
    """

    async def handle(
        self,
        market: Dict[str, Any],
        fsm: TradeFSM,
        ctx: TradeContext,
        tick: TickBundle,
        params: StrategyParams,
        deps: StrategyDependencies,
        filter_logger: TickFilterLogger
    ) -> None:
        market_id = market["id"]
        asset_type = market.get("__asset_type", "UNKNOWN")
        now_ts = tick.now_ts

        # 1. 临期交割拦截
        time_to_expiry = ctx.end_time - now_ts if ctx.end_time > 0 else 999.0
        if ctx.end_time > 0 and time_to_expiry < params.min_time_to_expiry_entry:
            filter_logger.intercept(
                market_id, asset_type,
                f"临近交割 (剩余 {time_to_expiry:.1f}s < {params.min_time_to_expiry_entry}s)，禁止开仓",
                ctx, deps
            )
            return

        # 2. 买卖价差过大拦截 (流动性真空)
        if tick.best_bid_yes and (tick.best_ask_yes - tick.best_bid_yes) > 0.05:
            filter_logger.intercept(
                market_id, asset_type,
                f"YES 买卖价差 {(tick.best_ask_yes - tick.best_bid_yes):.4f} > 0.05",
                ctx, deps
            )
            return
        if tick.best_bid_no and (tick.best_ask_no - tick.best_bid_no) > 0.05:
            filter_logger.intercept(
                market_id, asset_type,
                f"NO 买卖价差 {(tick.best_ask_no - tick.best_bid_no):.4f} > 0.05",
                ctx, deps
            )
            return

        # 3. [模式 A] Dual-GTC Bracket 双挂做市
        if params.dual_bracket_entry and tick.best_bid_yes is not None and tick.best_bid_no is not None:
            if deps.get_unhedged_count() >= params.max_concurrent_unhedged_trades:
                filter_logger.intercept(
                    market_id, asset_type,
                    f"达到最大并发敞口数 ({params.max_concurrent_unhedged_trades})",
                    ctx, deps
                )
                return

            yes_p, no_p, err = PricingEngine.calculate_dual_bracket_prices(
                tick.best_bid_yes, tick.best_bid_no, params.entry_max_price, params.entry_min_price
            )
            if err:
                filter_logger.intercept(market_id, asset_type, err, ctx, deps)
                return

            is_prof, net_ev, p_msg = PricingEngine.verify_hedged_profitability(
                yes_p, params.amount, no_p, params.amount,
                leg1_order_type="GTC", leg2_order_type="GTC"
            )
            if not is_prof:
                filter_logger.intercept(market_id, asset_type, f"双挂锁利校验未通过: {p_msg}", ctx, deps)
                return

            safe_p_yes, shares = OrderExecutionService.sanitize_order_params(yes_p, params.amount)
            safe_p_no, _ = OrderExecutionService.sanitize_order_params(no_p, params.amount)
            lock_amount = round(shares * (safe_p_yes + safe_p_no), 2)

            if not deps.risk_manager.acquire_trade_lock(params.strategy_id, market_id, lock_amount, is_live=params.is_live):
                filter_logger.intercept(market_id, asset_type, f"风控敞口超限 (双挂要求 ${lock_amount:.2f})", ctx, deps)
                return

            # 原子双挂发单
            order_yes = {"token_id": str(tick.yes_token), "price": safe_p_yes, "size": shares, "side": "BUY", "order_type": "GTC"}
            order_no = {"token_id": str(tick.no_token), "price": safe_p_no, "size": shares, "side": "BUY", "order_type": "GTC"}
            batch_res = await deps.client.post_batch_orders_async([order_yes, order_no])
            
            if batch_res and batch_res.get("status") != "ERROR":
                fsm.transition_to(TradeState.PENDING_BOTH_LEGS, orders=batch_res.get("orders", []))
            else:
                deps.risk_manager.release_market_lock(params.strategy_id, market_id, is_live=params.is_live)
            return

        # 4. [模式 B] Taker-Maker 吃单开首腿
        min_ask, target_token, target_side = (
            (tick.best_ask_yes, tick.yes_token, "YES")
            if tick.best_ask_yes <= tick.best_ask_no
            else (tick.best_ask_no, tick.no_token, "NO")
        )
        if min_ask <= params.entry_max_price and min_ask >= params.entry_min_price:
            safe_p, safe_s = OrderExecutionService.sanitize_order_params(min_ask, params.amount)
            lock_amount = round(safe_p * safe_s, 2)
            
            if not deps.risk_manager.acquire_trade_lock(params.strategy_id, market_id, lock_amount, is_live=params.is_live):
                filter_logger.intercept(market_id, asset_type, f"风控敞口超限 (首腿要求 ${lock_amount:.2f})", ctx, deps)
                return

            order = await OrderExecutionService.adaptive_post_order(
                deps.client, deps.risk_manager, str(target_token), safe_p, safe_s,
                side="BUY", initial_order_type=params.leg1_order_type, strategy_id=params.strategy_id
            )
            if order:
                ctx.leg1_dir = target_side
                fsm.transition_to(TradeState.PENDING_LEG1, order_info=order)
                # 优先通过私有 WebSocket 极速捕获成交 (超时自动降级至 Data API 终极对账)
                is_fill, pos = await OrderExecutionService.async_reconcile_phantom_fill(
                    deps.client, order.get("orderID") or order.get("order_id"), str(target_token), safe_s, params.strategy_id
                )
                if is_fill:
                    fsm.transition_to(TradeState.LEG1_ONLY)
                else:
                    fsm.transition_to(TradeState.FAILED, reason="首腿成交确认超时")
            else:
                deps.risk_manager.release_market_lock(params.strategy_id, market_id, is_live=params.is_live)
