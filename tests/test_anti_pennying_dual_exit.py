import unittest
from unittest.mock import MagicMock, AsyncMock
import time

from polymarket.services.handlers.pending_leg2_handler import PendingLeg2TickHandler
from polymarket.services.handlers.context import StrategyParams, StrategyDependencies, TickBundle, TickFilterLogger
from polymarket.domain.fsm import TradeFSM, TradeState
from polymarket.domain.models import TradeContext, LegPosition
from polymarket.services.liquidator import AdaptiveLiquidatorService
from polymarket.services.grid import OrderbookMemoryGrid, OrderbookSnapshot

class TestAntiPennyingDualExit(unittest.IsolatedAsyncioTestCase):
    """对冲端微观执行、Anti-Pennying 阶梯跟单与弹性缓冲测试"""

    def setUp(self):
        self.handler = PendingLeg2TickHandler()
        self.fsm = TradeFSM("test_m_pegging", initial_state=TradeState.PENDING_LEG2)
        self.ctx = TradeContext(
            market_id="test_m_pegging",
            status="pending_leg2",
            leg1=LegPosition(order_id="ord_l1", token="tok_yes", cost=0.40, size=25.0, side="BUY"),
            leg1_filled_time=time.time() - 20.0,
            dual_orders=[
                {"order_id": "ord_sell_1", "side": "SELL", "price": 0.420, "size": 25.0},
                {"order_id": "ord_buy_1", "side": "BUY", "price": 0.570, "size": 25.0}
            ]
        )
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
        self.trades = {"test_m_pegging": self.ctx.to_dict()}
        mock_client = MagicMock()
        mock_client.is_live = False
        mock_client.cancel_order_async = AsyncMock(return_value=True)
        mock_client.post_order_async = AsyncMock(return_value={"status": "OK", "orderID": "mock_new_buy"})
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

    async def test_oco_buy_order_step_jump_pegging(self):
        """测试 OCO 对冲买单被对手盘反超后，执行 0.002~0.004 阶梯跳跃跟单"""
        now_ts = time.time()
        self.ctx.last_reprice_time = now_ts - 5.0  # 5 秒前改过价，满足 >= 3.0s 冷却
        
        # 对手盘 NO 买一抬升至 0.578 (> 我方 0.570)
        tick = TickBundle(
            now_ts=now_ts,
            yes_token="tok_yes",
            no_token="tok_no",
            best_ask_yes=0.425,
            best_bid_yes=0.415,
            best_ask_no=0.590,
            best_bid_no=0.578
        )
        market = {"id": "test_m_pegging", "expiry": now_ts + 100.0, "__asset_type": "BTC"}

        await self.handler.handle(market, self.fsm, self.ctx, tick, self.params, self.deps, self.filter_logger)

        buy_info = next(o for o in self.ctx.dual_orders if o.get("side") == "BUY")
        # 验证买单价格跳跃抬升至 > 0.578 (0.580 ~ 0.582)
        self.assertGreater(buy_info["price"], 0.578)
        # 验证未突破最高保利上限 (1.0 - 0.40 - fee - 0.002 约 0.596)
        self.assertLessEqual(buy_info["price"], 0.596)

    async def test_oco_sell_order_maintains_target_without_premature_discount(self):
        """测试 OCO 卖单在持仓时间 < 20s 时坚守初始高抛目标价 0.420，不进行过早降价"""
        now_ts = time.time()
        self.ctx.leg1_filled_time = now_ts - 10.0  # 持仓仅 10 秒，处于 0~20s 坚守期
        self.ctx.last_reprice_time = now_ts - 10.0
        
        # 距离到期只有 35 秒
        market = {"id": "test_m_pegging", "expiry": now_ts + 35.0, "__asset_type": "BTC"}
        
        # YES 买一为 0.380 (低于首腿成本 0.40)
        tick = TickBundle(
            now_ts=now_ts,
            yes_token="tok_yes",
            no_token="tok_no",
            best_ask_yes=0.390,
            best_bid_yes=0.380,
            best_ask_no=0.620,
            best_bid_no=0.610
        )

        await self.handler.handle(market, self.fsm, self.ctx, tick, self.params, self.deps, self.filter_logger)

        sell_info = next(o for o in self.ctx.dual_orders if o.get("side") == "SELL")
        # 验证卖单依然维持在 0.420 初始利润价，没有提前自杀降价至 0.380
        self.assertEqual(sell_info["price"], 0.420)

    def test_liquidator_vwap_grace_extension(self):
        """测试强平引擎在买盘穿透亏损 > 5% 时给予一次性 10s 弹性缓冲"""
        now_ts = time.time()
        ctx = TradeContext(
            market_id="test_m_grace",
            status="pending_leg2",
            leg1=LegPosition(order_id="ord_l1", token="tok_yes_g", cost=0.50, size=20.0, side="BUY"),
            leg1_filled_time=now_ts - 95.0,
            end_time=now_ts + 40.0
        )
        grid = OrderbookMemoryGrid.get_instance()
        # 买盘极端单边穿透 (买一 0.42 < 0.50 * 0.95 = 0.475)
        grid._books["tok_yes_g"] = OrderbookSnapshot(
            token_id="tok_yes_g",
            best_bid=0.42,
            best_ask=0.44,
            bids=((0.42, 50.0),),
            asks=((0.44, 50.0),),
            last_update_ts=now_ts
        )

        client = MagicMock()
        client.is_live = False

        # 首次执行强平：触发 10s 弹性缓冲，返回 False
        res, price, size, oid = AdaptiveLiquidatorService.execute_force_close(client, ctx, "test_strat", allow_grace=True)
        self.assertFalse(res)
        self.assertTrue(ctx.ttl_grace_extended)
        self.assertEqual(ctx.dynamic_ttl, 100.0)

        # 第二次执行强平：缓冲已用尽，强制执行 FOK 平仓
        res2, price2, size2, oid2 = AdaptiveLiquidatorService.execute_force_close(client, ctx, "test_strat", allow_grace=True)
        self.assertTrue(res2)
        self.assertEqual(price2, 0.42)

if __name__ == "__main__":
    unittest.main()
