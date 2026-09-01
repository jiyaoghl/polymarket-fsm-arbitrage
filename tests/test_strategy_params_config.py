import unittest
import time
from unittest.mock import MagicMock

from polymarket.domain.fsm import TradeFSM, TradeState
from polymarket.domain.models import TradeContext
from polymarket.services.handlers.idle_handler import IdleTickHandler
from polymarket.services.handlers.context import StrategyParams, StrategyDependencies, TickBundle, TickFilterLogger

class TestStrategyParamsConfig(unittest.IsolatedAsyncioTestCase):
    """测试微观结构守门阈值配置化与参数分档生效"""

    def test_params_default_and_custom_values(self):
        """测试 StrategyParams 默认值与显式自定义赋值"""
        default_params = StrategyParams(
            strategy_id="s_default", amount=10.0, entry_max_price=0.42, entry_min_price=0.30,
            reentry_trigger=0.35, is_live=False, leg1_order_type="FOK", leg2_order_type="GTC",
            leg2_price_mode="bid", dual_bracket_entry=False, max_slippage_tolerance=0.005,
            leg1_max_unhedged_seconds=90.0, max_concurrent_unhedged_trades=3, exit_mode="dual_exit",
            initial_margin=0.015, breakeven_margin=0.002, flip_timeout_sec=35.0, min_time_to_expiry_entry=45.0
        )
        self.assertEqual(default_params.open_silence_sec, 15.0)
        self.assertEqual(default_params.max_spread, 0.05)
        self.assertEqual(default_params.mm_min_bid, 0.38)
        self.assertEqual(default_params.obi_floor, -0.40)
        self.assertEqual(default_params.base_opp_depth, 20.0)
        self.assertEqual(default_params.opp_depth_amp_mult, 1.5)

        custom_params = StrategyParams(
            strategy_id="s_custom", amount=10.0, entry_max_price=0.42, entry_min_price=0.30,
            reentry_trigger=0.35, is_live=False, leg1_order_type="FOK", leg2_order_type="GTC",
            leg2_price_mode="bid", dual_bracket_entry=False, max_slippage_tolerance=0.005,
            leg1_max_unhedged_seconds=90.0, max_concurrent_unhedged_trades=3, exit_mode="dual_exit",
            initial_margin=0.015, breakeven_margin=0.002, flip_timeout_sec=35.0, min_time_to_expiry_entry=45.0,
            open_silence_sec=25.0, max_spread=0.08, mm_min_bid=0.30, obi_floor=-0.50, base_opp_depth=30.0, opp_depth_amp_mult=2.0
        )
        self.assertEqual(custom_params.open_silence_sec, 25.0)
        self.assertEqual(custom_params.max_spread, 0.08)
        self.assertEqual(custom_params.mm_min_bid, 0.30)
        self.assertEqual(custom_params.obi_floor, -0.50)
        self.assertEqual(custom_params.base_opp_depth, 30.0)
        self.assertEqual(custom_params.opp_depth_amp_mult, 2.0)

    async def test_custom_spread_threshold_interception(self):
        """测试 IdleTickHandler 准确响应自定义的 max_spread 阈值"""
        handler = IdleTickHandler()
        now_ts = time.time()

        ctx = TradeContext(market_id="m_spread_test", status=TradeState.IDLE, end_time=now_ts + 200.0)
        fsm = TradeFSM(market_id="m_spread_test", initial_state=TradeState.IDLE)
        
        # 自定义严格价差: max_spread = 0.02
        strict_params = StrategyParams(
            strategy_id="s_strict", amount=10.0, entry_max_price=0.42, entry_min_price=0.30,
            reentry_trigger=0.35, is_live=False, leg1_order_type="FOK", leg2_order_type="GTC",
            leg2_price_mode="bid", dual_bracket_entry=False, max_slippage_tolerance=0.005,
            leg1_max_unhedged_seconds=90.0, max_concurrent_unhedged_trades=3, exit_mode="dual_exit",
            initial_margin=0.015, breakeven_margin=0.002, flip_timeout_sec=35.0, min_time_to_expiry_entry=45.0,
            max_spread=0.02
        )

        deps = StrategyDependencies(
            client=MagicMock(), risk_manager=MagicMock(), repository=MagicMock(),
            get_trade=lambda m: None, set_trade=MagicMock(), add_trade_event=MagicMock(),
            update_trade_status=MagicMock(), get_unhedged_count=lambda: 0
        )
        filter_logger = TickFilterLogger("s_strict")

        market = {"id": "m_spread_test", "asset": "BTC", "tokens": {"YES": "tok_yes", "NO": "tok_no"}}
        
        # 价差为 0.03 (大于 0.02 严格门槛，但小于默认 0.05)
        tick = TickBundle(
            yes_token="tok_yes", no_token="tok_no",
            best_bid_yes=0.37, best_ask_yes=0.40,
            best_bid_no=0.57, best_ask_no=0.60,
            now_ts=now_ts
        )

        await handler.handle(market, fsm, ctx, tick, strict_params, deps, filter_logger)
        
        # 应该被严格价差拦截在 IDLE 态
        self.assertEqual(fsm.current_state, TradeState.IDLE)

if __name__ == "__main__":
    unittest.main()
