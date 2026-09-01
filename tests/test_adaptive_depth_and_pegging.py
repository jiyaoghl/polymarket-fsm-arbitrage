import unittest
from unittest.mock import MagicMock, AsyncMock, patch
import time

from polymarket.services.pegging import MakerPeggingService
from polymarket.services.handlers.pending_leg2_handler import PendingLeg2TickHandler
from polymarket.services.handlers.idle_handler import IdleTickHandler
from polymarket.domain.fsm import TradeFSM, TradeState
from polymarket.domain.models import TradeContext, LegPosition
from polymarket.services.handlers.context import StrategyParams, StrategyDependencies, TickBundle, TickFilterLogger
from polymarket.services.grid import OrderbookMemoryGrid, OrderbookSnapshot

class TestAdaptiveDepthAndPegging(unittest.IsolatedAsyncioTestCase):
    """测试波动率联动深度壁垒与价差自适应 Anti-Pennying"""

    def test_pegging_adaptive_params_calculation(self):
        """测试不同价差下的自适应迟滞与步长计算"""
        # 1. 宽价差 (>= 0.010)
        delay_wide, s_min_wide, s_max_wide = MakerPeggingService.calculate_adaptive_pegging_params(0.015)
        self.assertEqual(delay_wide, 1.5)
        self.assertEqual(s_min_wide, 0.003)
        self.assertEqual(s_max_wide, 0.005)

        # 2. 紧凑价差 (< 0.010)
        delay_tight, s_min_tight, s_max_tight = MakerPeggingService.calculate_adaptive_pegging_params(0.004)
        self.assertEqual(delay_tight, 3.0)
        self.assertEqual(s_min_tight, 0.002)
        self.assertEqual(s_max_tight, 0.004)

    def test_calculate_pegged_price_with_spread(self):
        """测试传入 spread 时的阶梯反卷计算"""
        # 被反超: current_bid = 0.45 > our_price = 0.43, 上限 0.55
        should_repeg, new_p, reason = MakerPeggingService.calculate_pegged_price(
            current_best_bid=0.45,
            our_current_price=0.43,
            entry_max_price=0.55,
            spread=0.015
        )
        self.assertTrue(should_repeg)
        # 步长 0.003~0.005 -> 新价格在 0.4530 ~ 0.4550 之间
        self.assertGreaterEqual(new_p, 0.4530)
        self.assertLessEqual(new_p, 0.4550)

    async def test_pending_leg2_spread_adaptive_delay(self):
        """测试 PendingLeg2TickHandler 在不同价差下的自适应冷却跟单"""
        handler = PendingLeg2TickHandler()
        now_ts = 1000.0

        ctx = TradeContext(
            market_id="m_pegging_test",
            status=TradeState.PENDING_LEG2,
            leg1=LegPosition(order_id="l1", token="tok_yes", side="BUY", cost=0.40, size=10.0),
            leg1_filled_time=now_ts - 2.0,  # 2 秒前成交
            dual_orders=[
                {"side": "SELL", "price": 0.42, "order_id": "ord_sell"},
                {"side": "BUY", "price": 0.45, "order_id": "ord_buy"}  # 我方当前买单挂 0.45
            ]
        )

        fsm = TradeFSM(market_id="m_pegging_test", initial_state=TradeState.PENDING_LEG2)
        params = StrategyParams(
            strategy_id="strat_peg",
            amount=10.0,
            entry_max_price=0.48,
            entry_min_price=0.10,
            reentry_trigger=0.52,
            is_live=False,
            leg1_order_type="FOK",
            leg2_order_type="GTC",
            leg2_price_mode="bid",
            dual_bracket_entry=False,
            max_slippage_tolerance=0.005,
            leg1_max_unhedged_seconds=90.0,
            max_concurrent_unhedged_trades=3,
            exit_mode="dual_exit",
            initial_margin=0.025,
            breakeven_margin=0.002,
            flip_timeout_sec=15.0,
            min_time_to_expiry_entry=45.0,
        )
        
        client = MagicMock()
        client.post_order_async = AsyncMock()
        client.cancel_order_async = AsyncMock()
        risk_mgr = MagicMock()
        repo = MagicMock()
        
        deps = StrategyDependencies(
            client=client,
            risk_manager=risk_mgr,
            repository=repo,
            get_trade=lambda m: None,
            set_trade=MagicMock(),
            add_trade_event=MagicMock(),
            update_trade_status=MagicMock(),
            get_unhedged_count=lambda: 0
        )
        filter_logger = TickFilterLogger("strat_peg")

        market = {
            "id": "m_pegging_test",
            "asset": "BTC",
            "tokens": {"YES": "tok_yes", "NO": "tok_no"}
        }

        # 1. 宽价差场景: NO 卖一 0.52, 买一 0.48 (Spread = 0.040 >= 0.010, 冷却只需 1.5s)
        # 当前距离上次更新过去了 2.0s >= 1.5s，必须触发追单！
        tick_wide = TickBundle(
            yes_token="tok_yes",
            no_token="tok_no",
            best_bid_yes=0.40,
            best_ask_yes=0.42,
            best_bid_no=0.48,  # 对手反超至 0.48
            best_ask_no=0.52,  # 价差 0.040
            now_ts=now_ts
        )

        await handler.handle(market, fsm, ctx, tick_wide, params, deps, filter_logger)
        
        # 验证 buy_info 中的价格被追高更新 (> 0.48)
        buy_order = [o for o in ctx.dual_orders if o["side"] == "BUY"][0]
        self.assertGreater(buy_order["price"], 0.48)
        self.assertEqual(ctx.last_reprice_time, now_ts)

    async def test_idle_handler_dynamic_volatility_depth_barrier(self):
        """测试高波动率下动态提升对侧深度门槛拦截 (20 -> 46 份)"""
        handler = IdleTickHandler()
        grid = OrderbookMemoryGrid.get_instance()
        now_ts = time.time()

        # 模拟盘口: YES 卖一 0.38, 对侧 NO 买盘深度 25.0 份 (高于静态 20 份，但低于高波动要求的 46 份)
        grid._books["tok_yes"] = OrderbookSnapshot(
            token_id="tok_yes",
            best_bid=0.36,
            best_ask=0.38,
            bids=((0.36, 10.0),),
            asks=((0.38, 20.0),),
            last_update_ts=now_ts
        )
        grid._books["tok_no"] = OrderbookSnapshot(
            token_id="tok_no",
            best_bid=0.60,
            best_ask=0.62,
            bids=((0.60, 25.0),),  # 25 份深度
            asks=((0.62, 10.0),),
            last_update_ts=now_ts
        )

        market = {
            "id": "m_depth_test",
            "asset": "BTC",
            "tokens": {"YES": "tok_yes", "NO": "tok_no"}
        }

        ctx = TradeContext(market_id="m_depth_test", status=TradeState.IDLE)
        fsm = TradeFSM(market_id="m_depth_test", initial_state=TradeState.IDLE)
        params = StrategyParams(
            strategy_id="strat_test",
            amount=10.0,
            entry_max_price=0.40,
            entry_min_price=0.30,
            reentry_trigger=0.35,
            is_live=False,
            leg1_order_type="FOK",
            leg2_order_type="GTC",
            leg2_price_mode="bid",
            dual_bracket_entry=False,
            max_slippage_tolerance=0.005,
            leg1_max_unhedged_seconds=90.0,
            max_concurrent_unhedged_trades=3,
            exit_mode="dual_exit",
            initial_margin=0.015,
            breakeven_margin=0.002,
            flip_timeout_sec=35.0,
            min_time_to_expiry_entry=45.0,
        )

        client = MagicMock()
        client.post_order_async = AsyncMock(return_value={"orderID": "0x_mock_1", "status": "LIVE"})
        risk_mgr = MagicMock()
        risk_mgr.is_market_occupied.return_value = (False, None)
        risk_mgr.acquire_trade_lock.return_value = True
        repo = MagicMock()
        
        deps = StrategyDependencies(
            client=client,
            risk_manager=risk_mgr,
            repository=repo,
            get_trade=lambda m: None,
            set_trade=MagicMock(),
            add_trade_event=MagicMock(),
            update_trade_status=MagicMock(),
            get_unhedged_count=lambda: 0
        )
        filter_logger = TickFilterLogger("strat_test")

        tick = TickBundle(
            yes_token="tok_yes",
            no_token="tok_no",
            best_bid_yes=0.36,
            best_ask_yes=0.38,
            best_bid_no=0.60,
            best_ask_no=0.62,
            now_ts=now_ts
        )

        # Mock K 线振幅为 0.35% (警戒上限约 0.40%，振幅比 87.5%，要求深度 20 * 2.31 = 46.2 份)
        with patch("polymarket.kline_analyzer.get_asset_status", return_value={"amplitude": 0.35}):
            await handler.handle(market, fsm, ctx, tick, params, deps, filter_logger)
        
        # 验证被动态深度壁垒成功拦截，保持在 IDLE 状态，且未发出任何下单请求！
        self.assertEqual(fsm.current_state, TradeState.IDLE)
        self.assertIn("承接深度不足", ctx.filter_reason)
        client.post_order_async.assert_not_called()

if __name__ == "__main__":
    unittest.main()
