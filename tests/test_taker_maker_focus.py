import unittest
from unittest.mock import MagicMock, AsyncMock
import time
import json

from polymarket.services.handlers.idle_handler import IdleTickHandler
from polymarket.services.handlers.context import StrategyParams, StrategyDependencies, TickBundle, TickFilterLogger
from polymarket.domain.fsm import TradeFSM, TradeState
from polymarket.domain.models import TradeContext
from polymarket.services.grid import OrderbookMemoryGrid, OrderbookSnapshot
from polymarket.paths import configs_dir

class TestTakerMakerFocus(unittest.IsolatedAsyncioTestCase):
    """Taker-Maker 聚焦与极速挂二腿直通通道单元测试"""

    def setUp(self):
        self.handler = IdleTickHandler()
        self.fsm = TradeFSM("test_m_focus")
        self.ctx = TradeContext(market_id="test_m_focus", status="idle")
        self.params = StrategyParams(
            strategy_id="taker_maker_standard",
            amount=10.0,
            entry_max_price=0.42,
            entry_min_price=0.30,
            reentry_trigger=0.35,
            is_live=False,
            leg1_order_type="FOK",
            leg2_order_type="GTC",
            leg2_price_mode="bid",
            dual_bracket_entry=False,
            max_slippage_tolerance=0.01,
            leg1_max_unhedged_seconds=90.0,
            max_concurrent_unhedged_trades=1,
            exit_mode="dual_exit",
            initial_margin=0.015,
            breakeven_margin=0.002,
            flip_timeout_sec=35.0,
            min_time_to_expiry_entry=150.0
        )
        self.trades = {}
        mock_client = MagicMock()
        mock_client.is_live = False
        mock_client.post_order_async = AsyncMock(return_value={"status": "OK", "orderID": "mock_leg1_fok"})
        mock_client.post_batch_orders_async = AsyncMock(return_value={
            "status": "OK",
            "orders": [
                {"order_id": "ord_dual_sell", "token_id": "tok_yes", "price": 0.435, "size": 25.0},
                {"order_id": "ord_dual_buy", "token_id": "tok_no", "price": 0.580, "size": 25.0},
            ]
        })
        self.deps = StrategyDependencies(
            client=mock_client,
            risk_manager=MagicMock(),
            repository=MagicMock(),
            get_trade=lambda m: self.trades.get(m),
            set_trade=lambda m, d: self.trades.update({m: d}),
            add_trade_event=lambda m, s, e: None,
            update_trade_status=lambda m, s, **kw: None,
            get_unhedged_count=lambda: 0,
        )
        self.filter_logger = TickFilterLogger("taker_maker_standard")

    def test_strategies_config_matrix(self):
        """验证 strategies.json 配置已正确收紧价格并下线标准做市"""
        cfg_file = configs_dir() / "strategies.json"
        with open(cfg_file, "r", encoding="utf-8") as f:
            cfgs = json.load(f)

        cfg_map = {c["strategy_id"]: c for c in cfgs}
        
        # 1. 验证价格阶梯收紧
        self.assertEqual(cfg_map["taker_maker_conservative"]["entry_max_price"], 0.40)
        self.assertEqual(cfg_map["taker_maker_standard"]["entry_max_price"], 0.42)
        self.assertEqual(cfg_map["taker_maker_aggressive"]["entry_max_price"], 0.44)

        # 2. 验证 maker_maker_standard 已下线
        self.assertFalse(cfg_map["maker_maker_standard"]["is_active"])
        # 3. 验证 maker_maker_conservative 保持 3U
        self.assertTrue(cfg_map["maker_maker_conservative"]["is_active"])
        self.assertEqual(cfg_map["maker_maker_conservative"]["amount"], 3.0)

    async def test_dual_bracket_entry_038_guard(self):
        """验证做市双挂单成熟度守门已从 0.35 提升至 0.38"""
        params_maker = StrategyParams(
            strategy_id="maker_maker_conservative",
            amount=3.0,
            entry_max_price=0.42,
            entry_min_price=0.30,
            reentry_trigger=0.35,
            is_live=False,
            leg1_order_type="GTC",
            leg2_order_type="GTC",
            leg2_price_mode="bid",
            dual_bracket_entry=True,
            max_slippage_tolerance=0.01,
            leg1_max_unhedged_seconds=90.0,
            max_concurrent_unhedged_trades=1,
            exit_mode="dual_exit",
            initial_margin=0.015,
            breakeven_margin=0.002,
            flip_timeout_sec=35.0,
            min_time_to_expiry_entry=150.0
        )
        now_ts = time.time()
        self.ctx.end_time = now_ts + 200.0

        # 场景 A: 买一为 0.36 (< 0.38)，必须被拦截
        tick_low = TickBundle(
            now_ts=now_ts, yes_token="tok_y", no_token="tok_n",
            best_ask_yes=0.37, best_bid_yes=0.36,
            best_ask_no=0.64, best_bid_no=0.63
        )
        market = {"id": "test_m_maker", "__asset_type": "BTC"}
        await self.handler.handle(market, self.fsm, self.ctx, tick_low, params_maker, self.deps, self.filter_logger)
        self.assertEqual(self.fsm.current_state, TradeState.IDLE)
        self.assertIn("0.38", self.ctx.filter_reason or "")

    async def test_taker_fill_immediate_zero_latency_leg2_dispatch(self):
        """验证首腿成交确认后，就地直通挂出二腿 OCO，流转至 PENDING_LEG2"""
        now_ts = time.time()
        self.ctx.end_time = now_ts + 200.0

        tick = TickBundle(
            now_ts=now_ts,
            yes_token="tok_yes_f",
            no_token="tok_no_f",
            best_ask_yes=0.40,
            best_bid_yes=0.39,
            best_ask_no=0.60,
            best_bid_no=0.58
        )
        market = {"id": "test_m_focus", "__asset_type": "BTC"}

        grid = OrderbookMemoryGrid.get_instance()
        grid._books["tok_yes_f"] = OrderbookSnapshot(
            token_id="tok_yes_f", best_bid=0.39, best_ask=0.40,
            bids=((0.39, 50.0),), asks=((0.40, 50.0),), last_update_ts=now_ts
        )
        grid._books["tok_no_f"] = OrderbookSnapshot(
            token_id="tok_no_f", best_bid=0.58, best_ask=0.60,
            bids=((0.58, 50.0),), asks=((0.60, 50.0),), last_update_ts=now_ts
        )

        await self.handler.handle(market, self.fsm, self.ctx, tick, self.params, self.deps, self.filter_logger)

        # 验证首腿下单后，直接就地直通触发了二腿挂单，状态直接流转至 PENDING_LEG2（无需等待下一个Tick）
        self.assertEqual(self.fsm.current_state, TradeState.PENDING_LEG2)
        self.assertEqual(self.deps.client.post_batch_orders_async.call_count, 1)

if __name__ == "__main__":
    unittest.main()
