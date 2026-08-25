import pytest
import asyncio
from unittest.mock import MagicMock, patch
from polymarket.risk_manager import RiskManager
from polymarket.client import PolyClient, get_client
from polymarket import config

def test_paper_default_exposure():
    """测试模拟盘默认资金池为 100U，且可正常申请与超限拦截"""
    rm = RiskManager()
    # 重置状态
    rm.paper_used_exposure = 0.0
    rm.locked_orders.clear()
    rm.locked_is_live.clear()
    
    assert rm.paper_max_exposure == 100.0
    
    # 申请 10U 模拟额度
    assert rm.acquire_trade_lock("paper_strat_1", "market_1", 10.0, is_live=False) is True
    assert rm.paper_used_exposure == 10.0
    
    # 申请 85U 模拟额度 (累计 95U <= 100U)
    assert rm.acquire_trade_lock("paper_strat_2", "market_2", 85.0, is_live=False) is True
    assert rm.paper_used_exposure == 95.0
    
    # 申请 10U 模拟额度 (累计 105U > 100U) -> 应该被拦截
    assert rm.acquire_trade_lock("paper_strat_3", "market_3", 10.0, is_live=False) is False
    assert rm.paper_used_exposure == 95.0
    
    # 释放 market_1
    rm.release_market_lock("paper_strat_1", "market_1", is_live=False)
    assert rm.paper_used_exposure == 85.0
    
    # 全量清理
    rm.release_market_lock("paper_strat_2", "market_2", is_live=False)
    assert rm.paper_used_exposure == 0.0


def test_live_exposure_from_chain_and_isolation():
    """测试实盘从链上刷新真实余额（带95%缓冲），且实盘与模拟盘双池完全隔离"""
    rm = RiskManager()
    rm.live_used_exposure = 0.0
    rm.paper_used_exposure = 0.0
    rm.locked_orders.clear()
    rm.locked_is_live.clear()
    
    # 模拟实盘客户端返回 0.14 USDC
    mock_live_client = MagicMock(spec=PolyClient)
    mock_live_client.is_live = True
    mock_live_client.get_balance.return_value = {"usdc": 0.14, "pending": 0.0}
    
    # 强制刷新实盘余额 (绕过 30s 限制)
    rm.last_balance_refresh = 0.0
    rm.refresh_balance_from_chain(mock_live_client, min_interval=0.0)
    
    # 实盘上限应为 0.14 * 0.95 = 0.133
    assert pytest.approx(rm.live_max_exposure, 0.01) == 0.133
    
    # 实盘申请 3U -> 必须被拒绝（超出实盘 0.13U 上限）
    assert rm.acquire_trade_lock("live_strat", "market_live", 3.0, is_live=True) is False
    assert rm.live_used_exposure == 0.0
    
    # 关键隔离验证：模拟盘申请 10U -> 必须成功放行（不受实盘 0.13U 限制！）
    assert rm.acquire_trade_lock("paper_strat", "market_paper", 10.0, is_live=False) is True
    assert rm.paper_used_exposure == 10.0
    assert rm.live_used_exposure == 0.0
    
    # 清理
    rm.release_market_lock("paper_strat", "market_paper", is_live=False)
    assert rm.paper_used_exposure == 0.0


def test_poly_client_balance_and_batch_async():
    """测试 PolyClient 模拟盘返回 100U，以及 post_batch_orders_async 异步方法"""
    client_paper = get_client(is_live=False)
    bal = client_paper.get_balance()
    assert bal.get("usdc") == 100.0
    
    # 测试 post_batch_orders_async
    orders = [
        {"token_id": "token_yes", "price": 0.45, "amount": 10.0, "side": "BUY", "order_type": "GTC"},
        {"token_id": "token_no", "price": 0.45, "amount": 10.0, "side": "BUY", "order_type": "GTC"}
    ]
    res = asyncio.run(client_paper.post_batch_orders_async(orders))
    assert res is not None
    assert res.get("status") == "SIMULATED"
    assert len(res.get("orders", [])) == 2
