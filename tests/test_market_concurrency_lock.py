import pytest
from polymarket.risk_manager import RiskManager

def test_market_concurrency_lock_lifecycle():
    rm = RiskManager()
    
    market_id = "test_market_exclusive_001"
    strat_a = "taker_maker_aggressive"
    strat_b = "taker_maker_standard"
    
    # 初始状态：未被占用
    is_occ, occ = rm.is_market_occupied(market_id, strat_a)
    assert is_occ is False
    assert occ is None
    
    # 策略 A 申请锁定
    acquired_a = rm.acquire_trade_lock(strategy_id=strat_a, market_id=market_id, amount=10.0, is_live=False)
    assert acquired_a is True
    
    # 策略 A 再次检查自身占用：未被其他策略占用
    is_occ_a, occ_a = rm.is_market_occupied(market_id, strat_a)
    assert is_occ_a is False
    
    # 策略 B 检查占用：已被策略 A 占用！
    is_occ_b, occ_b = rm.is_market_occupied(market_id, strat_b)
    assert is_occ_b is True
    assert occ_b == strat_a
    
    # 策略 A 释放全量锁
    rm.release_market_lock(strategy_id=strat_a, market_id=market_id, is_live=False)
    
    # 策略 B 再次检查占用：已解除占用
    is_occ_b2, occ_b2 = rm.is_market_occupied(market_id, strat_b)
    assert is_occ_b2 is False
    assert occ_b2 is None
