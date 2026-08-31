import time
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

        # 1. 开盘时空窗口守门 (开盘 15s 绝对静默 + 临期交割守门)
        time_to_expiry = ctx.end_time - now_ts if ctx.end_time > 0 else 999.0
        if ctx.end_time > 0:
            # 开盘前 15s 绝对静默保护 (防止开盘瞬间做市商铺单未稳的流动性真空)
            if time_to_expiry >= 285.0:
                filter_logger.intercept(
                    market_id, asset_type,
                    f"开盘前 15s 流动性成熟期 (剩余 {time_to_expiry:.1f}s >= 285.0s)，等待做市铺单",
                    ctx, deps
                )
                return
            # 临近交割拦截
            if time_to_expiry < params.min_time_to_expiry_entry:
                filter_logger.intercept(
                    market_id, asset_type,
                    f"临近交割 (剩余 {time_to_expiry:.1f}s < {params.min_time_to_expiry_entry}s)，禁止开仓",
                    ctx, deps
                )
                return

        # 2. 单市场跨策略排他锁检查 (Market-Level Concurrency Lock)
        if hasattr(deps.risk_manager, "is_market_occupied"):
            res = deps.risk_manager.is_market_occupied(market_id, params.strategy_id)
            if isinstance(res, (tuple, list)) and len(res) == 2:
                is_occ, occ_strat = res
                if is_occ:
                    filter_logger.intercept(
                        market_id, asset_type,
                        f"市场已被策略 [{occ_strat}] 锁定占用 (单市场排他)",
                        ctx, deps
                    )
                    return

        # 3. 买卖价差过大拦截 (流动性真空)
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

            # [盘口成熟度防御] 双边买一必须均 >= 0.38，防止在低成熟度单边行情中盲目挂单
            if tick.best_bid_yes < 0.38 or tick.best_bid_no < 0.38:
                filter_logger.intercept(
                    market_id, asset_type,
                    f"做市盘口流动性尚未成熟 (YES买一 {tick.best_bid_yes:.4f} / NO买一 {tick.best_bid_no:.4f} < 0.38)",
                    ctx, deps
                )
                return

            yes_p, no_p, err = PricingEngine.calculate_dual_bracket_prices(
                best_bid_yes=tick.best_bid_yes,
                best_bid_no=tick.best_bid_no,
                entry_max_price=params.entry_max_price,
                entry_min_price=params.entry_min_price,
                min_profit_margin=params.initial_margin or 0.015,
                best_ask_yes=tick.best_ask_yes,
                best_ask_no=tick.best_ask_no,
                anti_penny_step=0.001
            )
            if err:
                filter_logger.intercept(market_id, asset_type, err, ctx, deps)
                return

            is_prof, net_ev, p_msg = PricingEngine.verify_hedged_profitability(
                yes_p, params.amount, no_p, params.amount,
                min_profit_margin=params.initial_margin or 0.015,
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

        # 4. [模式 B] Taker-Maker 全盘口净 EV 驱动吃单开首腿
        is_opp, target_side, entry_price, expected_ev, opp_reason = PricingEngine.evaluate_taker_ev_opportunity(
            best_ask_yes=tick.best_ask_yes,
            best_bid_yes=tick.best_bid_yes,
            best_ask_no=tick.best_ask_no,
            best_bid_no=tick.best_bid_no,
            entry_max_price=params.entry_max_price,
            entry_min_price=params.entry_min_price,
            min_profit_margin=params.initial_margin or 0.010,
            leg1_amount=params.amount
        )

        if is_opp and target_side and entry_price:
            target_token = tick.yes_token if target_side == "YES" else tick.no_token
            opp_token = tick.no_token if target_side == "YES" else tick.yes_token

            from polymarket.services.grid import OrderbookMemoryGrid
            grid = OrderbookMemoryGrid.get_instance()
            target_snap = grid.get_snapshot(str(target_token))
            opp_snap = grid.get_snapshot(str(opp_token))

            # [OBI 卖盘压迫防御] 目标 Token 卖盘严重压迫时拦截
            if target_snap and not target_snap.is_stale(10.0):
                obi_val, tot_depth, is_valid_depth = PricingEngine.calculate_obi(
                    list(target_snap.bids), list(target_snap.asks), top_n_levels=5, min_total_shares=30.0
                )
                if is_valid_depth and obi_val < -0.40:
                    filter_logger.intercept(
                        market_id, asset_type,
                        f"OBI 卖盘深度严重压迫 (OBI={obi_val:.2f} < -0.40, 总深度={tot_depth:.1f}份)，拦截吃单",
                        ctx, deps
                    )
                    return

            # [对侧买盘 OBI 承接深度壁垒] 对侧买盘前 5 档深度必须 >= 20.0 份
            if opp_snap and not opp_snap.is_stale(10.0):
                opp_bids = list(opp_snap.bids)
                opp_bid_depth = sum(float(b[1] if isinstance(b, (list, tuple)) else b.get("size", 0.0)) for b in opp_bids[:5])
                if opp_bid_depth < 20.0:
                    filter_logger.intercept(
                        market_id, asset_type,
                        f"对侧买盘承接深度不足 (前5档买盘={opp_bid_depth:.1f}份 < 20.0份)，拦截吃单",
                        ctx, deps
                    )
                    return

            safe_p, safe_s = OrderExecutionService.sanitize_order_params(entry_price, params.amount)

            # [多档位穿透式 VWAP 深度吃单] 若卖一深度不足但多档均价满足 entry_max_price，采用加权 VWAP 发单防踏空
            if target_snap and not target_snap.is_stale(10.0):
                vwap_ask = grid.calculate_ask_vwap_local(str(target_token), safe_s)
                if vwap_ask and vwap_ask <= params.entry_max_price:
                    safe_p, _ = OrderExecutionService.sanitize_order_params(vwap_ask, params.amount)

            # [波动率自适应做 T 周期] 联动 K 线波幅初始化动态脱手窗口
            from polymarket.kline_analyzer import get_asset_status
            from polymarket.config import ASSET_CHOP_THRESHOLDS, CRYPTO_CHOP_MAX_AMPLITUDE
            asset_status = get_asset_status(asset_type)
            asset_amp = float(asset_status.get("amplitude", 0.0))
            max_amp = float(ASSET_CHOP_THRESHOLDS.get(asset_type.upper(), {}).get("max_amplitude", CRYPTO_CHOP_MAX_AMPLITUDE))
            ctx.dynamic_flip_timeout = PricingEngine.calculate_adaptive_flip_duration(
                base_duration=params.flip_timeout_sec, asset_amplitude=asset_amp, max_amplitude_threshold=max_amp
            )

            # 若为极度超跌单，标记退场阶段并适度压缩做 T 衰减基准
            if "[极度超跌做T达标]" in opp_reason:
                ctx.dynamic_flip_timeout = min(ctx.dynamic_flip_timeout or 35.0, 20.0)

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
                    if pos:
                        ctx.leg1 = pos
                    ctx.leg1_filled_time = time.time()
                    deps.set_trade(market_id, ctx.to_dict())
                    fsm.transition_to(TradeState.LEG1_ONLY)
                    
                    # [极速直通通道] 首腿成交后就地触发挂出二腿，抢占盘口队列优先位 (<5ms)
                    from polymarket.services.handlers.leg1_only_handler import Leg1OnlyTickHandler
                    await Leg1OnlyTickHandler().handle(market, fsm, ctx, tick, params, deps, filter_logger)
                else:
                    fsm.transition_to(TradeState.FAILED, reason="首腿成交确认超时")
            else:
                deps.risk_manager.release_market_lock(params.strategy_id, market_id, is_live=params.is_live)
