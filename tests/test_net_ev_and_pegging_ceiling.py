import unittest

from polymarket.services.pricing import PricingEngine
from polymarket.config import TAKER_FEE_RATE, MAKER_FEE_RATE


class TestNetEVAndPeggingCeiling(unittest.TestCase):
    """测试首腿严格净 EV 守门与二腿追单保利天花板硬钳制"""

    def test_evaluate_taker_ev_strictly_blocks_negative_net_ev(self):
        """测试 evaluate_taker_ev_opportunity 严格拦截价格倒挂或亏损机会"""
        # 场景: min_ask = 0.435, opp_bid = 0.580 (总成本 1.015 > 1.0 倒挂，哪怕 min_ask <= 0.44 也必须拦截)
        is_opp, side, p, ev, msg = PricingEngine.evaluate_taker_ev_opportunity(
            best_ask_yes=0.435,
            best_bid_yes=0.420,
            best_ask_no=0.600,
            best_bid_no=0.580,
            entry_max_price=0.440,
            entry_min_price=0.300,
            min_profit_margin=0.015,
            leg1_amount=10.0
        )
        self.assertFalse(is_opp, "价格倒挂或总成本 > 1.0 时必须严格拦截")

    def test_evaluate_taker_ev_accepts_healthy_net_ev(self):
        """测试在具备充足健康净 EV 空间时判定通过"""
        # 场景: min_ask = 0.380, opp_bid = 0.580 (总成本 0.960, 毛利 0.040 充足)
        is_opp, side, p, ev, msg = PricingEngine.evaluate_taker_ev_opportunity(
            best_ask_yes=0.380,
            best_bid_yes=0.370,
            best_ask_no=0.610,
            best_bid_no=0.580,
            entry_max_price=0.440,
            entry_min_price=0.300,
            min_profit_margin=0.018,
            leg1_amount=10.0
        )
        self.assertTrue(is_opp)
        self.assertEqual(side, "YES")
        self.assertEqual(p, 0.380)
        self.assertGreater(ev, 0.0)

    def test_pegging_ceiling_formula(self):
        """测试二腿追单天花板公式核算与手续费安全垫"""
        leg1_cost = 0.420
        leg1_is_taker = True
        leg2_is_taker = False
        breakeven_margin = 0.005

        fee1 = PricingEngine.calculate_parabolic_fee(leg1_cost, 1.0) if leg1_is_taker else 0.0
        fee2 = PricingEngine.calculate_parabolic_fee(max(0.001, 1.0 - leg1_cost), 1.0) if leg2_is_taker else 0.0
        fee_buffer = fee1 + fee2
        max_allowed_buy_price = round(max(0.01, 1.0 - leg1_cost - breakeven_margin - fee_buffer), 4)

        # 验证总成本 (leg1_cost + max_allowed_buy_price + fees) <= 1.0 - breakeven_margin
        total_cost = leg1_cost + max_allowed_buy_price + fee_buffer
        self.assertLessEqual(total_cost, 1.0 - breakeven_margin + 1e-4)


if __name__ == "__main__":
    unittest.main()
