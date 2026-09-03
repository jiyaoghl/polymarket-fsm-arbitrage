import pytest
import time
import asyncio
from unittest.mock import MagicMock, AsyncMock

from polymarket.domain.fsm import TradeFSM, TradeState
from polymarket.domain.models import TradeContext, LegPosition
from polymarket.services.handlers.pending_both_handler import PendingBothLegsTickHandler
from polymarket.services.handlers.context import StrategyParams, StrategyDependencies, TickBundle, TickFilterLogger

def create_mock_dependencies():
    client = MagicMock()
    client.cancel_order_async = AsyncMock()
    client.is_live = False
    
    risk_manager = MagicMock()
    risk_manager.release_trade_lock = MagicMock()
    
    trades = {}
    def set_trade(m_id, data):
        trades[m_id] = data

    deps = StrategyDependencies(
        client=client,
        risk_manager=risk_manager,
        repository=MagicMock(),
        get_trade=lambda m_id: trades.get(m_id),
        set_trade=set_trade,
        add_trade_event=lambda m_id, state, msg: None,
        update_trade_status=lambda m_id, status=None, **kwargs: None,
        get_unhedged_count=lambda: 0,
    )
    return deps, client, risk_manager

def create_sample_params(**kwargs):
    default_kwargs = dict(
        strategy_id="maker_maker_standard",
        amount=20.0,
        entry_max_price=0.45,
        entry_min_price=0.28,
        reentry_trigger=0.39,
        is_live=False,
        leg1_order_type="GTC",
        leg2_order_type="GTC",
        leg2_price_mode="bid",
        dual_bracket_entry=True,
        max_slippage_tolerance=0.005,
        leg1_max_unhedged_seconds=85.0,
        max_concurrent_unhedged_trades=3,
        exit_mode="dual_exit",
        initial_margin=0.020,
        breakeven_margin=0.003,
        flip_timeout_sec=30.0,
        min_time_to_expiry_entry=45.0,
    )
    default_kwargs.update(kwargs)
    return StrategyParams(**default_kwargs)

def test_partial_fill_locks_and_cancels_remainder():
    """测试部分成交场景：挂 40 份但仅成交 15 份 (>= 5.0 份)，即时锁定 15 份并撤销剩余尾巴"""
    async def _test():
        handler = PendingBothLegsTickHandler()
        fsm = TradeFSM("test_mkt_partial", initial_state=TradeState.PENDING_BOTH_LEGS)
        ctx = TradeContext(
            market_id="test_mkt_partial",
            status="pending_both",
            end_time=time.time() + 200,
            dual_orders=[
                {"token_id": "tok_yes", "price": 0.44, "size": 40.0, "order_id": "ord_yes_1"},
                {"token_id": "tok_no", "price": 0.54, "size": 40.0, "order_id": "ord_no_1"}
            ]
        )
        ctx._sim_partial_fill_size = 15.0

        market = {"id": "test_mkt_partial"}
        tick = TickBundle(
            yes_token="tok_yes",
            no_token="tok_no",
            best_ask_yes=0.43,  # 卖一穿透，触发 YES 侧成交
            best_bid_yes=0.42,
            best_ask_no=0.56,   # NO 侧未成交
            best_bid_no=0.53,
            now_ts=time.time()
        )
        params = create_sample_params()
        deps, client, risk_manager = create_mock_dependencies()
        filter_logger = TickFilterLogger(params.strategy_id)

        await handler.handle(market, fsm, ctx, tick, params, deps, filter_logger)

        # 1. 验证状态机流转至 LEG1_ONLY
        assert fsm.current_state == TradeState.LEG1_ONLY
        
        # 2. 验证首腿持仓被精确记录为 15 份 (而非 40 份)
        assert ctx.leg1 is not None
        assert ctx.leg1.size == 15.0
        assert ctx.leg1.original_size == 40.0
        assert ctx.leg1.is_partially_filled is True
        assert ctx.leg1.cost == 0.44

        # 3. 验证对手盘 NO (ord_no_1) 与首腿剩余尾巴 (ord_yes_1) 均被撤单
        assert client.cancel_order_async.await_count == 2
        called_orders = [call.args[0] for call in client.cancel_order_async.await_args_list]
        assert "ord_no_1" in called_orders
        assert "ord_yes_1" in called_orders

        # 4. 验证释放了未使用的 25 份风控额度 (25 * 0.44 = 11.0 USDC)
        risk_manager.release_trade_lock.assert_called_once_with(
            "maker_maker_standard", "test_mkt_partial", 11.0, is_live=False
        )

    asyncio.run(_test())

