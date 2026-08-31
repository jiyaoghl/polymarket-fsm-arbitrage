import unittest
from unittest.mock import MagicMock, AsyncMock, patch
import time

from polymarket.services.pricing import PricingEngine
from polymarket.kline_analyzer import is_asset_choppy, _asset_status
from polymarket.services.handlers.idle_handler import IdleTickHandler
from polymarket.services.handlers.context import StrategyParams, StrategyDependencies, TickBundle, TickFilterLogger
from polymarket.domain.fsm import TradeFSM, TradeState
from polymarket.domain.models import TradeContext
from polymarket.services.grid import OrderbookMemoryGrid, OrderbookSnapshot

class TestEntryQualityGuard(unittest.IsolatedAsyncioTestCase):
    """入场质量守门与防接飞刀四重防护网测试"""

    def setUp(self):
        self.handler = IdleTickHandler()
        self.fsm = TradeFSM("test_market")
        self.ctx = TradeContext(market_id="test_market", status="idle")
        self.params = StrategyParams(
            strategy_id="test_guard_strat",
            amount=10.0,
            entry_max_price=0.45,
            entry_min_price=0.20,
            reentry_trigger=0.015,
            is_live=False,
            leg1_order_type="FOK",
            leg2_order_type="GTC",
            leg2_price_mode="match_best",
            dual_bracket_entry=False,
            max_slippage_tolerance=0.01,
            leg1_max_unhedged_seconds=90.0,
            max_concurrent_unhedged_trades=1,
            exit_mode="dual_exit",
            initial_margin=0.02,
            breakeven_margin=0.002,
            flip_timeout_sec=35.0,
            min_time_to_expiry_entry=150.0
        )
        mock_client = MagicMock()
        mock_client.post_order_async = AsyncMock(return_value={"status": "OK", "orderID": "mock_ord_1"})
        mock_client.post_batch_orders_async = AsyncMock(return_value={"status": "OK", "orders": []})
        self.deps = StrategyDependencies(
            client=mock_client,
            risk_manager=MagicMock(),
            repository=MagicMock(),
            get_trade=lambda m: None,
            set_trade=lambda m, d: None,
            add_trade_event=lambda m, s, e: None,
            update_trade_status=lambda m, s, **kw: None,
            get_unhedged_count=lambda: 0,
        )
        self.filter_logger = TickFilterLogger("test_guard_strat")

    # ─────────────────────────────────────────────────────────────
    # 1. 开盘 15s 绝对静默时空窗口守门
    # ─────────────────────────────────────────────────────────────
    async def test_opening_15s_absolute_silence_guard(self):
        now_ts = time.time()
        # 5min 市场总时长 300s，若 end_time 为 now_ts + 290，剩余 290s > 285s（刚开盘 10s）
        self.ctx.end_time = now_ts + 290.0
        
        tick = TickBundle(
            now_ts=now_ts,
            yes_token="tok_yes",
            no_token="tok_no",
            best_ask_yes=0.40,
            best_bid_yes=0.39,
            best_ask_no=0.60,
            best_bid_no=0.59
        )
        market = {"id": "test_market", "__asset_type": "BTC"}

        await self.handler.handle(
            market, self.fsm, self.ctx, tick, self.params, self.deps, self.filter_logger
        )

        # 验证被开盘静默拦截，状态保持 IDLE，未触发发单
        self.assertEqual(self.fsm.current_state, TradeState.IDLE)
        self.deps.client.post_order_async.assert_not_called()

    # ─────────────────────────────────────────────────────────────
    # 2. 现货 1m 极速动量飞刀冲击检测
    # ─────────────────────────────────────────────────────────────
    @patch("polymarket.kline_analyzer._session.get")
    def test_spot_1m_momentum_shock_filter(self, mock_get):
        _asset_status.clear()
        
        # 模拟 10 根 1m K 线，前 9 根平稳，最后一根 1m 暴跌 0.35% (消耗远超 65% 阈值)
        # K 线结构: [open_time, open, high, low, close, ...]
        base_p = 70000.0
        klines = [
            [i, str(base_p), str(base_p + 10), str(base_p - 10), str(base_p), 0]
            for i in range(9)
        ]
        # 第 10 根 1m：从 70000 瀑布跌至 69700 (跌 0.428%)
        klines.append([9, "70000.0", "70005.0", "69680.0", "69700.0", 100])
        
        mock_resp = MagicMock()
        mock_resp.json.return_value = klines
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        # BTC 的 max_net_change 为 0.30%，65% 门槛为 0.195%
        # 最后一根跌 0.428% >= 0.195%，必须判定为单边动量冲击
        is_choppy = is_asset_choppy("BTC", limit=10, cache_ttl=0.0)
        self.assertFalse(is_choppy, "1m 发生急跌瀑布时，必须拦截开仓 (is_choppy=False)")

    # ─────────────────────────────────────────────────────────────
    # 3. 首腿常规严格保利与极度超跌做 T 双轨核算
    # ─────────────────────────────────────────────────────────────
    def test_evaluate_taker_ev_strict_and_oversold(self):
        # 场景 A: 常规价格 (0.40)，对侧买一充足 (0.58)，保利达标 (0.40 + 0.58 = 0.98 <= 1.0)
        opp, side, p, ev, msg = PricingEngine.evaluate_taker_ev_opportunity(
            best_ask_yes=0.40, best_bid_yes=0.39,
            best_ask_no=0.60, best_bid_no=0.58,
            entry_max_price=0.45, min_profit_margin=0.010, leg1_amount=10.0
        )
        self.assertTrue(opp)
        self.assertEqual(side, "YES")
        self.assertEqual(p, 0.40)

        # 场景 B: 常规价格 (0.40)，但对侧买一极低 (0.20 < 0.25)，对冲不成立，必须拦截
        opp, side, p, ev, msg = PricingEngine.evaluate_taker_ev_opportunity(
            best_ask_yes=0.40, best_bid_yes=0.39,
            best_ask_no=0.80, best_bid_no=0.20,
            entry_max_price=0.45, min_profit_margin=0.010, leg1_amount=10.0
        )
        self.assertFalse(opp, "常规单对侧买一不足 0.25 时必须拦截")

        # 场景 C: 极度超跌 (0.22 <= 0.25)，对侧买一达到 0.18 (>= 0.15)，允许超跌做 T
        opp, side, p, ev, msg = PricingEngine.evaluate_taker_ev_opportunity(
            best_ask_yes=0.22, best_bid_yes=0.21,
            best_ask_no=0.80, best_bid_no=0.18,
            entry_max_price=0.45, min_profit_margin=0.010, leg1_amount=10.0
        )
        self.assertTrue(opp, "极度超跌且对侧 >= 0.15 应当放行做 T")
        self.assertEqual(side, "YES")
        self.assertEqual(p, 0.22)
        self.assertIn("极度超跌做T达标", msg)

    # ─────────────────────────────────────────────────────────────
    # 4. 对侧买盘 OBI 深度承接壁垒拦截
    # ─────────────────────────────────────────────────────────────
    async def test_opponent_orderbook_depth_guard(self):
        now_ts = time.time()
        # 处于成熟期 (剩余 200s)
        self.ctx.end_time = now_ts + 200.0
        
        tick = TickBundle(
            now_ts=now_ts,
            yes_token="tok_yes_1",
            no_token="tok_no_1",
            best_ask_yes=0.38,
            best_bid_yes=0.37,
            best_ask_no=0.62,
            best_bid_no=0.60
        )
        market = {"id": "test_market_depth", "__asset_type": "BTC"}

        grid = OrderbookMemoryGrid.get_instance()
        # 目标 Token: 深度正常
        snap_yes = OrderbookSnapshot(
            token_id="tok_yes_1",
            best_bid=0.37,
            best_ask=0.38,
            bids=((0.37, 50.0),),
            asks=((0.38, 50.0),),
            last_update_ts=now_ts
        )
        # 对侧 Token: 买盘极薄 (前 5 档仅 8 份 < 20.0 份)
        snap_no = OrderbookSnapshot(
            token_id="tok_no_1",
            best_bid=0.60,
            best_ask=0.62,
            bids=((0.60, 5.0), (0.59, 3.0)),
            asks=((0.62, 50.0),),
            last_update_ts=now_ts
        )
        grid._books["tok_yes_1"] = snap_yes
        grid._books["tok_no_1"] = snap_no

        await self.handler.handle(
            market, self.fsm, self.ctx, tick, self.params, self.deps, self.filter_logger
        )

        # 验证由于对侧深度不足 20 份被拦截，未开仓
        self.assertEqual(self.fsm.current_state, TradeState.IDLE)
        self.deps.client.post_order_async.assert_not_called()

if __name__ == "__main__":
    unittest.main()
