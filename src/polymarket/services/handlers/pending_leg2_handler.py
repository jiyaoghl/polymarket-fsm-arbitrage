import time
from typing import Dict, Any

from polymarket.domain.fsm import TradeFSM, TradeState
from polymarket.domain.models import TradeContext, LegPosition
from polymarket.services.pricing import PricingEngine
from polymarket.services.pegging import MakerPeggingService
from polymarket.services.execution import OrderExecutionService
from polymarket.services.liquidator import AdaptiveLiquidatorService
from polymarket.services.handlers.base import BaseTickHandler
from polymarket.services.handlers.context import StrategyParams, StrategyDependencies, TickBundle, TickFilterLogger

class PendingLeg2TickHandler(BaseTickHandler):
    """
    PENDING_LEG2 状态处理器 (Pending Leg2 State Handler)。
    
    职责：
    1. OCO (dual_exit) 双单成交裁决：
       - 卖单成交 -> 取消买单并释放额度，流转 SETTLED；
       - 买单成交 -> 取消卖单，锁定无风险对冲，流转 LOCKED；
    2. 单订单出场确认：
       - 做 T 限价卖单成交 -> 核算已实现收益并流转 SETTLED；
       - 配对限价买单成交 -> 达成对冲锁仓并流转 LOCKED。
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
        leg1 = ctx.leg1
        if not leg1:
            return

        market_id = market["id"]
        asset_type = market.get("__asset_type", "Crypto")
        yes_token = tick.yes_token
        no_token = tick.no_token
        best_ask_yes = tick.best_ask_yes
        best_bid_yes = tick.best_bid_yes
        best_ask_no = tick.best_ask_no
        best_bid_no = tick.best_bid_no

        is_leg1_yes = (str(leg1.token) == str(yes_token))
        opp_token = no_token if is_leg1_yes else yes_token

        # ── A. 处理 dual_exit 模式下的 OCO 双单成交 ───────────
        if ctx.dual_orders and len(ctx.dual_orders) >= 2:
            sell_info = next((o for o in ctx.dual_orders if o.get("side") == "SELL"), None)
            buy_info = next((o for o in ctx.dual_orders if o.get("side") == "BUY"), None)
            sell_id = sell_info.get("order_id") or sell_info.get("orderID") if sell_info else None
            buy_id = buy_info.get("order_id") or buy_info.get("orderID") if buy_info else None

            # 检查卖单是否成交
            sell_filled = False
            if sell_info:
                sell_target_price = float(sell_info.get("price", 0.0))
                cur_bid = best_bid_yes if is_leg1_yes else best_bid_no
                if not params.is_live:
                    # 模拟盘：只有当买一价达到或超过做T卖单挂单价时才判定成交
                    sell_filled = (cur_bid is not None and cur_bid >= sell_target_price)
                elif sell_id:
                    sell_filled, _ = await OrderExecutionService.async_reconcile_phantom_fill(
                        deps.client, sell_id, str(leg1.token), leg1.size, params.strategy_id
                    )

            if sell_filled:
                # 卖单成交！立即取消买单并释放额度
                if buy_id:
                    await deps.client.cancel_order_async(buy_id)
                buy_price = float(buy_info.get("price", 0.0)) if buy_info else 0.0
                deps.risk_manager.release_trade_lock(params.strategy_id, market_id, round(buy_price * leg1.size, 2), is_live=params.is_live)
                
                sell_price = float(sell_info.get("price", leg1.cost)) if sell_info else leg1.cost
                realized_pnl, gross_pnl, fee = AdaptiveLiquidatorService.calculate_realized_pnl(
                    leg1_cost=leg1.cost, leg1_size=leg1.size,
                    close_price=sell_price, leg1_is_taker=(params.leg1_order_type == "FOK"), close_is_taker=False
                )
                ctx.profit_usdc = realized_pnl
                ctx.realized_pnl = realized_pnl
                ctx.gross_profit_usdc = gross_pnl
                ctx.fee_usdc = fee
                ctx.settlement_type = "DUAL_EXIT_SELL_SETTLED"
                ctx.leg2 = LegPosition(
                    token=str(leg1.token),
                    side="SELL",
                    cost=sell_price,
                    size=leg1.size,
                    order_id=sell_id or "sim_sell"
                )
                deps.set_trade(market_id, ctx.to_dict())

                from polymarket.metrics import metrics
                hold_sec = max(0.0, time.time() - float(ctx.leg1_filled_time or time.time()))
                metrics.unhedged_duration_seconds.observe(hold_sec)
                metrics.dual_exit_sells_total.inc()

                from polymarket.services.notifier import DiscordNotifier
                DiscordNotifier.get_instance().notify_flip_success(
                    market_id=market_id,
                    asset=ctx.asset or asset_type,
                    strategy_name=params.strategy_id,
                    leg1_cost=leg1.cost,
                    sell_price=sell_price,
                    shares=leg1.size,
                    hold_seconds=hold_sec,
                    net_profit=realized_pnl,
                    gross_profit=gross_pnl,
                    fee_usdc=fee,
                    is_live=params.is_live
                )

                fsm.transition_to(TradeState.SETTLED, reason=f"OCO 做T卖出率先成交变现，净锁定 ${realized_pnl:.4f}")
                return

            # 检查买单是否成交
            buy_filled = False
            if buy_info:
                buy_target_price = float(buy_info.get("price", 0.0))
                cur_ask = best_ask_no if is_leg1_yes else best_ask_yes
                if not params.is_live:
                    # 模拟盘：只有当卖一价达到或低于对冲买单挂单价时才判定成交
                    buy_filled = (cur_ask is not None and cur_ask <= buy_target_price)
                elif buy_id:
                    buy_filled, _ = await OrderExecutionService.async_reconcile_phantom_fill(
                        deps.client, buy_id, str(opp_token), leg1.size, params.strategy_id
                    )

            if buy_filled:
                # 买单成交！立即取消卖单
                if sell_id:
                    await deps.client.cancel_order_async(sell_id)
                buy_price = float(buy_info.get("price", 0.5)) if buy_info else 0.5
                ctx.leg2 = LegPosition(
                    token=str(opp_token),
                    side="BUY",
                    cost=buy_price,
                    size=leg1.size,
                    order_id=buy_id or "sim_leg2"
                )

                # 精确核算对冲锁仓真实 Net EV 损益
                gross_ev, fee, net_ev = PricingEngine.calculate_net_ev(
                    leg1_cost=leg1.cost, leg1_size=leg1.size,
                    leg2_cost=buy_price, leg2_size=leg1.size,
                    leg1_order_type=params.leg1_order_type, leg2_order_type="GTC"
                )
                ctx.profit_usdc = net_ev
                ctx.realized_pnl = net_ev
                ctx.gross_profit_usdc = gross_ev
                ctx.fee_usdc = fee
                ctx.settlement_type = "HEDGED_LOCKED"

                from polymarket.metrics import metrics
                hold_sec = max(0.0, time.time() - float(ctx.leg1_filled_time or time.time()))
                metrics.unhedged_duration_seconds.observe(hold_sec)
                metrics.trades_locked_total.inc()

                deps.set_trade(market_id, ctx.to_dict())
                fsm.transition_to(TradeState.LOCKED)
                return

            # ── A.1 OCO 双单的主动 Anti-Pennying 阶梯跟单与坚守利润 ───────────
            if not sell_filled and not buy_filled and buy_info:
                now_ts = tick.now_ts
                last_reprice = float(getattr(ctx, "last_reprice_time", None) or ctx.leg1_filled_time or now_ts)
                
                cur_opp_bid = best_bid_no if is_leg1_yes else best_bid_yes
                cur_opp_ask = best_ask_no if is_leg1_yes else best_ask_yes
                cur_buy_price = float(buy_info.get("price", 0.0))
                
                opp_spread = max(0.0, (cur_opp_ask - cur_opp_bid)) if (cur_opp_ask is not None and cur_opp_bid is not None) else 0.005
                adaptive_delay, _, _ = MakerPeggingService.calculate_adaptive_pegging_params(opp_spread)
                
                # 依据对侧价差自适应迟滞冷却 (宽价差 >=0.010 时 1.5s 极速抢位，紧凑价差时 3.0s 防抖)
                if now_ts - last_reprice >= adaptive_delay:
                    if cur_opp_bid is not None and cur_buy_price > 0:
                        # 计算保利买入最高上限 (严格扣除首腿与二腿真实手续费，并保留 breakeven_margin 净利安全垫)
                        from polymarket.config import TAKER_FEE_RATE, MAKER_FEE_RATE
                        fee1 = leg1.cost * (TAKER_FEE_RATE if params.leg1_order_type == "FOK" else MAKER_FEE_RATE)
                        fee2 = (1.0 - leg1.cost) * (MAKER_FEE_RATE if params.leg2_order_type != "FOK" else TAKER_FEE_RATE)
                        fee_buffer = fee1 + fee2
                        target_margin = getattr(params, "breakeven_margin", 0.003)
                        max_allowed_buy_price = round(max(0.01, 1.0 - leg1.cost - target_margin - fee_buffer), 4)
                        
                        should_repeg, new_target, reason = MakerPeggingService.calculate_pegged_price(
                            current_best_bid=cur_opp_bid,
                            our_current_price=cur_buy_price,
                            entry_max_price=max_allowed_buy_price,
                            spread=opp_spread
                        )
                        if should_repeg and new_target > cur_buy_price:
                            safe_new_price, _ = OrderExecutionService.sanitize_order_params(new_target, new_target * leg1.size)
                            if safe_new_price > cur_buy_price and safe_new_price <= max_allowed_buy_price:
                                old_p = float(cur_buy_price)
                                if params.is_live and buy_id:
                                    try:
                                        await deps.client.cancel_order_async(buy_id)
                                        new_order = await deps.client.post_order_async(str(opp_token), safe_new_price, leg1.size, "BUY", "GTC")
                                        if new_order and new_order.get("status") not in ("ERROR", None):
                                            buy_info["price"] = safe_new_price
                                            buy_info["order_id"] = new_order.get("orderID") or new_order.get("order_id")
                                            ctx.record_reprice(old_p, safe_new_price, reason=f"MakerPegging: {reason}", token=str(opp_token), timestamp=now_ts)
                                            deps.set_trade(market_id, ctx.to_dict())
                                        else:
                                            # 发新单失败，尝试以原价格紧急补回对冲挂单，防止敞口裸奔
                                            logger.critical(f"[{params.strategy_id}] 实盘追单挂单失败，启动保底重新挂单 @ {old_p}")
                                            fallback = await deps.client.post_order_async(str(opp_token), old_p, leg1.size, "BUY", "GTC")
                                            if fallback and fallback.get("status") not in ("ERROR", None):
                                                buy_info["order_id"] = fallback.get("orderID") or fallback.get("order_id")
                                    except Exception as ex:
                                        logger.error(f"[{params.strategy_id}] 实盘二腿追单改单异常: {ex}")
                                elif not params.is_live:
                                    buy_info["price"] = safe_new_price
                                    ctx.record_reprice(old_p, safe_new_price, reason=f"MakerPegging: {reason}", token=str(opp_token), timestamp=now_ts)
                                    deps.set_trade(market_id, ctx.to_dict())

            return

        # ── B. 处理单订单模式成交 ────────────────────────────
        leg2 = ctx.leg2
        if not leg2 or not ctx.leg2_order_id:
            return

        is_fill = False
        if not params.is_live:
            target_price = leg2.cost
            if leg2.side == "SELL":
                cur_bid = best_bid_yes if is_leg1_yes else best_bid_no
                is_fill = (cur_bid is not None and cur_bid >= target_price)
            else:
                cur_ask = best_ask_no if is_leg1_yes else best_ask_yes
                is_fill = (cur_ask is not None and cur_ask <= target_price)
        else:
            is_fill, _ = await OrderExecutionService.async_reconcile_phantom_fill(
                deps.client, ctx.leg2_order_id, str(leg2.token), leg2.size, params.strategy_id
            )

        if is_fill:
            if leg2.side == "SELL":
                leg1_is_taker = (params.leg1_order_type == "FOK")
                realized_pnl, gross_pnl, fee = AdaptiveLiquidatorService.calculate_realized_pnl(
                    leg1_cost=ctx.leg1.cost, leg1_size=ctx.leg1.size,
                    close_price=leg2.cost, leg1_is_taker=leg1_is_taker, close_is_taker=False
                )
                ctx.profit_usdc = realized_pnl
                ctx.realized_pnl = realized_pnl
                ctx.gross_profit_usdc = gross_pnl
                ctx.fee_usdc = fee
                ctx.settlement_type = "SMART_FLIP_SETTLED"
                deps.set_trade(market_id, ctx.to_dict())

                from polymarket.services.notifier import DiscordNotifier
                hold_sec = time.time() - float(ctx.leg1_filled_time or time.time())
                DiscordNotifier.get_instance().notify_flip_success(
                    market_id=market_id,
                    asset=ctx.asset or asset_type,
                    strategy_name=params.strategy_id,
                    leg1_cost=ctx.leg1.cost,
                    sell_price=leg2.cost,
                    shares=ctx.leg1.size,
                    hold_seconds=max(0.0, hold_sec),
                    net_profit=realized_pnl,
                    gross_profit=gross_pnl,
                    fee_usdc=fee,
                    is_live=params.is_live
                )

                fsm.transition_to(TradeState.SETTLED, reason=f"智能做T高抛成交变现，净锁定 ${realized_pnl:.4f}")
            else:
                gross_ev, fee, net_ev = PricingEngine.calculate_net_ev(
                    leg1_cost=ctx.leg1.cost, leg1_size=ctx.leg1.size,
                    leg2_cost=ctx.leg2.cost, leg2_size=ctx.leg2.size,
                    leg1_order_type=params.leg1_order_type, leg2_order_type=params.leg2_order_type
                )
                ctx.profit_usdc = net_ev
                ctx.realized_pnl = net_ev
                ctx.gross_profit_usdc = gross_ev
                ctx.fee_usdc = fee
                ctx.settlement_type = "HEDGED_LOCKED"
                deps.set_trade(market_id, ctx.to_dict())
                fsm.transition_to(TradeState.LOCKED)
                return

        # ── C. 阶梯式做 T 智能让价与追单保护 (Multi-Level Maker) ──
        # 当订单挂出超过一定时间且交割临近时，按倒计时阶梯微调挂单价，降低强平率
        if not is_fill and leg2.side == "SELL":
            now_ts = tick.now_ts
            last_reprice = float(getattr(ctx, "last_reprice_time", None) or ctx.leg1_filled_time or now_ts)
            time_to_exp = market.get("expiry", 0) - now_ts if market.get("expiry") else 120.0

            if now_ts - last_reprice >= 15.0:
                cur_bid = best_bid_yes if is_leg1_yes else best_bid_no
                if cur_bid is not None and cur_bid > 0:
                    target_reprice = leg2.cost
                    if time_to_exp < 45.0:
                        # 临近交割（<45s）：贴近当前买一价快速出场
                        target_reprice = round(max(cur_bid, leg1.cost * 0.98), 4)
                    elif time_to_exp < 75.0:
                        # 中期阶梯（45~75s）：挂在买一 + 0.002
                        target_reprice = round(max(cur_bid + 0.002, leg1.cost), 4)

                    safe_new_price, _ = OrderExecutionService.sanitize_order_params(target_reprice, target_reprice * leg1.size)
                    if abs(safe_new_price - leg2.cost) >= 0.003 and safe_new_price >= leg1.cost * 0.95:
                        old_p = float(leg2.cost)
                        if params.is_live:
                            try:
                                await deps.client.cancel_order_async(ctx.leg2_order_id)
                                new_order = await deps.client.post_order_async(str(leg2.token), safe_new_price, leg1.size, "SELL", "GTC")
                                if new_order and new_order.get("status") not in ("ERROR", None):
                                    ctx.leg2.cost = safe_new_price
                                    ctx.leg2_order_id = new_order.get("orderID") or new_order.get("order_id")
                                    ctx.record_reprice(old_p, safe_new_price, reason=f"LadderReprice (TTL: {time_to_exp:.0f}s)", token=str(leg2.token), timestamp=now_ts)
                                    deps.set_trade(market_id, ctx.to_dict())
                                else:
                                    logger.warning(f"[{params.strategy_id}] 做T改单挂单失败，尝试保底重发 @ {old_p}")
                                    fallback = await deps.client.post_order_async(str(leg2.token), old_p, leg1.size, "SELL", "GTC")
                                    if fallback and fallback.get("status") not in ("ERROR", None):
                                        ctx.leg2_order_id = fallback.get("orderID") or fallback.get("order_id")
                            except Exception as ex:
                                logger.error(f"[{params.strategy_id}] 做T阶梯改单异常: {ex}")
                        else:
                            ctx.leg2.cost = safe_new_price
                            ctx.record_reprice(old_p, safe_new_price, reason=f"LadderReprice (TTL: {time_to_exp:.0f}s)", token=str(leg2.token), timestamp=now_ts)
                            deps.set_trade(market_id, ctx.to_dict())
