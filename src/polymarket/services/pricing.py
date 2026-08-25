from typing import Dict, Any, Tuple, Optional, List
from polymarket.config import TAKER_FEE_RATE, MAKER_FEE_RATE
from polymarket.logger import logger

class PricingEngine:
    """
    纯数学计算与定价引擎 (Stateless Pure Math Service)。
    不产生任何 I/O 与网络请求，纯粹基于盘口与头寸进行确定性数学核算。
    """

    @staticmethod
    def calculate_vwap(orderbook_asks: List[Any], target_shares: float) -> Optional[float]:
        """
        基于订单簿深度计算买入 target_shares 份数的加权平均成交价 (Ask VWAP)。
        若深度不足，返回 None。
        """
        if not orderbook_asks or target_shares <= 0:
            return None

        # 统一解析格式: [price, size] 或 {"price": ..., "size": ...}
        parsed_asks = []
        for item in orderbook_asks:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                parsed_asks.append((float(item[0]), float(item[1])))
            elif isinstance(item, dict):
                parsed_asks.append((float(item.get("price", 0.0)), float(item.get("size", 0.0))))

        # 卖单按价格升序排列 (从低到高吃)
        sorted_asks = sorted(parsed_asks, key=lambda x: x[0])
        
        accum_shares = 0.0
        total_cost = 0.0

        for price, size in sorted_asks:
            if size <= 0:
                continue

            needed = target_shares - accum_shares
            if size >= needed:
                total_cost += needed * price
                accum_shares += needed
                break
            else:
                total_cost += size * price
                accum_shares += size

        if accum_shares < target_shares:
            return None

        return round(total_cost / target_shares, 4)

    @staticmethod
    def calculate_bid_vwap(orderbook_bids: List[Any], target_shares: float) -> Optional[float]:
        """
        基于订单簿买盘深度计算卖出平仓 target_shares 份数的加权平均成交价 (Bid VWAP)。
        买单按价格降序排列 (从高到低吃买盘)。
        若深度不足，返回 None。
        """
        if not orderbook_bids or target_shares <= 0:
            return None

        parsed_bids = []
        for item in orderbook_bids:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                parsed_bids.append((float(item[0]), float(item[1])))
            elif isinstance(item, dict):
                parsed_bids.append((float(item.get("price", 0.0)), float(item.get("size", 0.0))))

        # 买单按价格降序排列 (从高到低吃买单)
        sorted_bids = sorted(parsed_bids, key=lambda x: x[0], reverse=True)

        accum_shares = 0.0
        total_revenue = 0.0

        for price, size in sorted_bids:
            if size <= 0:
                continue

            needed = target_shares - accum_shares
            if size >= needed:
                total_revenue += needed * price
                accum_shares += needed
                break
            else:
                total_revenue += size * price
                accum_shares += size

        if accum_shares < target_shares:
            return None

        return round(total_revenue / target_shares, 4)

    @staticmethod
    def calculate_net_ev(
        leg1_cost: float,
        leg1_size: float,
        leg2_cost: float,
        leg2_size: float,
        leg1_order_type: str = "FOK",
        leg2_order_type: str = "GTC"
    ) -> Tuple[float, float, float]:
        """
        精确核算双腿扣费净 EV。
        
        Returns:
            (gross_profit, total_fee, net_ev)
        """
        if leg1_size <= 0 or leg2_size <= 0:
            return 0.0, 0.0, 0.0

        fee1_rate = TAKER_FEE_RATE if leg1_order_type == "FOK" else MAKER_FEE_RATE
        fee2_rate = TAKER_FEE_RATE if leg2_order_type == "FOK" else MAKER_FEE_RATE

        fee1 = leg1_cost * leg1_size * fee1_rate
        fee2 = leg2_cost * leg2_size * fee2_rate
        total_fee = fee1 + fee2

        # Guaranteed payout is min(leg1_size, leg2_size) * $1.00
        gross_profit = min(leg1_size, leg2_size) - (leg1_cost * leg1_size + leg2_cost * leg2_size)
        net_ev = gross_profit - total_fee

        return round(gross_profit, 4), round(total_fee, 4), round(net_ev, 4)

    @staticmethod
    def verify_hedged_profitability(
        leg1_cost: float,
        leg1_size: float,
        leg2_cost: float,
        leg2_size: float,
        min_profit_margin: float = 0.015,
        leg1_order_type: str = "GTC",
        leg2_order_type: str = "GTC"
    ) -> Tuple[bool, float, str]:
        """
        双腿净收益严格数学校验拦截器。
        
        Returns:
            (is_profitable, net_ev, reason_msg)
        """
        gross_profit, total_fee, net_ev = PricingEngine.calculate_net_ev(
            leg1_cost, leg1_size, leg2_cost, leg2_size, leg1_order_type, leg2_order_type
        )

        min_shares = min(leg1_size, leg2_size)
        if min_shares <= 0:
            return False, 0.0, "持仓份数必须大于 0"

        # 净收益率 (基于 1.0 兑付基准)
        net_margin = net_ev / min_shares
        
        if net_margin < min_profit_margin:
            return False, net_ev, f"净利润率 {net_margin:.4f} < 门槛 {min_profit_margin:.4f} (Net EV: ${net_ev:.4f})"

        return True, net_ev, f"锁利达标：Net EV ${net_ev:.4f} (Margin: {net_margin:.2%})"

    @staticmethod
    def calculate_dual_bracket_prices(
        best_bid_yes: float,
        best_bid_no: float,
        entry_max_price: float,
        entry_min_price: float = 0.05,
        min_profit_margin: float = 0.015
    ) -> Tuple[Optional[float], Optional[float], Optional[str]]:
        """
        计算 Maker-Maker 双挂互补定价。
        YES 挂买一前沿 (+0.001)，NO 挂互补保利买价 (1.0 - YES - margin)。
        
        Returns:
            (yes_bid_price, no_bid_price, filter_reason)
        """
        yes_bid_price = round(min(best_bid_yes + 0.001, entry_max_price), 4)
        no_bid_price = round(1.0 - yes_bid_price - min_profit_margin, 4)

        if yes_bid_price < entry_min_price or no_bid_price < entry_min_price:
            return None, None, f"双挂价格偏斜: YES={yes_bid_price:.4f}, NO={no_bid_price:.4f} < {entry_min_price}"

        if no_bid_price > (best_bid_no + 0.01):
            return None, None, f"NO 侧溢价过高 ({no_bid_price:.4f} > 买一 {best_bid_no:.4f} + 0.01)"

        return yes_bid_price, no_bid_price, None
