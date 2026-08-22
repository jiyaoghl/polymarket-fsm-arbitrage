import sys
import os
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from polymarket.risk_manager import RiskManager
from polymarket.base_strategy import BaseStrategy
from polymarket.client import PolyClient

class TestSafetyFixes(unittest.TestCase):

    def test_risk_manager_market_lock_and_release(self):
        rm = RiskManager(max_allowed_exposure=100.0)
        strategy_id = "test_strat"
        market_id = "test_mkt_1"

        # 1. 申请额度
        self.assertTrue(rm.acquire_trade_lock(strategy_id, market_id, 30.0))
        self.assertEqual(rm.used_exposure, 30.0)

        # 2. 超额拦截
        self.assertFalse(rm.acquire_trade_lock(strategy_id, "test_mkt_2", 80.0))

        # 3. 显式释放市场锁
        rm.release_market_lock(strategy_id, market_id)
        self.assertEqual(rm.used_exposure, 0.0)
        self.assertNotIn(f"{strategy_id}_{market_id}", rm.locked_orders)

    def test_fee_calculation_taker_maker(self):
        # 首腿 cost=0.40, size=10, 二腿 cost=0.58, size=10
        # 总成本 = 4.0 + 5.8 = 9.80, guaranteed payout = 10.0
        # 如果是 Taker-Maker: 首腿 1% (0.04), 二腿 0% (0.00), total_fee = 0.04, net_ev = 10 - 9.80 - 0.04 = +0.16
        # 如果全部按 Taker 1%: total_fee = 0.098, net_ev = 10 - 9.80 - 0.098 = +0.102
        is_ok, ev_tm, msg = BaseStrategy._verify_hedged_profitability(
            leg1_cost=0.40, leg1_size=10.0,
            leg2_cost=0.58, leg2_size=10.0,
            min_profit_margin=0.01,
            leg1_order_type="FOK",
            leg2_order_type="GTC"
        )
        self.assertTrue(is_ok)
        self.assertAlmostEqual(ev_tm, 0.16, places=4)

if __name__ == "__main__":
    unittest.main()
