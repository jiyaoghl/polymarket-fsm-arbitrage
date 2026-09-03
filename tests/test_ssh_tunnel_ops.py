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


def test_ssh_remote_executor_is_configured():
    """验证 SSH 远程执行器根据环境变量判断是否配置就绪"""
    from scripts.vps_ops import SSHRemoteExecutor
    with patch.dict(os.environ, {"VPS_SSH_PASSWORD": "mypassword"}):
        assert SSHRemoteExecutor.is_configured() is True
    with patch.dict(os.environ, {"VPS_SSH_PASSWORD": "", "VPS_SSH_KEY_FILE": ""}, clear=True):
        assert SSHRemoteExecutor.is_configured() is False


def test_ssh_remote_executor_curl_api():
    """验证通过 SSH 远程执行 curl 获取 API JSON 数据"""
    from scripts.vps_ops import SSHRemoteExecutor
    mock_json_str = '{"status": "ok", "server_time": 1234567890}'
    with patch.object(SSHRemoteExecutor, "exec_cmd", return_value=(0, mock_json_str, "")):
        data = SSHRemoteExecutor.curl_api("/api/status")
        assert data is not None
        assert data.get("status") == "ok"
        assert data.get("server_time") == 1234567890


def test_ssh_remote_executor_tail_logs():
    """验证通过 SSH 远程获取日志行"""
    from scripts.vps_ops import SSHRemoteExecutor
    mock_logs = "2026-09-03 14:00:00 | INFO | trade 1\n2026-09-03 14:00:01 | INFO | trade 2\n"
    with patch.object(SSHRemoteExecutor, "exec_cmd", return_value=(0, mock_logs, "")):
        lines = SSHRemoteExecutor.tail_logs(lines=2)
        assert len(lines) == 2
        assert "trade 1" in lines[0]


def test_fetch_api_fallback_to_ssh_when_http_fails():
    """验证当 HTTP 失败时，fetch_api 自动无缝降级到 SSH 密码通道"""
    from scripts.vps_ops import fetch_api, SSHRemoteExecutor
    mock_api_data = {"status": "ok", "from_ssh": True}
    with patch("requests.get", side_effect=Exception("Connection timed out")):
        with patch.object(SSHRemoteExecutor, "is_configured", return_value=True):
            with patch.object(SSHRemoteExecutor, "curl_api", return_value=mock_api_data):
                res = fetch_api("/api/status")
                assert res is not None
                assert res.get("from_ssh") is True

