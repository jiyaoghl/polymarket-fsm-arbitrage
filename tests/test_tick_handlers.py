import pytest
import time
import asyncio
from unittest.mock import MagicMock, AsyncMock

from polymarket.domain.fsm import TradeFSM, TradeState
from polymarket.domain.models import TradeContext, LegPosition
from polymarket.services.handlers import (
    StrategyParams,
    StrategyDependencies,
    TickBundle,
    TickFilterLogger,
    IdleTickHandler,
    PendingBothLegsTickHandler,
    Leg1OnlyTickHandler,
    PendingLeg2TickHandler,
    MarketTickDispatcher,
)

def create_mock_dependencies():
    """创建隔离的依赖项 Mock"""
    client = MagicMock()
    client.post_order_async = AsyncMock()
    client.post_batch_orders_async = AsyncMock()
    client.cancel_order_async = AsyncMock()
    
    risk_manager = MagicMock()
    risk_manager.acquire_trade_lock.return_value = True
    risk_manager.is_market_occupied.return_value = (False, None)
    
    repository = MagicMock()
    
    trades = {}
    
    def get_trade(m_id):
        return trades.get(m_id)
    
    def set_trade(m_id, data):
        trades[m_id] = data
        
    def add_trade_event(m_id, state, msg):
        pass
        
    def update_trade_status(m_id, status=None, **kwargs):
        if m_id in trades:
            if status:
                trades[m_id]["status"] = status
            trades[m_id].update(kwargs)
            
    def get_unhedged_count():
        return 0
        
    deps = StrategyDependencies(
        client=client,
        risk_manager=risk_manager,
        repository=repository,
        get_trade=get_trade,
        set_trade=set_trade,
        add_trade_event=add_trade_event,
        update_trade_status=update_trade_status,
        get_unhedged_count=get_unhedged_count,
    )
    return deps, trades

def create_sample_params(**kwargs):
    """创建测试策略参数"""
    default_kwargs = dict(
        strategy_id="test_strat",
        amount=5.0,
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
        exit_mode="smart_flip",
        initial_margin=0.025,
        breakeven_margin=0.002,
        flip_timeout_sec=15.0,
        min_time_to_expiry_entry=45.0,
    )
    default_kwargs.update(kwargs)
    return StrategyParams(**default_kwargs)


def test_idle_handler_spread_filter():
    """测试 IdleTickHandler 正确拦截买卖价差过大的流动性真空盘口"""
    async def _test():
        handler = IdleTickHandler()
        deps, trades = create_mock_dependencies()
        params = create_sample_params()
        filter_logger = TickFilterLogger(params.strategy_id)
        
        market = {"id": "m_spread", "__asset_type": "BTC"}
        fsm = TradeFSM("m_spread", initial_state=TradeState.IDLE)
        ctx = TradeContext(market_id="m_spread", status=TradeState.IDLE.value, end_time=time.time() + 300)
        trades["m_spread"] = ctx.to_dict()
        
        # YES 买卖价差 0.50 - 0.40 = 0.10 > 0.05
        tick = TickBundle(
            yes_token="tok_yes",
            no_token="tok_no",
            best_ask_yes=0.50,
            best_bid_yes=0.40,
            best_ask_no=0.50,
            best_bid_no=0.48,
        )
        
        await handler.handle(market, fsm, ctx, tick, params, deps, filter_logger)
        
        assert fsm.current_state == TradeState.IDLE
        assert "买卖价差" in (ctx.filter_reason or "")

    asyncio.run(_test())


def test_idle_handler_expiry_filter():
    """测试 IdleTickHandler 正确拦截临近交割盘口"""
    async def _test():
        handler = IdleTickHandler()
        deps, trades = create_mock_dependencies()
        params = create_sample_params(min_time_to_expiry_entry=60.0)
        filter_logger = TickFilterLogger(params.strategy_id)
        
        market = {"id": "m_expiry", "__asset_type": "BTC"}
        fsm = TradeFSM("m_expiry", initial_state=TradeState.IDLE)
        # 距离交割只有 20 秒
        ctx = TradeContext(market_id="m_expiry", status=TradeState.IDLE.value, end_time=time.time() + 20)
        trades["m_expiry"] = ctx.to_dict()
        
        tick = TickBundle(
            yes_token="tok_yes",
            no_token="tok_no",
            best_ask_yes=0.45,
            best_bid_yes=0.44,
            best_ask_no=0.55,
            best_bid_no=0.54,
        )
        
        await handler.handle(market, fsm, ctx, tick, params, deps, filter_logger)
        
        assert fsm.current_state == TradeState.IDLE
        assert "临近交割" in (ctx.filter_reason or "")

    asyncio.run(_test())


