import pytest
from polymarket.client import get_client

def test_get_market_price_method_exists():
    """测试客户端获取市场价格接口方法规范"""
    client = get_client(is_live=False)
    assert hasattr(client, "get_market_price")
    assert callable(client.get_market_price)
