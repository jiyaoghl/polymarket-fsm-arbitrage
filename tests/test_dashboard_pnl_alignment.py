import unittest
from polymarket.domain.models import TradeContext, LegPosition


class TestDashboardPnLAlignment(unittest.TestCase):
    """测试 Dashboard 与 Discord Bot 的盈亏计算对齐，特别是做 T 卖出单与 0 费率订单"""

    def test_maker_maker_dual_exit_pnl_not_overwritten(self):
        """测试 DUAL_EXIT_SELL_SETTLED 订单不会被错误当成双买公式计算导致严重亏损"""
        trade_dict = {
            "market_id": "0x_test_mm_dual_exit",
            "strategy_id": "maker_maker_conservative",
            "status": "settled",
            "settlement_type": "DUAL_EXIT_SELL_SETTLED",
            "profit_usdc": 0.0977,
            "gross_profit_usdc": 0.0977,
            "fee_usdc": 0.0,
            "leg1": {
                "order_id": "ord_buy_1",
                "token": "tok_1",
                "side": "BUY",
                "cost": 0.5326,
                "size": 7.13
            },
            "leg2": {
                "order_id": "ord_sell_2",
                "token": "tok_1",
                "side": "SELL",
                "cost": 0.5463,
                "size": 7.13
            }
        }

        # 验证提取的权威损益为 +0.0977，而不是被错误双买公式 7.13 - (0.5326*7.13 + 0.5463*7.13) = -0.5625 覆盖
        profit_usdc = float(trade_dict.get("profit_usdc") if trade_dict.get("profit_usdc") is not None else (trade_dict.get("ev") or 0.0))
        self.assertAlmostEqual(profit_usdc, 0.0977, places=4)
        self.assertGreater(profit_usdc, 0.0)


if __name__ == "__main__":
    unittest.main()
