import os
from unittest.mock import patch, MagicMock
from scripts.vps_ops import get_active_vps_host, DEFAULT_VPS_HOST


def test_get_active_vps_host_prefers_local_tunnel():
    """验证当本地 127.0.0.1:8888 隧道连通时，优先返回本地安全端点"""
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_get.return_value = mock_resp

        # 模拟未设置显式 VPS_HOST
        with patch.dict(os.environ, {}, clear=True):
            host = get_active_vps_host()
            assert host == "http://127.0.0.1:8888"


def test_get_active_vps_host_fallback_to_default_when_no_tunnel():
    """验证本地无隧道时平滑降级到默认公网地址"""
    with patch("requests.get", side_effect=Exception("Connection refused")):
        with patch.dict(os.environ, {}, clear=True):
            host = get_active_vps_host()
            assert host == DEFAULT_VPS_HOST


def test_get_active_vps_host_respects_env_override():
    """验证显式环境变量覆盖优先于探测"""
    with patch.dict(os.environ, {"VPS_HOST": "http://custom-node:9999"}):
        host = get_active_vps_host()
        assert host == "http://custom-node:9999"