def test_small_partial_fill_under_minimum_size_waits():
    """测试碎单保护场景：仅成交 2.5 份 (< 5.0 份)，系统安全保持挂单继续等待，不撤单不流转"""
    async def _test():
        handler = PendingBothLegsTickHandler()
        fsm = TradeFSM("test_mkt_small_partial", initial_state=TradeState.PENDING_BOTH_LEGS)
        ctx = TradeContext(
            market_id="test_mkt_small_partial",
            status="pending_both",
            end_time=time.time() + 200,
            dual_orders=[
                {"token_id": "tok_yes", "price": 0.44, "size": 40.0, "order_id": "ord_yes_2"},
                {"token_id": "tok_no", "price": 0.54, "size": 40.0, "order_id": "ord_no_2"}
            ]
        )
        ctx._sim_partial_fill_size = 2.5

        market = {"id": "test_mkt_small_partial"}
        tick = TickBundle(
            yes_token="tok_yes",
            no_token="tok_no",
            best_ask_yes=0.43,
            best_bid_yes=0.42,
            best_ask_no=0.56,
            best_bid_no=0.53,
            now_ts=time.time()
        )
        params = create_sample_params()
        deps, client, risk_manager = create_mock_dependencies()
        filter_logger = TickFilterLogger(params.strategy_id)

        await handler.handle(market, fsm, ctx, tick, params, deps, filter_logger)

        # 1. 验证状态机保持在 PENDING_BOTH_LEGS，不盲目流转
        assert fsm.current_state == TradeState.PENDING_BOTH_LEGS
        assert ctx.leg1 is None

        # 2. 验证未调用撤单 (保持挂单等待继续吃单)
        assert client.cancel_order_async.await_count == 0

        # 3. 验证未释放资金锁
        risk_manager.release_trade_lock.assert_not_called()

    asyncio.run(_test())

def test_full_fill_normal_transition():
    """测试全额成交场景：挂 40 份全量成交 40 份，无残留撤单，正常流转"""
    async def _test():
        handler = PendingBothLegsTickHandler()
        fsm = TradeFSM("test_mkt_full", initial_state=TradeState.PENDING_BOTH_LEGS)
        ctx = TradeContext(
            market_id="test_mkt_full",
            status="pending_both",
            end_time=time.time() + 200,
            dual_orders=[
                {"token_id": "tok_yes", "price": 0.44, "size": 40.0, "order_id": "ord_yes_3"},
                {"token_id": "tok_no", "price": 0.54, "size": 40.0, "order_id": "ord_no_3"}
            ]
        )

        market = {"id": "test_mkt_full"}
        tick = TickBundle(
            yes_token="tok_yes",
            no_token="tok_no",
            best_ask_yes=0.43,
            best_bid_yes=0.42,
            best_ask_no=0.56,
            best_bid_no=0.53,
            now_ts=time.time()
        )
        params = create_sample_params()
        deps, client, risk_manager = create_mock_dependencies()
        filter_logger = TickFilterLogger(params.strategy_id)

        await handler.handle(market, fsm, ctx, tick, params, deps, filter_logger)

        assert fsm.current_state == TradeState.LEG1_ONLY
        assert ctx.leg1.size == 40.0
        assert ctx.leg1.is_partially_filled is False
        # 只撤销对手盘 NO，无需撤销首腿
        assert client.cancel_order_async.await_count == 1
        client.cancel_order_async.assert_awaited_once_with("ord_no_3")
        risk_manager.release_trade_lock.assert_not_called()

    asyncio.run(_test())
