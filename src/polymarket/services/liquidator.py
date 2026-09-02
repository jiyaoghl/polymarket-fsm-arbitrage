import time
from typing import Dict, Any, Optional, Tuple

from polymarket.logger import logger
from polymarket.client import PolyClient
from polymarket.domain.models import TradeContext, LegPosition
from polymarket.services.execution import OrderExecutionService

class AdaptiveLiquidatorService:
    """
    动态自适应强平与止损服务 (Adaptive Liquidation Service)。
    
    核心机制：
    1. 动态自适应 TTL 计算：基础 90s，联动 BTC/ETH 波动率压缩至 35~60s，临期截断，单调递减。
    2. 基于绝对时间戳 (leg1_filled_time) 判定超时，杜绝重启后时间基准漂移。
    3. FOK + GTC @ 0.99 双重兜底强平，确保单边敞口彻底离场。
    """

    @staticmethod
    def calculate_adaptive_ttl(
        base_ttl: float,
        time_to_expiry: float,
        asset_amplitude_pct: float = 0.0,
        asset_amplitude_threshold: float = 0.30,
        current_dynamic_ttl: Optional[float] = None
    ) -> float:
        """
        计算当前时刻适用的动态 TTL (秒)。
        保证单调递减 (Monotonic Decay)，持仓期间只允许变短，绝不反向延长。
        """
        target_ttl = base_ttl

        # 1. 高波动率联动收紧：当振幅接近或超过阈值 70% 时，线性压缩 TTL
        if asset_amplitude_threshold > 0 and asset_amplitude_pct > 0:
            ratio = asset_amplitude_pct / asset_amplitude_threshold
            if ratio >= 0.7:
                # ratio 在 [0.7, 1.0] 时，将 TTL 压缩至 [60s, 35s]
                compression = min(max((ratio - 0.7) / 0.3, 0.0), 1.0)
                target_ttl = 60.0 - (compression * 25.0)  # 60s -> 35s

        # 2. 临期截断：距离到期交割不足 60s 时，强制在交割前 10s 离场
        if 0 < time_to_expiry < 60.0:
            target_ttl = min(target_ttl, max(15.0, time_to_expiry - 10.0))

        # 3. 单调递减防抖动：持仓期间 TTL 只能收紧，绝不延长
        if current_dynamic_ttl is not None:
            return round(min(current_dynamic_ttl, target_ttl), 1)

        return round(target_ttl, 1)

    @staticmethod
    def evaluate_timeout(
        context: TradeContext,
        base_ttl: float = 90.0,
        asset_amplitude_pct: float = 0.0,
        asset_amplitude_threshold: float = 0.30
    ) -> Tuple[bool, float, float]:
        """
        评估当前持仓是否已超过动态自适应 TTL。
        以 context.leg1_filled_time 为绝对时间基准。
        
        Returns:
            (is_timed_out, elapsed_seconds, current_ttl)
        """
        now = time.time()
        
        # 若尚未成交首腿，不存在单边敞口
        if not context.leg1_filled_time:
            return False, 0.0, base_ttl

        elapsed = now - context.leg1_filled_time
        time_to_expiry = max(0.0, context.end_time - now) if context.end_time > 0 else 999.0

        # 计算并更新 context 中的单调递减 dynamic_ttl
        current_ttl = AdaptiveLiquidatorService.calculate_adaptive_ttl(
            base_ttl=base_ttl,
            time_to_expiry=time_to_expiry,
            asset_amplitude_pct=asset_amplitude_pct,
            asset_amplitude_threshold=asset_amplitude_threshold,
            current_dynamic_ttl=context.dynamic_ttl
        )
        context.dynamic_ttl = current_ttl

        is_timed_out = elapsed >= current_ttl
        return is_timed_out, elapsed, current_ttl

    @staticmethod
    def calculate_realized_pnl(
        leg1_cost: float,
        leg1_size: float,
        close_price: float,
        leg1_is_taker: bool = True,
        close_is_taker: bool = True
    ) -> Tuple[float, float, float]:
        """
        计算市价平仓后的已实现盈亏 (Realized PnL)。
        
        Returns:
            (realized_pnl, gross_pnl, total_fee)
        """
        from polymarket.services.pricing import PricingEngine
        buy_notional = leg1_cost * leg1_size
        sell_notional = close_price * leg1_size
        gross_pnl = sell_notional - buy_notional

        fee_buy = PricingEngine.calculate_parabolic_fee(leg1_cost, leg1_size) if leg1_is_taker else 0.0
        fee_sell = PricingEngine.calculate_parabolic_fee(close_price, leg1_size) if close_is_taker else 0.0

        total_fee = round(fee_buy + fee_sell, 4)
        realized_pnl = round(gross_pnl - total_fee, 4)
        return realized_pnl, round(gross_pnl, 4), total_fee

    @staticmethod
    def calculate_expiry_settled_pnl(
        leg1_cost: float,
        leg1_size: float,
        settlement_price: float,
        leg1_is_taker: bool = True
    ) -> Tuple[float, float, float]:
        """
        计算市价平仓失败直至到期后的最终交割结算盈亏 (Settled PnL)。
        Polymarket 到期交割领奖无卖出手续费。
        
        Returns:
            (settled_pnl, gross_pnl, entry_fee)
        """
        from polymarket.services.pricing import PricingEngine
        buy_notional = leg1_cost * leg1_size
        settled_revenue = settlement_price * leg1_size
        gross_pnl = settled_revenue - buy_notional

        entry_fee = PricingEngine.calculate_parabolic_fee(leg1_cost, leg1_size) if leg1_is_taker else 0.0

        settled_pnl = round(gross_pnl - entry_fee, 4)
        return settled_pnl, round(gross_pnl, 4), entry_fee

    @staticmethod
    def execute_force_close(
        client: PolyClient,
        context: TradeContext,
        strategy_id: str = "default",
        allow_grace: bool = False
    ) -> Tuple[bool, Optional[float], float, Optional[str]]:
        """
        执行强平平仓动作：
        1. 先撤销二腿挂单 (若存在)
        2. 穿透买盘深度计算 VWAP 加权均价
        3. 发送首腿市价 FOK 平仓
        4. 若 FOK 快速确认未成交，立即以 GTC @ 0.01 挂单紧急兜底
        
        Returns:
            (success, close_price_or_none, size, close_order_id_or_none)
        """
        from polymarket.services.pricing import PricingEngine
        market_id = context.market_id
        logger.warning(f"[强平引擎：{strategy_id}] 触发动态 TTL 超时强平！市场: {market_id}")

        # 1. 撤销所有在途二腿与 OCO 挂单 (防止平仓后原买单被动成交产生孤儿仓位)
        orders_to_cancel = set()
        if context.leg2_order_id:
            orders_to_cancel.add(str(context.leg2_order_id))
        if context.dual_orders:
            for o in context.dual_orders:
                oid = o.get("order_id") or o.get("orderID")
                if oid:
                    orders_to_cancel.add(str(oid))

        for oid in orders_to_cancel:
            try:
                client.cancel_order(oid)
                logger.info(f"[强平引擎：{strategy_id}] 已撤销在途挂单: {oid}")
            except Exception as e:
                logger.warning(f"[强平引擎：{strategy_id}] 撤销在途挂单 {oid} 异常 (不阻断平仓): {e}")

        # 2. 获取首腿持仓明细
        leg1 = context.leg1
        if not leg1 or not leg1.token or leg1.size <= 0:
            logger.error(f"[强平引擎：{strategy_id}] 首腿持仓数据缺失，无法平仓: {leg1}")
            return False, None, 0.0, None

        token_id = leg1.token
        size = leg1.size
        # 首腿是 BUY，平仓方向必须为 SELL
        close_side = "SELL" if leg1.side == "BUY" else "BUY"

        # 3. 穿透订单簿买盘深度计算真实 VWAP 与边际价格 (优先使用本地 OrderbookMemoryGrid 0 网络 I/O，缺失时降级 REST)
        from polymarket.services.grid import OrderbookMemoryGrid
        vwap_price = None
        marginal_price = None
        best_price = 0.01

        # 3.1 尝试本地内存网格 (严格 5.0s 时效限制，耗时 <0.05ms)
        try:
            local_vwap, local_marginal, local_filled = OrderbookMemoryGrid.get_instance().calculate_bid_vwap_and_marginal_local(
                token_id, size, max_staleness=5.0
            )
            if local_vwap:
                vwap_price = local_vwap
                marginal_price = local_marginal
                best_price = local_vwap
                logger.info(f"[强平引擎：{strategy_id}] 基于本地共享盘口网格 (0 网络 I/O) 算出平仓 VWAP: {vwap_price}, 边际价: {marginal_price}")
        except Exception as e:
            logger.debug(f"[强平引擎：{strategy_id}] 本地网格穿透异常: {e}")

        # 3.2 本地未命中或陈旧时安全降级至 REST API
        if not vwap_price:
            try:
                orderbook = client.get_orderbook(token_id)
                if orderbook and orderbook.get("bids"):
                    vwap_price, marginal_price, _ = PricingEngine.calculate_bid_vwap_and_marginal(
                        orderbook.get("bids", []), size
                    )
                
                if vwap_price:
                    best_price = vwap_price
                    logger.info(f"[强平引擎：{strategy_id}] 基于 REST 订单簿深度算出平仓 VWAP: {vwap_price}, 边际价: {marginal_price}")
                else:
                    price_info = client.get_market_price(token_id)
                    best_price = float(price_info.get("bid", 0.01) if close_side == "SELL" else price_info.get("ask", 0.99))
                    marginal_price = best_price
            except Exception as e:
                logger.warning(f"[强平引擎：{strategy_id}] 拉取深度计算 VWAP 异常，使用保底盘口: {e}")
                price_info = client.get_market_price(token_id)
                best_price = float(price_info.get("bid", 0.01) if close_side == "SELL" else price_info.get("ask", 0.99))
                marginal_price = best_price

        # 3.3 [弹性缓冲保护] 若买盘穿透估算亏损 > 5% 且未曾给予过缓冲，给予一次 10s 均值回归缓冲
        time_to_exp = max(0.0, context.end_time - time.time()) if context.end_time > 0 else 999.0
        if allow_grace and vwap_price and leg1.cost > 0 and (vwap_price < leg1.cost * 0.95):
            if not getattr(context, "ttl_grace_extended", False) and time_to_exp >= 25.0:
                context.ttl_grace_extended = True
                context.dynamic_ttl = (context.dynamic_ttl or 90.0) + 10.0
                logger.warning(
                    f"[强平引擎：{strategy_id}] 买盘 VWAP 穿透亏损较大 (估算价 {vwap_price:.4f} < 成本 {leg1.cost:.4f} * 0.95)，"
                    f"给予一次性 10s 均值回归弹性缓冲 (剩余到期 {time_to_exp:.1f}s)"
                )
                return False, None, size, None

        # 4. 尝试市价 FOK 平仓 (基于边际价 P_marginal - 0.002 精确保护发单，确保 100% 一次性吃满)
        ref_price = marginal_price if marginal_price is not None else best_price
        safe_price = round(max(float(ref_price) - 0.002, 0.001), 4) if close_side == "SELL" else round(min(float(ref_price) + 0.002, 0.999), 4)
        
        try:
            fok_order = client.post_order(token_id, safe_price, size, close_side, "FOK")
            if fok_order and fok_order.get("status") not in ("ERROR", None):
                # 模拟盘强制 100% 严格使用加权 VWAP 均价记账，实盘取撮合成交价
                actual_price = float(vwap_price or fok_order.get("price") or safe_price)
                order_id = str(fok_order.get("orderID") or fok_order.get("order_id") or "fok_close")
                logger.info(f"[强平引擎：{strategy_id}] 市价 FOK 平仓单发送成功: {order_id}, 最终成交价(VWAP): {actual_price} (保护价: {safe_price})")
                
                from polymarket.metrics import metrics
                metrics.liquidations_total.inc()
                if context.leg1_filled_time:
                    hold_sec = max(0.0, time.time() - float(context.leg1_filled_time))
                    metrics.unhedged_duration_seconds.observe(hold_sec)
                
                return True, actual_price, size, order_id
        except Exception as e:
            logger.warning(f"[强平引擎：{strategy_id}] 发送市价 FOK 平仓失败: {e}")

        # 5. GTC 紧急挂单兜底 (以极端让价 0.01 确保被撮合)
        emergency_price = 0.001 if close_side == "SELL" else 0.999
        logger.warning(f"[强平引擎：{strategy_id}] FOK 未能即时成交，启动 GTC @ {emergency_price} 紧急挂单兜底！")
        try:
            gtc_order = client.post_order(token_id, emergency_price, size, close_side, "GTC")
            if gtc_order and gtc_order.get("status") not in ("ERROR", None):
                order_id = str(gtc_order.get("orderID") or gtc_order.get("order_id") or "gtc_close")
                logger.info(f"[强平引擎：{strategy_id}] 紧急 GTC 兜底单已挂出: {order_id}, 兜底价: {emergency_price}")
                return True, emergency_price, size, order_id
        except Exception as e:
            logger.critical(f"[强平引擎：{strategy_id}] 紧急 GTC 兜底挂单失败: {e}")

        return False, None, size, None