def test_idle_handler_dual_bracket():
    """测试 Dual-GTC Bracket 双挂做市开仓"""
    async def _test():
        handler = IdleTickHandler()
        deps, trades = create_mock_dependencies()
        deps.client.post_batch_orders_async.return_value = {
            "status": "OK",
            "orders": [
                {"order_id": "ord_yes", "token_id": "tok_yes", "price": 0.451, "size": 10.0},
                {"order_id": "ord_no", "token_id": "tok_no", "price": 0.534, "size": 10.0},
            ]
        }
        params = create_sample_params(dual_bracket_entry=True, leg1_order_type="GTC", leg2_order_type="GTC")
        filter_logger = TickFilterLogger(params.strategy_id)
        
        market = {"id": "m_dual", "__asset_type": "ETH"}
        fsm = TradeFSM("m_dual", initial_state=TradeState.IDLE)
        ctx = TradeContext(market_id="m_dual", status=TradeState.IDLE.value, end_time=time.time() + 300)
        trades["m_dual"] = ctx.to_dict()
        
        tick = TickBundle(
            yes_token="tok_yes",
            no_token="tok_no",
            best_ask_yes=0.46,
            best_bid_yes=0.45,
            best_ask_no=0.55,
            best_bid_no=0.54,
        )
        
        await handler.handle(market, fsm, ctx, tick, params, deps, filter_logger)
        
        assert fsm.current_state == TradeState.PENDING_BOTH_LEGS
        assert deps.client.post_batch_orders_async.called

    asyncio.run(_test())


def test_pending_both_handler_both_filled():
    """测试 PendingBothLegsTickHandler 双挂单双边均成交直接进入 LOCKED"""
    async def _test():
        handler = PendingBothLegsTickHandler()
        deps, trades = create_mock_dependencies()
        params = create_sample_params(is_live=False)
        filter_logger = TickFilterLogger(params.strategy_id)
        
        market = {"id": "m_both", "__asset_type": "BTC"}
        fsm = TradeFSM("m_both", initial_state=TradeState.PENDING_BOTH_LEGS)
        ctx = TradeContext(
            market_id="m_both",
            status=TradeState.PENDING_BOTH_LEGS.value,
            dual_orders=[
                {"order_id": "ord_yes", "token_id": "tok_yes", "price": 0.45, "size": 10.0},
                {"order_id": "ord_no", "token_id": "tok_no", "price": 0.52, "size": 10.0},
            ],
            end_time=time.time() + 300
        )
        trades["m_both"] = ctx.to_dict()
        
        # 模拟盘买一价同时达到挂单价 (0.46 >= 0.45, 0.53 >= 0.52)
        tick = TickBundle(
            yes_token="tok_yes",
            no_token="tok_no",
            best_ask_yes=0.46,
            best_bid_yes=0.46,
            best_ask_no=0.54,
            best_bid_no=0.53,
        )
        
        await handler.handle(market, fsm, ctx, tick, params, deps, filter_logger)
        
        assert fsm.current_state == TradeState.LOCKED
        assert ctx.settlement_type == "HEDGED_LOCKED"
        assert ctx.profit_usdc > 0

    asyncio.run(_test())


