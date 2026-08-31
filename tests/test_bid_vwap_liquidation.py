import unittest
from unittest.mock import MagicMock
import time

from polymarket.services.pricing import PricingEngine
from polymarket.services.grid import OrderbookMemoryGrid, OrderbookSnapshot
from polymarket.services.liquidator import AdaptiveLiquidatorService
from polymarket.domain.models import TradeContext, LegPosition

class TestBidVwapLiquidation(unittest.TestCase):
    """订单簿深度加权平仓 (Bid VWAP) 与防穿透保护测试"""

    def test_multi_level_vwap_and_marginal_calculation(self):
        """测试多档买盘穿透的 VWAP 均价与边际档位价计算"""
        bids = [
            (0.45, 10.0),  # 买一: 10 份 @ 0.45
            (0.40, 10.0),  # 买二: 10 份 @ 0.40
            (0.30, 10.0),  # 买三: 10 份 @ 0.30
        ]
        target_shares = 25.0

        # 吃 10 @ 0.45 + 10 @ 0.40 + 5 @ 0.30 = 4.5 + 4.0 + 1.5 = 10.0 / 25.0 = 0.4000
        # 触及的最低边际价为 0.30
        vwap, marginal_p, filled = PricingEngine.calculate_bid_vwap_and_marginal(bids, target_shares)
        
        self.assertEqual(vwap, 0.4000)
        self.assertEqual(marginal_p, 0.3000)
        self.assertEqual(filled, 25.0)

    def test_partial_depth_full_penetration_fallback(self):
        """测试买盘总深度不足时全量穿透至最后一档"""
        bids = [
            (0.45, 10.0),  # 买一: 10 份 @ 0.45
            (0.40, 5.0),   # 买二: 5 份 @ 0.40 (总共仅 15 份)
        ]
        target_shares = 25.0  # 需要卖 25 份

        # 吃尽全部 15 份: 4.5 + 2.0 = 6.5 / 15.0 = 0.4333
        # 边际价为最后一档 0.40
        vwap, marginal_p, filled = PricingEngine.calculate_bid_vwap_and_marginal(bids, target_shares)
        
        self.assertEqual(vwap, 0.4333)
        self.assertEqual(marginal_p, 0.4000)
        self.assertEqual(filled, 15.0)

    def test_grid_5s_strict_staleness_guard(self):
        """测试本地盘口内存网格严格 5.0 秒时效性守门"""
        grid = OrderbookMemoryGrid.get_instance()
        now_ts = time.time()
        
        # 1. 2 秒前的新鲜快照 (<= 5.0s)
        snap_fresh = OrderbookSnapshot(
            token_id="tok_fresh",
            best_bid=0.45,
            best_ask=0.46,
            bids=((0.45, 20.0),),
            asks=((0.46, 20.0),),
            last_update_ts=now_ts - 2.0
        )
        grid._books["tok_fresh"] = snap_fresh
        vwap_fresh, marginal_fresh, _ = grid.calculate_bid_vwap_and_marginal_local("tok_fresh", 10.0, max_staleness=5.0)
        self.assertIsNotNone(vwap_fresh)
        self.assertEqual(vwap_fresh, 0.45)

        # 2. 6 秒前的陈旧快照 (> 5.0s)，必须返回 None 触发 REST 降级
        snap_stale = OrderbookSnapshot(
            token_id="tok_stale",
            best_bid=0.45,
            best_ask=0.46,
            bids=((0.45, 20.0),),
            asks=((0.46, 20.0),),
            last_update_ts=now_ts - 6.0
        )
        grid._books["tok_stale"] = snap_stale
        vwap_stale, marginal_stale, _ = grid.calculate_bid_vwap_and_marginal_local("tok_stale", 10.0, max_staleness=5.0)
        self.assertIsNone(vwap_stale, "超过 5.0s 的快照必须判定陈旧并返回 None")

    def test_execute_force_close_marginal_protection_and_vwap_booking(self):
        """测试平仓发单以 marginal - 0.002 保护限价发单，并以 VWAP 真实记账"""
        grid = OrderbookMemoryGrid.get_instance()
        now_ts = time.time()
        token_id = "tok_force_test"

        # 买一: 10 @ 0.45, 买二: 15 @ 0.38
        grid._books[token_id] = OrderbookSnapshot(
            token_id=token_id,
            best_bid=0.45,
            best_ask=0.46,
            bids=((0.45, 10.0), (0.38, 15.0)),
            asks=((0.46, 20.0),),
            last_update_ts=now_ts
        )

        mock_client = MagicMock()
        mock_client.post_order.return_value = {
            "status": "FILLED",
            "orderID": "0x_fok_close_999",
            "price": 0.378  # 模拟发单保护价
        }

        ctx = TradeContext(
            market_id="m_vwap_test",
            status="leg1_only",
            leg1=LegPosition(order_id="ord_1", token=token_id, side="BUY", cost=0.43, size=20.0),
            end_time=now_ts + 60.0
        )

        # 需要卖出 20 份: 吃 10 @ 0.45 + 10 @ 0.38 = 4.5 + 3.8 = 8.3 / 20 = 0.4150
        # 最低边际价为 0.38
        # 发单保护限价应为 0.38 - 0.002 = 0.3780
        success, close_price, size, order_id = AdaptiveLiquidatorService.execute_force_close(
            mock_client, ctx, strategy_id="test_strat", allow_grace=False
        )

        self.assertTrue(success)
        self.assertEqual(close_price, 0.4150, "模拟盘平仓必须 100% 严格使用加权 VWAP 均价记账")
        
        # 验证 post_order 参数：价格为 0.378 (marginal - 0.002)
        mock_client.post_order.assert_called_once_with(
            token_id, 0.378, 20.0, "SELL", "FOK"
        )

if __name__ == "__main__":
    unittest.main()
