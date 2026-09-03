import pytest
from unittest.mock import MagicMock, patch
from polymarket.services.onchain_redeemer import (
    OnChainRedeemer, CTF_EXCHANGE_ADDRESS, USDC_BRIDGED_ADDRESS
)

def test_onchain_redeemer_init_and_rpc_rotation():
    redeemer = OnChainRedeemer(
        private_key="0x0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        rpc_candidates=["https://rpc1.example.com", "https://rpc2.example.com"]
    )
    assert redeemer.get_active_rpc() == "https://rpc1.example.com"
    new_rpc = redeemer.rotate_rpc()
    assert new_rpc == "https://rpc2.example.com"
    assert redeemer.get_active_rpc() == "https://rpc2.example.com"

def test_onchain_redeemer_format_bytes32():
    test_hex = "0x1234abcd"
    b = OnChainRedeemer.format_bytes32(test_hex)
    assert len(b) == 32
    assert b.hex() == "000000000000000000000000000000000000000000000000000000001234abcd"

def test_onchain_redeemer_encode_calldata_offline():
    redeemer = OnChainRedeemer()
    test_cond_id = "0xaa5e61862b3b5e09f62b56b1d570c34a267b01f45bac330397e1c51274156646"
    
    calldata = redeemer.encode_redeem_data(condition_id=test_cond_id)
    assert isinstance(calldata, bytes)
    assert len(calldata) > 0
    # redeemPositions(address,bytes32,bytes32,uint256[]) 标准函数选择器 0x01b7037c
    selector = calldata[:4].hex()
    assert selector == "01b7037c"

def test_onchain_redeemer_no_wallet_skipped():
    redeemer = OnChainRedeemer(private_key="your_private_key_here")
    res = redeemer.redeem_positions("0x123456")
    assert res.get("status") == "SKIPPED"

@patch("web3.Web3")
def test_onchain_redeemer_mock_success(mock_web3_class):
    dummy_pk = "0x0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    redeemer = OnChainRedeemer(private_key=dummy_pk, rpc_candidates=["https://polygon-rpc.com"])
    
    mock_w3 = MagicMock()
    mock_web3_class.return_value = mock_w3
    mock_w3.is_connected.return_value = True
    mock_w3.eth.get_transaction_count.return_value = 5
    mock_w3.eth.gas_price = 30000000000
    mock_w3.eth.send_raw_transaction.return_value = b"\x12" * 16

    mock_contract = MagicMock()
    mock_w3.eth.contract.return_value = mock_contract
    mock_contract.functions.redeemPositions.return_value.build_transaction.return_value = {
        "from": redeemer.wallet.address,
        "nonce": 5,
        "gas": 250000,
        "gasPrice": 36000000000,
        "chainId": 137
    }

    res = redeemer.redeem_positions("0xaa5e61862b3b5e09f62b56b1d570c34a267b01f45bac330397e1c51274156646")
    assert res.get("status") == "SUCCESS"
    assert "tx_hash" in res


@patch("web3.Web3")
def test_onchain_redeemer_dry_run_revert_intercepts_without_broadcasting(mock_web3_class):
    """验证当链上静态模拟预检 Revert 时，系统在本地直接拦截，绝不上链，绝不消耗 Gas"""
    dummy_pk = "0x0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    redeemer = OnChainRedeemer(private_key=dummy_pk, rpc_candidates=["https://polygon-rpc.com"])

    mock_w3 = MagicMock()
    mock_web3_class.return_value = mock_w3
    mock_w3.is_connected.return_value = True

    mock_contract = MagicMock()
    mock_w3.eth.contract.return_value = mock_contract
    # 模拟 call() 抛出异常 (比如 Condition not resolved 或零持仓)
    mock_contract.functions.redeemPositions.return_value.call.side_effect = Exception("execution reverted: Condition not resolved")

    res = redeemer.redeem_positions("0xaa5e61862b3b5e09f62b56b1d570c34a267b01f45bac330397e1c51274156646")

    assert res.get("status") == "SKIPPED"
    assert "Dry-run reverted" in res.get("reason", "")
    # 核心铁律验证：绝对没有向网络广播过真实交易
    mock_w3.eth.send_raw_transaction.assert_not_called()

