import unittest
from unittest.mock import MagicMock, AsyncMock
import time

from polymarket.domain.fsm import TradeFSM, TradeState
from polymarket.domain.models import TradeContext, LegPosition
from polymarket.services.handlers.leg1_only_handler import Leg1OnlyTickHandler
from polymarket.services.handlers.context import StrategyParams, StrategyDependencies, TickBundle, TickFilterLogger
from polymarket.services.liquidator import AdaptiveLiquidatorService

class TestLossPathDefenses(unittest.IsolatedAsyncioTestCase):
    """测试主亏损路径防御机制与异常容错隔离"""

    async def test_leg1_only_network_error_resilience(self):
        """测试首腿成交后挂二腿遭遇网络异常时，状态机稳健保留上下文并在下一轮允许重试"""
        handler = Leg1OnlyTickHandler()
        now_ts = time.time()

        ctx = TradeContext(
            market_id="m_loss_test_1",
            status=TradeState.LEG1_ONLY,
            leg1=LegPosition(order_id="l1", token="tok_yes", side="BUY", cost=0.38, size=10.0),
            leg1_filled_time=now_ts
        )

        fsm = TradeFSM(market_id="m_loss_test_1", initial_state=TradeState.LEG1_ONLY)
        params = StrategyParams(
            strategy_id="strat_test",
            amount=10.0,
            entry_max_price=0.40,
            entry_min_price=0.30,
            reentry_trigger=0.35,
            is_live=True,
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
        # 模拟实盘批量下发 dual_exit 订单时遭遇网络 503 报错
        client.post_batch_orders_async = AsyncMock(side_effect=RuntimeError("CLOB 503 Service Unavailable"))
        client.post_order_async = AsyncMock(side_effect=RuntimeError("CLOB 503 Service Unavailable"))
        
        risk_mgr = MagicMock()
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

        market = {
            "id": "m_loss_test_1",
            "asset": "BTC",
            "tokens": {"YES": "tok_yes", "NO": "tok_no"}
        }

        tick = TickBundle(
            yes_token="tok_yes",
            no_token="tok_no",
            best_bid_yes=0.36,
            best_ask_yes=0.38,
            best_bid_no=0.60,
            best_ask_no=0.62,
            now_ts=now_ts
        )

        # 触发 Tick，验证不会抛出未捕获异常导致崩溃
        try:
            await handler.handle(market, fsm, ctx, tick, params, deps, filter_logger)
        except Exception as e:
            self.fail(f"Leg1OnlyTickHandler 在网络异常时不应抛出崩溃异常: {e}")

        # 验证状态机依然安全保持在 LEG1_ONLY 状态，不会产生错误跳转
        self.assertEqual(fsm.current_state, TradeState.LEG1_ONLY)

    def test_liquidator_cancel_error_isolation(self):
        """测试强平时撤单网络报错不阻断市价 FOK 逃命平仓"""
        context = TradeContext(
            market_id="m_loss_test_2",
            status=TradeState.LEG1_ONLY,
            leg1=LegPosition(order_id="l1", token="tok_yes", side="BUY", cost=0.40, size=10.0),
            leg1_filled_time=time.time() - 100.0,
            dual_orders=[
                {"side": "SELL", "price": 0.42, "order_id": "ord_sell_error"},
                {"side": "BUY", "price": 0.58, "order_id": "ord_buy_ok"}
            ]
        )

        client = MagicMock()
        # 模拟在途撤单时，第一笔抛出网络异常，第二笔正常
        def mock_cancel(oid):
            if oid == "ord_sell_error":
                raise ConnectionResetError("Remote host closed connection during cancel")
            return {"status": "CANCELED"}

        client.cancel_order = MagicMock(side_effect=mock_cancel)
        # 模拟 FOK 平仓单成功发出
        client.post_order = MagicMock(return_value={"orderID": "fok_close_123", "status": "FILLED", "price": 0.39})

        success, actual_p, size, oid = AdaptiveLiquidatorService.execute_force_close(
            client=client,
            context=context,
            strategy_id="strat_test",
            allow_grace=False
        )

        # 验证平仓依然 100% 成功执行！
        self.assertTrue(success)
        self.assertEqual(oid, "fok_close_123")
        self.assertEqual(actual_p, 0.39)
        self.assertEqual(size, 10.0)

        # 验证 cancel_order 被调用了 2 次 (两次挂单均尝试了撤销)
        self.assertEqual(client.cancel_order.call_count, 2)
        # 验证 post_order 被正常调用发送 FOK 平仓
        client.post_order.assert_called_once()

if __name__ == "__main__":
    unittest.main()
