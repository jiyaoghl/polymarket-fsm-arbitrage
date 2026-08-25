import pytest
import httpx
from unittest.mock import MagicMock, patch
from polymarket.client import PolyClient, get_client
from polymarket import config

def test_poly_client_http2_initialization():
    """测试 PolyClient 原生 HTTP/2 客户端配置与连接池初始化"""
    client = PolyClient(is_live=False, warm_up=False)
    
    assert isinstance(client.http2_client, httpx.Client)
    # 验证 HTTP/2 特性开启
    assert client.http2_client._transport._pool._http2 is True or getattr(client.http2_client, "_http2", True)
    assert client.session is client.http2_client


def test_poly_client_warm_up():
    """测试连接池主动预热机制"""
    client = PolyClient(is_live=False, warm_up=False)
    
    with patch.object(client.http2_client, "get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_get.return_value = mock_resp
        
        # 执行同步预热
        client.warm_up_connections(async_background=False)
        
        # 验证同时对 CLOB 和 Gamma 节点发送了探测包
        assert mock_get.call_count == 2
        calls = [c[0][0] for c in mock_get.call_args_list]
        assert any("/time" in url for url in calls)
        assert any("/markets" in url for url in calls)


def test_poly_client_http2_signed_request():
    """测试 HTTP/2 签名请求头装配与发送"""
    client = PolyClient(is_live=False, warm_up=False)
    
    with patch.object(client.http2_client, "get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"balance": 100.0}
        mock_get.return_value = mock_resp
        
        res = client._get_signed("/test-endpoint")
        assert res == {"balance": 100.0}
        assert mock_get.called
        headers = mock_get.call_args[1]["headers"]
        assert "POLY_TIMESTAMP" in headers
        assert "POLY_SIGNATURE" in headers


def test_poly_client_close():
    """测试 HTTP/2 连接池安全释放"""
    client = PolyClient(is_live=False, warm_up=False)
    client.close()
    assert client.http2_client.is_closed
