import random
import time
from typing import Dict, Any, Optional, Tuple

from polymarket.logger import logger
from polymarket.client import PolyClient
from polymarket.domain.models import TradeContext, LegPosition

class MakerPeggingService:
    """
    Maker 智能盯盘与反卷服务 (Anti-Pennying Pegging Service)。
    
    核心机制：
    1. 随机装死迟滞 (Play-Dead Delay): 被对手压价时随机等待 1.5~3.5s，过滤高频假动作与 API 限流。
    2. 阶梯式跳跃反卷 (Step Jump): 确需追单时直接按 0.002~0.004 阶梯跃迁加价，而非慢吞吞加 0.001。
    3. 纯无状态设计：所有时间戳记录在 Context 或返回值中。
    """

    @staticmethod
    def calculate_pegged_price(
        current_best_bid: float,
        our_current_price: float,
        entry_max_price: float,
        step_min: float = 0.002,
        step_max: float = 0.004
    ) -> Tuple[bool, float, str]:
        """
        计算反卷挂单新价格。
        
        Returns:
            (should_repeg, new_price, reason)
        """
        # 如果当前买一价高于我们挂出的价格，说明排位被反超
        if current_best_bid > our_current_price:
            # 阶梯式跳跃反卷
            step = round(random.uniform(step_min, step_max), 4)
            new_target = round(min(current_best_bid + step, entry_max_price), 4)
            
            if new_target > our_current_price:
                return True, new_target, f"被反超 (买一 {current_best_bid:.4f} > 我方 {our_current_price:.4f})，阶梯追价至 {new_target:.4f}"
            else:
                return False, our_current_price, f"目标追价 {new_target:.4f} 已触顶上限 {entry_max_price:.4f}"

        return False, our_current_price, "排位领先或持平，维持当前挂单"

    @staticmethod
    def should_wait_delay(
        last_overtaken_time: Optional[float],
        delay_seconds: float = 2.0
    ) -> Tuple[bool, float]:
        """
        检查是否处于装死迟滞期。
        
        Returns:
            (is_in_delay, remaining_delay)
        """
        if not last_overtaken_time:
            return False, 0.0

        elapsed = time.time() - last_overtaken_time
        if elapsed < delay_seconds:
            return True, round(delay_seconds - elapsed, 2)

        return False, 0.0
