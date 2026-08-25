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
    def execute_force_close(
        client: PolyClient,
        context: TradeContext,
        strategy_id: str = "default"
    ) -> bool:
        """
        执行强平平仓动作：
        1. 先撤销二腿挂单 (若存在)
        2. 发送首腿市价 FOK 平仓
        3. 若 FOK 快速确认未成交，立即以 GTC @ 0.99 挂单紧急兜底
        """
        market_id = context.market_id
        logger.warning(f"[强平引擎：{strategy_id}] 触发动态 TTL 超时强平！市场: {market_id}")

        # 1. 撤销二腿挂单
        if context.leg2_order_id:
            try:
                client.cancel_order(context.leg2_order_id)
                logger.info(f"[强平引擎：{strategy_id}] 已撤销未成交的二腿挂单: {context.leg2_order_id}")
            except Exception as e:
                logger.warning(f"[强平引擎：{strategy_id}] 撤销二腿挂单异常: {e}")

        # 2. 获取首腿持仓明细
        leg1 = context.leg1
        if not leg1 or not leg1.token or leg1.size <= 0:
            logger.error(f"[强平引擎：{strategy_id}] 首腿持仓数据缺失，无法平仓: {leg1}")
            return False

        token_id = leg1.token
        size = leg1.size
        # 首腿是 BUY，平仓方向则为 SELL
        close_side = "SELL" if leg1.side == "BUY" else "BUY"

        # 3. 尝试市价 FOK 平仓 (快速吃单离场)
        try:
            price_info = client.get_market_price(token_id)
            # 卖单取买一价的 95% 保证市价成交
            best_price = price_info.get("bid", 0.01) if close_side == "SELL" else price_info.get("ask", 0.99)
            safe_price = round(max(float(best_price) * 0.95, 0.01), 4) if close_side == "SELL" else round(min(float(best_price) * 1.05, 0.99), 4)
            
            fok_order = client.post_order(token_id, safe_price, size, close_side, "FOK")
            if fok_order and fok_order.get("status") not in ("ERROR", None):
                logger.info(f"[强平引擎：{strategy_id}] 市价 FOK 平仓单发送成功: {fok_order.get('orderID')}")
                return True
        except Exception as e:
            logger.warning(f"[强平引擎：{strategy_id}] 发送市价 FOK 平仓失败: {e}")

        # 4. GTC 紧急挂单兜底 (以极端让价确保被撮合)
        emergency_price = 0.01 if close_side == "SELL" else 0.99
        logger.warning(f"[强平引擎：{strategy_id}] FOK 未能即时成交，启动 GTC @ {emergency_price} 紧急挂单兜底！")
        try:
            gtc_order = client.post_order(token_id, emergency_price, size, close_side, "GTC")
            if gtc_order and gtc_order.get("status") not in ("ERROR", None):
                logger.info(f"[强平引擎：{strategy_id}] 紧急 GTC 兜底单已挂出: {gtc_order.get('orderID')}")
                return True
        except Exception as e:
            logger.critical(f"[强平引擎：{strategy_id}] 紧急 GTC 兜底挂单失败: {e}")

        return False
