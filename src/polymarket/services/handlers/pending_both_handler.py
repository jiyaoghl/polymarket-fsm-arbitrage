import time
from typing import Dict, Any

from polymarket.logger import logger
from polymarket.domain.fsm import TradeFSM, TradeState
from polymarket.domain.models import TradeContext, LegPosition
from polymarket.services.pricing import PricingEngine
from polymarket.services.execution import OrderExecutionService
from polymarket.services.handlers.base import BaseTickHandler
from polymarket.services.handlers.context import StrategyParams, StrategyDependencies, TickBundle, TickFilterLogger

class PendingBothLegsTickHandler(BaseTickHandler):
    """
    PENDING_BOTH_LEGS 状态处理器 (Pending Both Legs Handler)。
    
    职责：
    1. 判定 Dual-GTC 双挂单成交状态（实盘 WS / 模拟盘盘口匹配）；
    2. 双边全量成交 -> 瞬间锁定无风险对冲，流转至 LOCKED；
    3. 单边成交（YES 或 NO 率先成交） -> 挂单转为二腿并启动自适应 TTL 计时，流转至 PENDING_LEG2；
    4. 临期未成交 -> 原子撤销双边挂单，释放资金锁，流转至 FAILED。
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
        orders = ctx.dual_orders
        if not orders or len(orders) < 2:
            return

        yes_token = tick.yes_token
        no_token = tick.no_token
        best_ask_yes = tick.best_ask_yes
        best_bid_yes = tick.best_bid_yes
        best_ask_no = tick.best_ask_no
        best_bid_no = tick.best_bid_no
        now_ts = tick.now_ts

        # 提取 YES 与 NO 挂单详情 (优先按 Token 匹配，缺失时严格按双挂索引分配，杜绝双腿误绑同一订单)
        yes_order_info = next((o for o in orders if str(o.get("token_id") or o.get("token")) == str(yes_token)), None)
        no_order_info = next((o for o in orders if str(o.get("token_id") or o.get("token")) == str(no_token)), None)

        if yes_order_info is None and len(orders) >= 1:
            yes_order_info = orders[0]
        if no_order_info is None:
            no_order_info = orders[1] if len(orders) >= 2 else orders[0]

        yes_price = float(yes_order_info.get("price") or 0.0)
        yes_size = float(yes_order_info.get("size") or yes_order_info.get("amount") or 0.0)
        yes_id = yes_order_info.get("order_id") or yes_order_info.get("orderID")

        no_price = float(no_order_info.get("price") or 0.0)
        no_size = float(no_order_info.get("size") or no_order_info.get("amount") or 0.0)
        no_id = no_order_info.get("order_id") or no_order_info.get("orderID")

        # 判定 YES 挂买单成交情况 (只有卖盘打穿至挂买价时才成交)
        yes_filled = False
        if not params.is_live:
            yes_filled = (best_ask_yes is not None and best_ask_yes <= yes_price)
        elif yes_id:
            yes_filled, _ = await OrderExecutionService.async_reconcile_phantom_fill(
                deps.client, yes_id, str(yes_token), yes_size, params.strategy_id, timeout=2.0
            )

        # 判定 NO 挂买单成交情况 (只有卖盘打穿至挂买价时才成交)
        no_filled = False
        if not params.is_live:
            no_filled = (best_ask_no is not None and best_ask_no <= no_price)
        elif no_id:
            no_filled, _ = await OrderExecutionService.async_reconcile_phantom_fill(
                deps.client, no_id, str(no_token), no_size, params.strategy_id, timeout=2.0
            )

        # 分支 A: 双腿均被吃单，达成无风险对冲 (秒级锁定)
        if yes_filled and no_filled:
            ctx.leg1 = LegPosition(token=str(yes_token), side="BUY", cost=yes_price, size=yes_size, order_id=yes_id or "sim_yes")
            ctx.leg2 = LegPosition(token=str(no_token), side="BUY", cost=no_price, size=no_size, order_id=no_id or "sim_no")
            ctx.leg1_dir = "YES"
            ctx.leg2_dir = "NO"

            gross_ev, fee, net_ev = PricingEngine.calculate_net_ev(
                leg1_cost=yes_price, leg1_size=yes_size,
                leg2_cost=no_price, leg2_size=no_size,
                leg1_order_type="GTC", leg2_order_type="GTC"
            )
            ctx.profit_usdc = net_ev
            ctx.realized_pnl = net_ev
            ctx.gross_profit_usdc = gross_ev
            ctx.fee_usdc = fee
            ctx.settlement_type = "HEDGED_LOCKED"
            deps.set_trade(market_id, ctx.to_dict())
            fsm.transition_to(TradeState.LOCKED)
            return

        # 分支 B: YES 率先成交，立即撤销 NO 挂单并流转至 LEG1_ONLY 触发自适应做 T 与配对 OCO
        if yes_filled and not no_filled:
            now_time = time.time()
            if no_id:
                await deps.client.cancel_order_async(no_id)
            ctx.leg1 = LegPosition(token=str(yes_token), side="BUY", cost=yes_price, size=yes_size, order_id=yes_id or "sim_yes")
            ctx.leg2 = None
            ctx.leg1_dir = "YES"
            ctx.leg2_dir = None
            ctx.leg1_filled_time = now_time
            ctx.dual_orders = []
            deps.set_trade(market_id, ctx.to_dict())
            logger.info(f"[策略FSM：{params.strategy_id}] 双挂单 YES 侧率先成交 (price={yes_price:.4f}, size={yes_size:.2f})，已撤销 NO 挂单，流转至 LEG1_ONLY 触发 OCO 双向自适应变现。")
            fsm.transition_to(TradeState.LEG1_ONLY)
            return

        # 分支 C: NO 率先成交，立即撤销 YES 挂单并流转至 LEG1_ONLY 触发自适应做 T 与配对 OCO
        if no_filled and not yes_filled:
            now_time = time.time()
            if yes_id:
                await deps.client.cancel_order_async(yes_id)
            ctx.leg1 = LegPosition(token=str(no_token), side="BUY", cost=no_price, size=no_size, order_id=no_id or "sim_no")
            ctx.leg2 = None
            ctx.leg1_dir = "NO"
            ctx.leg2_dir = None
            ctx.leg1_filled_time = now_time
            ctx.dual_orders = []
            deps.set_trade(market_id, ctx.to_dict())
            logger.info(f"[策略FSM：{params.strategy_id}] 双挂单 NO 侧率先成交 (price={no_price:.4f}, size={no_size:.2f})，已撤销 YES 挂单，流转至 LEG1_ONLY 触发 OCO 双向自适应变现。")
            fsm.transition_to(TradeState.LEG1_ONLY)
            return

        # 分支 D: 双边均未成交且临近到期，原子撤单安全退出
        time_to_expiry = ctx.end_time - now_ts if ctx.end_time > 0 else 999.0
        if ctx.end_time > 0 and time_to_expiry <= max(30.0, params.min_time_to_expiry_entry):
            logger.info(f"[策略FSM：{params.strategy_id}] 双挂单临近交割未成交 (剩余 {time_to_expiry:.1f}s)，执行原子撤单安全退出。")
            if yes_id:
                await deps.client.cancel_order_async(yes_id)
            if no_id:
                await deps.client.cancel_order_async(no_id)
            deps.risk_manager.release_market_lock(params.strategy_id, market_id, is_live=params.is_live)
            fsm.transition_to(TradeState.FAILED, reason=f"双挂单临近到期未成交，撤单退出 (剩余 {time_to_expiry:.1f}s)")
            return

        # 分支 E: 双边均未成交 -> 执行自适应价差防抖的双挂动态智能跟单 (Dual-Bracket Re-peg)
        if not yes_filled and not no_filled:
            last_repeg = float(getattr(ctx, "last_reprice_time", None) or (now_ts - 10.0))
            spread_yes = max(0.0, (best_ask_yes - best_bid_yes)) if (best_ask_yes is not None and best_bid_yes is not None) else 0.010
            spread_no = max(0.0, (best_ask_no - best_bid_no)) if (best_ask_no is not None and best_bid_no is not None) else 0.010
            min_spread = min(spread_yes, spread_no)

            from polymarket.services.pegging import MakerPeggingService
            adaptive_delay, _, _ = MakerPeggingService.calculate_adaptive_pegging_params(min_spread)

            if (now_ts - last_repeg) >= adaptive_delay:
                should_repeg, new_yes_p, new_no_p, repeg_reason = MakerPeggingService.calculate_dual_bracket_repeg_prices(
                    current_yes_price=yes_price,
                    current_no_price=no_price,
                    best_bid_yes=best_bid_yes,
                    best_bid_no=best_bid_no,
                    best_ask_yes=best_ask_yes,
                    best_ask_no=best_ask_no,
                    entry_max_price=params.entry_max_price,
                    entry_min_price=params.entry_min_price,
                    min_profit_margin=params.initial_margin or 0.020,
                    anti_penny_step=0.001
                )
                if should_repeg:
                    safe_yes_p, safe_yes_size = OrderExecutionService.sanitize_order_params(new_yes_p, params.amount)
                    safe_no_p, safe_no_size = OrderExecutionService.sanitize_order_params(new_no_p, params.amount)

                    if params.is_live:
                        try:
                            # 实盘：原子取消旧双挂单并重新批量下发
                            if yes_id:
                                await deps.client.cancel_order_async(yes_id)
                            if no_id:
                                await deps.client.cancel_order_async(no_id)

                            order_yes = {"token_id": str(yes_token), "price": safe_yes_p, "size": safe_yes_size, "side": "BUY", "order_type": "GTC"}
                            order_no = {"token_id": str(no_token), "price": safe_no_p, "size": safe_no_size, "side": "BUY", "order_type": "GTC"}
                            batch_res = await deps.client.post_batch_orders_async([order_yes, order_no])

                            if batch_res and batch_res.get("status") != "ERROR":
                                ctx.dual_orders = batch_res.get("orders", [])
                                ctx.last_reprice_time = now_ts
                                ctx.record_reprice(yes_price, safe_yes_p, reason=repeg_reason, token=str(yes_token), timestamp=now_ts)
                                ctx.record_reprice(no_price, safe_no_p, reason=repeg_reason, token=str(no_token), timestamp=now_ts)
                                deps.set_trade(market_id, ctx.to_dict())
                                logger.info(f"[策略FSM：{params.strategy_id}] 实盘双挂跟价成功: {repeg_reason}")
                            else:
                                # 批量改单异常，启动原价保底重发
                                logger.critical(f"[{params.strategy_id}] 实盘双挂改单失败，启动保底重新挂单 @ ({yes_price}, {no_price})")
                                fallback_batch = await deps.client.post_batch_orders_async([
                                    {"token_id": str(yes_token), "price": yes_price, "size": yes_size, "side": "BUY", "order_type": "GTC"},
                                    {"token_id": str(no_token), "price": no_price, "size": no_size, "side": "BUY", "order_type": "GTC"}
                                ])
                                if fallback_batch:
                                    ctx.dual_orders = fallback_batch.get("orders", [])
                        except Exception as ex:
                            logger.error(f"[{params.strategy_id}] 实盘双挂跟价改单异常: {ex}")
                    else:
                        # 模拟盘 (PAPER)：直接平滑更新订单价格与重定价记录
                        yes_order_info["price"] = safe_yes_p
                        no_order_info["price"] = safe_no_p
                        ctx.last_reprice_time = now_ts
                        ctx.record_reprice(yes_price, safe_yes_p, reason=repeg_reason, token=str(yes_token), timestamp=now_ts)
                        ctx.record_reprice(no_price, safe_no_p, reason=repeg_reason, token=str(no_token), timestamp=now_ts)
                        deps.set_trade(market_id, ctx.to_dict())
                        logger.info(f"[策略FSM：{params.strategy_id}] [模拟盘] {repeg_reason}")
