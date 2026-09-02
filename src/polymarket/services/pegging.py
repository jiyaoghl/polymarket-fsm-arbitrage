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
    def calculate_adaptive_pegging_params(spread: float) -> Tuple[float, float, float]:
        """
        根据盘口实时价差 (Spread = BestAsk - BestBid) 计算自适应反卷参数：
        - 宽价差 (Spread >= 0.010): 冷却时间缩短至 1.5s，跃迁步长 0.003~0.005 (极速抢买一)
        - 紧凑价差 (Spread < 0.010): 冷却时间维持 3.0s，跃迁步长 0.002~0.004 (防高频抖动)
        
        Returns:
            (delay_seconds, step_min, step_max)
        """
        if spread >= 0.010:
            return 1.5, 0.003, 0.005
        return 3.0, 0.002, 0.004

    @staticmethod
    def calculate_pegged_price(
        current_best_bid: float,
        our_current_price: float,
        entry_max_price: float,
        step_min: float = 0.002,
        step_max: float = 0.004,
        spread: Optional[float] = None
    ) -> Tuple[bool, float, str]:
        """
        计算反卷挂单新价格 (支持基于价差自动调整步长)。
        
        Returns:
            (should_repeg, new_price, reason)
        """
        if spread is not None:
            _, s_min, s_max = MakerPeggingService.calculate_adaptive_pegging_params(spread)
            step_min, step_max = s_min, s_max

        # 如果当前买一价高于我们挂出的价格，说明排位被反超
        if current_best_bid > our_current_price:
            # 阶梯式跳跃反卷
            step = round(random.uniform(step_min, step_max), 4)
            new_target = round(min(current_best_bid + step, entry_max_price), 4)
            
            if new_target > our_current_price:
                return True, new_target, f"被反超 (买一 {current_best_bid:.4f} > 我方 {our_current_price:.4f})，阶梯追价至 {new_target:.4f} (步长: +{step:.4f})"
            else:
                return False, our_current_price, f"目标追价 {new_target:.4f} 已触顶上限 {entry_max_price:.4f}"

        return False, our_current_price, "排位领先或持平，维持当前挂单"

    @staticmethod
    def calculate_dual_bracket_repeg_prices(
        current_yes_price: float,
        current_no_price: float,
        best_bid_yes: Optional[float],
        best_bid_no: Optional[float],
        best_ask_yes: Optional[float],
        best_ask_no: Optional[float],
        entry_max_price: float = 0.45,
        entry_min_price: float = 0.28,
        min_profit_margin: float = 0.020,
        anti_penny_step: float = 0.001
    ) -> Tuple[bool, float, float, str]:
        """
        核算 Maker-Maker 双挂单是否需要执行动态 Re-peg 贴盘跟价 (纯无状态数学函数)。
        
        触发条件：
        1. YES 或 NO 买一向上漂移反超当前挂单价；
        2. 重新核算后的双边新价格 (P_yes + P_no) 严格满足保利底线 <= 1.0 - min_profit_margin；
        3. 至少有一边价格发生 >= 0.002 的阶梯跃迁，且未触顶 entry_max_price 与卖一防穿透保护。
        
        Returns:
            (should_repeg, new_yes_price, new_no_price, reason_msg)
        """
        if best_bid_yes is None or best_bid_no is None:
            return False, current_yes_price, current_no_price, "盘口买一数据缺失"

        # 检查是否有一边被反超
        is_yes_overtaken = (best_bid_yes > current_yes_price)
        is_no_overtaken = (best_bid_no > current_no_price)

        if not is_yes_overtaken and not is_no_overtaken:
            return False, current_yes_price, current_no_price, "双边排位均领先或持平，维持原单"

        from polymarket.services.pricing import PricingEngine
        new_yes, new_no, err = PricingEngine.calculate_dual_bracket_prices(
            best_bid_yes=best_bid_yes,
            best_bid_no=best_bid_no,
            entry_max_price=entry_max_price,
            entry_min_price=entry_min_price,
            min_profit_margin=min_profit_margin,
            best_ask_yes=best_ask_yes,
            best_ask_no=best_ask_no,
            anti_penny_step=anti_penny_step
        )

        if err or new_yes is None or new_no is None:
            return False, current_yes_price, current_no_price, f"跟价计算未通过: {err}"

        # 严格校验保利天花板
        is_prof, _, p_msg = PricingEngine.verify_hedged_profitability(
            new_yes, 10.0, new_no, 10.0,
            min_profit_margin=min_profit_margin,
            leg1_order_type="GTC", leg2_order_type="GTC"
        )
        if not is_prof:
            return False, current_yes_price, current_no_price, f"跟价后利差不足: {p_msg}"

        # 校验是否有 >= 0.002 的实质性改价
        diff_yes = round(new_yes - current_yes_price, 4)
        diff_no = round(new_no - current_no_price, 4)
        if abs(diff_yes) < 0.002 and abs(diff_no) < 0.002:
            return False, current_yes_price, current_no_price, "价格变动不足 0.002 阶梯阈值，忽略微小抖动"

        # 校验卖一防穿透
        if best_ask_yes is not None and new_yes >= best_ask_yes:
            return False, current_yes_price, current_no_price, f"YES 目标跟价 {new_yes:.4f} 触碰卖一 {best_ask_yes:.4f}"
        if best_ask_no is not None and new_no >= best_ask_no:
            return False, current_yes_price, current_no_price, f"NO 目标跟价 {new_no:.4f} 触碰卖一 {best_ask_no:.4f}"

        return (
            True, new_yes, new_no,
            f"双挂贴盘跟价触发 (YES: {current_yes_price:.4f}->{new_yes:.4f} [{diff_yes:+.4f}], NO: {current_no_price:.4f}->{new_no:.4f} [{diff_no:+.4f}])"
        )
