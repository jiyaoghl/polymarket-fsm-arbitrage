import unittest
import time

from polymarket.domain.models import TradeContext, LegPosition
from polymarket.services.grid import OrderbookMemoryGrid, OrderbookSnapshot


class TestRepegAndGridGC(unittest.TestCase):
    """测试二腿追单轨迹记录与内存盘口网格 GC 机制"""

    def test_trade_context_reprice_recording(self):
        """测试 TradeContext 结构化记录追单改价轨迹"""
        ctx = TradeContext(market_id="m_repeg_test", status="pending_leg2")
        self.assertEqual(ctx.reprice_count, 0)
        self.assertEqual(len(ctx.reprice_history), 0)

        # 记录第一次追单
        ctx.record_reprice(old_price=0.55, new_price=0.56, reason="MakerPegging: 抬升挂单", token="tok_123")
        self.assertEqual(ctx.reprice_count, 1)
        self.assertEqual(len(ctx.reprice_history), 1)
        self.assertEqual(ctx.reprice_history[0]["old_price"], 0.55)
        self.assertEqual(ctx.reprice_history[0]["new_price"], 0.56)
        self.assertEqual(ctx.reprice_history[0]["token"], "tok_123")

        # 序列化与反序列化验证
        d = ctx.to_dict()
        self.assertEqual(d["reprice_count"], 1)
        self.assertEqual(len(d["reprice_history"]), 1)

        ctx_restored = TradeContext.from_dict(d)
        self.assertEqual(ctx_restored.reprice_count, 1)
        self.assertEqual(ctx_restored.reprice_history[0]["reason"], "MakerPegging: 抬升挂单")

    def test_orderbook_grid_purge_stale_tokens(self):
        """测试 OrderbookMemoryGrid 驱逐淘汰过期 Token 快照"""
        grid = OrderbookMemoryGrid.get_instance()
        now = time.time()

        # 注入一个新鲜 Token 快照和一个过期 Token 快照
        fresh_snap = OrderbookSnapshot(
            token_id="tok_fresh", best_bid=0.45, best_ask=0.46,
            bids=((0.45, 10.0),), asks=((0.46, 10.0),),
            last_update_ts=now
        )
        stale_snap = OrderbookSnapshot(
            token_id="tok_stale", best_bid=0.40, best_ask=0.41,
            bids=((0.40, 10.0),), asks=((0.41, 10.0),),
            last_update_ts=now - 1000.0  # 1000秒前 (过期)
        )

        with grid._write_lock:
            grid._books["tok_fresh"] = fresh_snap
            grid._books["tok_stale"] = stale_snap

        # 执行清理 (TTL=600s)
        purged = grid.purge_stale_tokens(ttl_seconds=600.0)
        self.assertGreaterEqual(purged, 1)

        self.assertIsNotNone(grid.get_snapshot("tok_fresh"))
        self.assertIsNone(grid.get_snapshot("tok_stale"))


if __name__ == "__main__":
    unittest.main()
