import unittest
from unittest.mock import AsyncMock, MagicMock
import time

from polymarket.domain.fsm import TradeFSM, TradeState
from polymarket.domain.models import TradeContext
from polymarket.services.pegging import MakerPeggingService
from polymarket.services.handlers.pending_both_handler import PendingBothLegsTickHandler
from polymarket.services.handlers.context import StrategyParams, StrategyDependencies, TickBundle, TickFilterLogger

class TestDualMakerRepeg(unittest.IsolatedAsyncioTestCase):

    def test_calculate_dual_bracket_repeg_prices_overtaken(self):
        # 场景 1: 原挂单 (YES=0.34, NO=0.34)。盘口买一漂移至 (YES=0.36, NO=0.36)，卖一为 0.60
        # 目标保利 margin = 0.020 (即 1.0 - 0.02 = 0.98)
        should_repeg, new_yes, new_no, msg = MakerPeggingService.calculate_dual_bracket_repeg_prices(
            current_yes_price=0.34,
            current_no_price=0.34,
            best_bid_yes=0.36,
            best_bid_no=0.36,
            best_ask_yes=0.60,
            best_ask_no=0.60,
            entry_max_price=0.43,
            entry_min_price=0.28,
            min_profit_margin=0.020,
            anti_penny_step=0.001
        )
        self.assertTrue(should_repeg)
        self.assertGreater(new_yes, 0.34)
        self.assertGreater(new_no, 0.34)
        self.assertLessEqual(round(new_yes + new_no, 4), 0.98)
        self.assertIn("双挂贴盘跟价触发", msg)

    def test_calculate_dual_bracket_repeg_prices_insufficient_profit(self):
        # 场景 2: 盘口买一过高 (YES=0.48, NO=0.51)，总成本 0.99 > 0.98，保利不达标，必须拒绝跟价
        should_repeg, new_yes, new_no, msg = MakerPeggingService.calculate_dual_bracket_repeg_prices(
            current_yes_price=0.34,
            current_no_price=0.34,
            best_bid_yes=0.48,
            best_bid_no=0.51,
            best_ask_yes=0.52,
            best_ask_no=0.55,
            entry_max_price=0.43,
            entry_min_price=0.28,
            min_profit_margin=0.020
        )
        self.assertFalse(should_repeg)
        self.assertEqual(new_yes, 0.34)
        self.assertEqual(new_no, 0.34)

    async def test_pending_both_handler_repeg_flow_and_cooldown(self):
        handler = PendingBothLegsTickHandler()
        fsm = TradeFSM("maker_maker_standard")
        fsm.transition_to(TradeState.IDLE)
        fsm.transition_to(TradeState.PENDING_BOTH_LEGS)

        ctx = TradeContext(
            market_id="0x_test_market",
            status="pending_both",
            dual_orders=[
                {"token_id": "tok_yes", "price": 0.34, "size": 10.0, "side": "BUY"},
                {"token_id": "tok_no", "price": 0.34, "size": 10.0, "side": "BUY"}
            ],
            end_time=time.time() + 200.0,
            last_reprice_time=time.time() - 10.0  # 10s 前，已过冷却期
        )

        market = {"id": "0x_test_market", "__asset_type": "BTC"}
        params = StrategyParams(
            strategy_id="maker_maker_standard",
            amount=10.0,
            entry_max_price=0.43,
            entry_min_price=0.28,
            reentry_trigger=0.35,
            is_live=False,
            leg1_order_type="GTC",
            leg2_order_type="GTC",
            leg2_price_mode="bid",
            dual_bracket_entry=True,
            max_slippage_tolerance=0.01,
            leg1_max_unhedged_seconds=85,
            max_concurrent_unhedged_trades=3,
            exit_mode="dual_exit",
            initial_margin=0.020,
            breakeven_margin=0.003,
            flip_timeout_sec=30,
            min_time_to_expiry_entry=30.0
        )

        client = MagicMock()
        client.post_batch_orders_async = AsyncMock(return_value={"status": "OK", "orders": []})
        risk_mgr = MagicMock()
        repo = MagicMock()
        set_trade_fn = MagicMock()
        deps = StrategyDependencies(
            client=client,
            risk_manager=risk_mgr,
            repository=repo,
            get_trade=MagicMock(return_value=ctx.to_dict()),
            set_trade=set_trade_fn,
            add_trade_event=MagicMock(),
            update_trade_status=MagicMock(),
            get_unhedged_count=MagicMock(return_value=0)
        )
        filter_logger = TickFilterLogger("maker_maker_standard")

        now_ts = time.time()
        # 盘口买一上涨至 0.36，但双方卖一仍较高 (0.60/0.60) -> 触发模拟盘改单
        tick = TickBundle(
            yes_token="tok_yes",
            no_token="tok_no",
            best_bid_yes=0.36,
            best_ask_yes=0.60,
            best_bid_no=0.36,
            best_ask_no=0.60,
            now_ts=now_ts
        )

        await handler.handle(market, fsm, ctx, tick, params, deps, filter_logger)

        # 验证模拟盘中订单价格已被平滑抬升
        yes_order = next(o for o in ctx.dual_orders if o["token_id"] == "tok_yes")
        no_order = next(o for o in ctx.dual_orders if o["token_id"] == "tok_no")
        self.assertGreater(yes_order["price"], 0.34)
        self.assertGreater(no_order["price"], 0.34)
        self.assertEqual(len(ctx.reprice_history), 2)
        self.assertEqual(fsm.current_state, TradeState.PENDING_BOTH_LEGS)

        # 紧接着下一帧 (0.1s 内)，测试冷却防抖 (不应再次触发改单)
        tick_quick = TickBundle(
            yes_token="tok_yes",
            no_token="tok_no",
            best_bid_yes=0.37,
            best_ask_yes=0.60,
            best_bid_no=0.37,
            best_ask_no=0.60,
            now_ts=now_ts + 0.1
        )
        await handler.handle(market, fsm, ctx, tick_quick, params, deps, filter_logger)
        # 确认改单历史未继续增加
        self.assertEqual(len(ctx.reprice_history), 2)

if __name__ == "__main__":
    unittest.main()
