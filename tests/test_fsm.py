import time
import pytest
from polymarket.fsm import TradeFSM, TradeState, FSMTransitionError

def test_fsm_valid_transitions():
    """测试合法的状态跳转，并验证回调钩子被正确触发。"""
    fsm = TradeFSM(market_id="test_market_1", initial_state=TradeState.IDLE)
    
    hook_called = False
    
    def on_pending(fsm_instance, **kwargs):
        nonlocal hook_called
        hook_called = True
        assert kwargs.get("order_id") == "12345"

    fsm.register_transition_hook(TradeState.PENDING_LEG1, on_pending)
    
    # IDLE -> PENDING_LEG1 (合法)
    success = fsm.transition_to(TradeState.PENDING_LEG1, order_id="12345")
    assert success is True
    assert fsm.current_state == TradeState.PENDING_LEG1
    assert hook_called is True
    
    # PENDING_LEG1 -> LEG1_ONLY (合法)
    success = fsm.transition_to(TradeState.LEG1_ONLY)
    assert success is True
    assert fsm.current_state == TradeState.LEG1_ONLY

def test_fsm_invalid_transitions():
    """测试非法的状态跳转被拦截，不改变当前状态。"""
    fsm = TradeFSM(market_id="test_market_2", initial_state=TradeState.IDLE)
    
    # IDLE 不能直接跳到 LOCKED
    success = fsm.transition_to(TradeState.LOCKED)
    assert success is False
    assert fsm.current_state == TradeState.IDLE
    
    # IDLE 可以跳到 FAILED
    success = fsm.transition_to(TradeState.FAILED)
    assert success is True
    
    # FAILED 是终态，不能跳出
    success = fsm.transition_to(TradeState.IDLE)
    assert success is False
    assert fsm.current_state == TradeState.FAILED
