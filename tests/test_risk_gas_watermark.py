import pytest
from unittest.mock import MagicMock
from polymarket.risk_manager import RiskManager


@pytest.fixture
def risk_mgr():
    """获取 RiskManager 实例并重置测试状态"""
    rm = RiskManager()
    rm.is_gas_starved = False
    rm.pol_balance = 1.0
    rm.live_max_exposure = 100.0
    rm.live_used_exposure = 0.0
    rm.locked_orders.clear()
    rm.locked_is_live.clear()
    rm.active_market_occupants.clear()
    rm.last_balance_refresh = 0.0
    return rm


def test_gas_starvation_triggers_circuit_breaker(risk_mgr):
    """测试当 POL < 0.1 时触发实盘保护性熔断并拦截新开仓"""
    mock_client = MagicMock()
    mock_client.is_live = True
    # 模拟 POL 余额仅为 0.05
    mock_client.get_balance.return_value = {"usdc": 100.0, "pol": 0.05}

    risk_mgr.refresh_balance_from_chain(mock_client, min_interval=0.0)
    
    assert risk_mgr.pol_balance == 0.05
    assert risk_mgr.is_gas_starved is True

    # 尝试申请实盘开仓额度 -> 必须被熔断拦截
    acquired = risk_mgr.acquire_trade_lock("test_strat", "m_gas_test", 20.0, is_live=True)
    assert acquired is False
    assert risk_mgr.total_intercepted_count >= 1


def test_sufficient_gas_permits_trade_lock(risk_mgr):
    """测试当 POL 充足 (>= 0.5) 时正常放行实盘开仓"""
    mock_client = MagicMock()
    mock_client.is_live = True
    mock_client.get_balance.return_value = {"usdc": 100.0, "pol": 1.5}

    risk_mgr.refresh_balance_from_chain(mock_client, min_interval=0.0)

    assert risk_mgr.pol_balance == 1.5
    assert risk_mgr.is_gas_starved is False

    acquired = risk_mgr.acquire_trade_lock("test_strat", "m_gas_test2", 20.0, is_live=True)
    assert acquired is True
