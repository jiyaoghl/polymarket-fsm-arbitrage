import pytest
import time
from unittest.mock import patch, MagicMock
from polymarket.services.live_checker import LivePreflightChecker


@pytest.fixture
def mock_checker():
    # 使用合法的 64 位测试私钥
    valid_test_pk = "0x8f7c5f7b45c43b2a6b5f4daa3f201ed91785f76bd544ba6841f5f61141abb2ef"
    return LivePreflightChecker(
        private_key=valid_test_pk,
        api_key="test_api_key",
        api_secret="test_api_secret",
        api_passphrase="test_api_passphrase"
    )


def test_credentials_check_pass(mock_checker):
    """测试合法私钥与 API 凭证校验"""
    res = mock_checker.check_credentials()
    assert res["status"] == "PASS"
    assert res["address"].startswith("0x")
    assert res["has_api_creds"] is True


def test_credentials_check_fail_when_pk_empty():
    """测试私钥为空或占位符时直接 FAIL"""
    c = LivePreflightChecker(private_key="your_private_key_here")
    res = c.check_credentials()
    assert res["status"] == "FAIL"
    assert res["address"] is None


def test_chain_balances_check(mock_checker):
    """测试通过 RPC 模拟查询链上 POL 与 USDC 余额"""
    with patch("requests.post") as mock_post:
        mock_resp1 = MagicMock()
        # POL: 1.5 POL (1.5 * 1e18 = 1500000000000000000 = 0x14d1120d7b160000)
        mock_resp1.json.return_value = {"result": hex(int(1.5 * 1e18))}

        mock_resp2 = MagicMock()
        # USDC / pUSD: 50.0 USDC (50 * 1e6 = 50000000 = 0x2faf080)
        mock_resp2.json.return_value = {"result": hex(int(50.0 * 1e6))}

        mock_post.side_effect = [mock_resp1, mock_resp2, mock_resp2, mock_resp2]

        res = mock_checker.check_chain_balances("0x6F7FFC6C636a04C1C4B5fF16d860d5bFcEc69250")
        assert res["status"] == "PASS"
        assert res["pol_balance"] == 1.5
        assert res["matic_balance"] == 1.5
        assert res["total_chain_usdc"] >= 50.0


def test_clob_collateral_and_allowance(mock_checker):
    """测试 CLOB balance-allowance 查询与判定 (allowances 字典)"""
    with patch("py_clob_client.client.ClobClient.get_balance_allowance") as mock_get_bal:
        mock_get_bal.return_value = {
            "balance": "35000000",      # 35.0 USDC
            "allowances": {
                "0xE111180000d2663C0091e4f400237545B87B996B": "115792089237316195423570985008687907853269984665640564039457584007913129639935"
            }
        }
        res = mock_checker.check_clob_collateral_and_allowance()
        assert res["status"] == "PASS"
        assert res["clob_balance_usdc"] == 35.0
        assert res["is_approved"] is True



def test_clock_and_latency_drift_warning(mock_checker):
    """测试当时钟漂移超过 500ms 时触发 WARN"""
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        # 模拟服务器时间比本地慢 1.2 秒
        mock_resp.json.return_value = {"time": time.time() - 1.2}
        mock_get.return_value = mock_resp

        res = mock_checker.check_clock_and_latency()
        assert res["status"] == "WARN"
        assert "时钟漂移偏大" in res["message"]


def test_order_roundtrip_skip(mock_checker):
    """测试跳过发单穿透探针"""
    res = mock_checker.check_order_roundtrip(skip=True)
    assert res["status"] == "SKIPPED"


def test_order_roundtrip_probe_success(mock_checker):
    """测试极低价发单与撤单穿透成功"""
    with patch.object(mock_checker, "check_clob_collateral_and_allowance", return_value={"clob_balance_usdc": 25.0}), \
         patch("polymarket.gateway.live.LiveClobV2Gateway.post_order") as mock_post, \
         patch("polymarket.gateway.live.LiveClobV2Gateway.cancel_order") as mock_cancel:

        mock_post.return_value = {"status": "LIVE", "orderID": "0x_probe_order_999"}
        mock_cancel.return_value = True

        res = mock_checker.check_order_roundtrip(skip=False)
        assert res["status"] == "PASS"
        assert res["order_id"] == "0x_probe_order_999"
        mock_cancel.assert_called_once_with("0x_probe_order_999")


def test_run_all_aggregation(mock_checker):
    """测试全量五维聚合报告输出"""
    with patch.object(mock_checker, "check_credentials", return_value={"status": "PASS", "address": "0x123"}), \
         patch.object(mock_checker, "check_clock_and_latency", return_value={"status": "PASS"}), \
         patch.object(mock_checker, "check_chain_balances", return_value={"status": "PASS"}), \
         patch.object(mock_checker, "check_clob_collateral_and_allowance", return_value={"status": "PASS"}), \
         patch.object(mock_checker, "check_order_roundtrip", return_value={"status": "SKIPPED"}):

        report = mock_checker.run_all(skip_probe=True)
        assert report["overall_status"] == "PASS"
        assert "100% 绿灯通过" in report["conclusion"]
