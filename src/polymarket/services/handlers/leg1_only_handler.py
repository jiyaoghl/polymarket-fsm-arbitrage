import time
from typing import Dict, Any

from polymarket.domain.fsm import TradeFSM, TradeState
from polymarket.domain.models import TradeContext
from polymarket.services.pricing import PricingEngine
from polymarket.services.execution import OrderExecutionService
from polymarket.services.handlers.base import BaseTickHandler
from polymarket.services.handlers.context import StrategyParams, StrategyDependencies, TickBundle, TickFilterLogger

class Leg1OnlyTickHandler(BaseTickHandler):
    """
    LEG1_ONLY 状态处理器 (Leg1 Only State Handler)。
    
    职责：
    1. 模式 A (dual_exit): OCO 双挂并发挂出做 T 卖单与配对买单；
    2. 模式 B (smart_flip): 基于时间衰减目标利润率挂出 GTC 限价卖单；
    3. 模式 C (pair_only / hedge_fallback): 动态核算配对买入保利价格并下发二腿买单。
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
        now_ts = tick.now_ts
        elapsed_since_leg1 = now_ts - (ctx.leg1_filled_time or now_ts)
        
        yes_token = tick.yes_token
        no_token = tick.no_token
        best_ask_yes = tick.best_ask_yes
        best_bid_yes = tick.best_bid_yes
        best_ask_no = tick.best_ask_no
        best_bid_no = tick.best_bid_no

        is_leg1_yes = (str(leg1.token) == str(yes_token))
        opp_token = no_token if is_leg1_yes else yes_token
        opp_bid = best_bid_no if is_leg1_yes else best_bid_yes

        # 动态自适应做 T 周期 (由 K 线波动率自适应初始化)
        flip_timeout = float(ctx.dynamic_flip_timeout or params.flip_timeout_sec or 35.0)

        # ── 模式 A: OCO 双出口同时并发挂单 (Dual Exit) ──
        if params.exit_mode == "dual_exit":
            ctx.exit_stage = "dual_active"
            sell_price = PricingEngine.calculate_flip_sell_price(
                leg1_cost=leg1.cost, elapsed_seconds=elapsed_since_leg1,
                initial_margin=params.initial_margin, min_margin=params.breakeven_margin,
                decay_duration=flip_timeout, leg1_is_taker=(params.leg1_order_type == "FOK")
            )
            pair_price = PricingEngine.calculate_hedged_pair_price(
                leg1_cost=leg1.cost, elapsed_seconds=elapsed_since_leg1,
                initial_margin=params.initial_margin, min_margin=params.breakeven_margin,
                decay_duration=flip_timeout, leg1_is_taker=(params.leg1_order_type == "FOK")
            )
            safe_p_sell, _ = OrderExecutionService.sanitize_order_params(sell_price, sell_price * leg1.size)
            safe_p_pair, safe_s_pair = OrderExecutionService.sanitize_order_params(pair_price, pair_price * leg1.size)
            
            # 申请买单额度
            lock_amount = round(safe_p_pair * safe_s_pair, 2)
            if deps.risk_manager.acquire_trade_lock(params.strategy_id, market_id, lock_amount, is_live=params.is_live):
                order_sell = {"token_id": str(leg1.token), "price": safe_p_sell, "size": leg1.size, "side": "SELL", "order_type": "GTC"}
                order_buy = {"token_id": str(opp_token), "price": safe_p_pair, "size": safe_s_pair, "side": "BUY", "order_type": "GTC"}
                batch_res = await deps.client.post_batch_orders_async([order_sell, order_buy])
                if batch_res and batch_res.get("status") != "ERROR":
                    orders = batch_res.get("orders", [])
                    ctx.dual_orders = orders
                    deps.set_trade(market_id, ctx.to_dict())
                    fsm.transition_to(TradeState.PENDING_LEG2, orders=orders)
                else:
                    deps.risk_manager.release_trade_lock(params.strategy_id, market_id, lock_amount, is_live=params.is_live)
            return

        # ── 模式 B: 智能做 T 高抛 (Smart Flip) ──
        if params.exit_mode == "smart_flip" and elapsed_since_leg1 <= flip_timeout:
            ctx.exit_stage = "flip_active"
            sell_price = PricingEngine.calculate_flip_sell_price(
                leg1_cost=leg1.cost,
                elapsed_seconds=elapsed_since_leg1,
                initial_margin=params.initial_margin,
                min_margin=params.breakeven_margin,
                decay_duration=flip_timeout,
                leg1_is_taker=(params.leg1_order_type == "FOK")
            )
            safe_p, safe_s = OrderExecutionService.sanitize_order_params(sell_price, sell_price * leg1.size)
            
            # 发送同向限价卖单 (GTC SELL)
            order = await deps.client.post_order_async(str(leg1.token), safe_p, leg1.size, "SELL", "GTC")
            if order and order.get("status") not in ("ERROR", None):
                fsm.transition_to(TradeState.PENDING_LEG2, order_info=order)
                ctx.leg2_order_id = order.get("orderID") or order.get("order_id")
                deps.set_trade(market_id, ctx.to_dict())
            return

        # ── 模式 C: 反向配对对冲 (Pair Hedging) ──
        ctx.exit_stage = "hedge_fallback"

        # 动态计算二腿买入配对价
        target_leg2_price = PricingEngine.calculate_hedged_pair_price(
            leg1_cost=leg1.cost,
            elapsed_seconds=max(0.0, elapsed_since_leg1 - flip_timeout),
            initial_margin=params.initial_margin,
            min_margin=params.breakeven_margin,
            decay_duration=30.0,
            leg1_is_taker=(params.leg1_order_type == "FOK")
        )
        
        # 若盘口买一价比理论对冲价更优，则优先使用盘口买一价
        if opp_bid and opp_bid < target_leg2_price:
            target_leg2_price = opp_bid

        # 对冲盈利数学校验
        is_prof, net_ev, p_msg = PricingEngine.verify_hedged_profitability(
            leg1_cost=leg1.cost, leg1_size=leg1.size,
            leg2_cost=target_leg2_price, leg2_size=leg1.size,
            leg1_order_type=params.leg1_order_type, leg2_order_type=params.leg2_order_type
        )
        if is_prof:
            safe_p, safe_s = OrderExecutionService.sanitize_order_params(target_leg2_price, target_leg2_price * leg1.size)
            # 申请二腿额度
            if deps.risk_manager.acquire_trade_lock(params.strategy_id, market_id, round(safe_p * safe_s, 2), is_live=params.is_live):
                order = await deps.client.post_order_async(str(opp_token), safe_p, safe_s, "BUY", params.leg2_order_type)
                if order and order.get("status") not in ("ERROR", None):
                    fsm.transition_to(TradeState.PENDING_LEG2, order_info=order)
                else:
                    deps.risk_manager.release_trade_lock(params.strategy_id, market_id, round(safe_p * safe_s, 2), is_live=params.is_live)
