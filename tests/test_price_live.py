import pytest
from polymarket.client import get_client

def test_client_has_core_interfaces():
    """测试客户端核心接口存在性与类型"""
    client = get_client(is_live=False)
    assert callable(client.get_market_price)
    assert callable(client.post_order)
    assert callable(client.post_batch_orders)
    assert callable(client.cancel_order)