def test_leg1_only_handler_smart_flip():
    """测试 Leg1OnlyTickHandler 在 Smart Flip 模式下发送限价做 T 卖单"""
    async def _test():
        handler = Leg1OnlyTickHandler()
        deps, trades = create_mock_dependencies()
        deps.client.post_order_async.return_value = {
            "status": "OK",
            "orderID": "sell_ord_123"
        }
        params = create_sample_params(exit_mode="smart_flip")
        filter_logger = TickFilterLogger(params.strategy_id)
        
        market = {"id": "m_leg1", "__asset_type": "BTC"}
        fsm = TradeFSM("m_leg1", initial_state=TradeState.LEG1_ONLY)
        ctx = TradeContext(
            market_id="m_leg1",
            status=TradeState.LEG1_ONLY.value,
            leg1=LegPosition(token="tok_yes", side="BUY", cost=0.45, size=10.0, order_id="buy_1"),
            leg1_filled_time=time.time() - 2.0,
            end_time=time.time() + 300
        )
        trades["m_leg1"] = ctx.to_dict()
        
        tick = TickBundle(
            yes_token="tok_yes",
            no_token="tok_no",
            best_ask_yes=0.48,
            best_bid_yes=0.46,
            best_ask_no=0.54,
            best_bid_no=0.52,
        )
        
        await handler.handle(market, fsm, ctx, tick, params, deps, filter_logger)
        
        assert fsm.current_state == TradeState.PENDING_LEG2
        assert ctx.leg2_order_id == "sell_ord_123"
        assert deps.client.post_order_async.called

    asyncio.run(_test())


def test_pending_leg2_handler_dual_exit_sell():
    """测试 PendingLeg2TickHandler 在 OCO 模式下卖单成交立即撤销买单并流转 SETTLED"""
    async def _test():
        handler = PendingLeg2TickHandler()
        deps, trades = create_mock_dependencies()
        params = create_sample_params(exit_mode="dual_exit", is_live=False)
        filter_logger = TickFilterLogger(params.strategy_id)
        
        market = {"id": "m_leg2", "__asset_type": "BTC"}
        fsm = TradeFSM("m_leg2", initial_state=TradeState.PENDING_LEG2)
        ctx = TradeContext(
            market_id="m_leg2",
            status=TradeState.PENDING_LEG2.value,
            leg1=LegPosition(token="tok_yes", side="BUY", cost=0.45, size=10.0, order_id="buy_1"),
            dual_orders=[
                {"order_id": "sell_ord_1", "side": "SELL", "token_id": "tok_yes", "price": 0.48, "size": 10.0},
                {"order_id": "buy_ord_2", "side": "BUY", "token_id": "tok_no", "price": 0.51, "size": 10.0},
            ],
            end_time=time.time() + 300
        )
        trades["m_leg2"] = ctx.to_dict()
        
        # 模拟盘买一价达到卖单价 (0.49 >= 0.48)
        tick = TickBundle(
            yes_token="tok_yes",
            no_token="tok_no",
            best_ask_yes=0.50,
            best_bid_yes=0.49,
            best_ask_no=0.55,
            best_bid_no=0.50,
        )
        
        await handler.handle(market, fsm, ctx, tick, params, deps, filter_logger)
        
        assert fsm.current_state == TradeState.SETTLED
        assert ctx.settlement_type == "DUAL_EXIT_SELL_SETTLED"
        assert deps.client.cancel_order_async.called

    asyncio.run(_test())


def test_dispatcher_routing():
    """测试 MarketTickDispatcher 正确按状态分发"""
    async def _test():
        dispatcher = MarketTickDispatcher()
        deps, trades = create_mock_dependencies()
        params = create_sample_params()
        filter_logger = TickFilterLogger(params.strategy_id)
        
        mock_handler = MagicMock()
        mock_handler.handle = AsyncMock()
        dispatcher.register_handler(TradeState.IDLE, mock_handler)
        
        market = {"id": "m_route"}
        fsm = TradeFSM("m_route", initial_state=TradeState.IDLE)
        ctx = TradeContext(market_id="m_route", status=TradeState.IDLE.value)
        trades["m_route"] = ctx.to_dict()
        
        tick = TickBundle(
            yes_token="tok_yes", no_token="tok_no",
            best_ask_yes=0.5, best_bid_yes=0.49, best_ask_no=0.5, best_bid_no=0.49
        )
        
        await dispatcher.dispatch(market, fsm, ctx, tick, params, deps, filter_logger)
        assert mock_handler.handle.called

    asyncio.run(_test())
