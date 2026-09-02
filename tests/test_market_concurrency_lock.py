import pytest
from polymarket.risk_manager import RiskManager

def test_market_concurrency_lock_lifecycle_live():
    """验证实盘模式 (is_live=True) 下强制单市场跨策略排他锁"""
    rm = RiskManager()
    rm.live_max_exposure = 100.0
    
    market_id = "test_market_exclusive_live_001"
    strat_a = "maker_maker_conservative"
    strat_b = "taker_maker_standard"
    
    # 初始状态：未被占用
    is_occ, occ = rm.is_market_occupied(market_id, strat_a, is_live=True)
    assert is_occ is False
    assert occ is None
    
    # 策略 A 申请实盘额度与排他锁
    acquired_a = rm.acquire_trade_lock(strategy_id=strat_a, market_id=market_id, amount=10.0, is_live=True)
    assert acquired_a is True
    
    # 策略 A 再次检查自身占用：未被其他策略占用
    is_occ_a, occ_a = rm.is_market_occupied(market_id, strat_a, is_live=True)
    assert is_occ_a is False
    
    # 策略 B 检查占用：已被策略 A 占用！
    is_occ_b, occ_b = rm.is_market_occupied(market_id, strat_b, is_live=True)
    assert is_occ_b is True
    assert occ_b == strat_a
    
    # 策略 A 释放全量锁
    rm.release_market_lock(strategy_id=strat_a, market_id=market_id, is_live=True)
    
    # 策略 B 再次检查占用：已解除占用
    is_occ_b2, occ_b2 = rm.is_market_occupied(market_id, strat_b, is_live=True)
    assert is_occ_b2 is False
    assert occ_b2 is None


def test_market_concurrency_lock_paper_allow_concurrent():
    """验证模拟盘模式 (is_live=False, PAPER_MARKET_LOCK_ENABLED=False) 允许多策略并发开仓演练"""
    rm = RiskManager()
    
    market_id = "test_market_paper_concurrent_001"
    strat_a = "maker_maker_conservative"
    strat_b = "taker_maker_standard"
    
    # 策略 A 申请模拟盘额度
    acquired_a = rm.acquire_trade_lock(strategy_id=strat_a, market_id=market_id, amount=10.0, is_live=False)
    assert acquired_a is True
    
    # 策略 B 检查占用：模拟盘默认不互相排他，返回未占用
    is_occ_b, occ_b = rm.is_market_occupied(market_id, strat_b, is_live=False)
    assert is_occ_b is False
    assert occ_b is None
    
    # 策略 B 也可同时成功申请模拟盘额度
    acquired_b = rm.acquire_trade_lock(strategy_id=strat_b, market_id=market_id, amount=10.0, is_live=False)
    assert acquired_b is True
