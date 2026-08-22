import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from polymarket.client import PolyClient

def test_client_v2_sign():
    os.environ["POLX_PK"] = "0x0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    from polymarket import config
    config.PK = os.environ["POLX_PK"]
    
    client = PolyClient(is_live=True)
    assert client.wallet is not None
    
    # 构造买单
    signed_buy = client._create_v2_signed_order("123456789", 0.45, 10.0, "BUY")
    assert "nonce" not in signed_buy
    assert "feeRateBps" not in signed_buy
    assert "taker" not in signed_buy
    assert signed_buy["expiration"] == "0"
    assert "timestamp" in signed_buy
    assert "signature" in signed_buy
    assert signed_buy["makerAmount"] == "4500000"
    assert signed_buy["takerAmount"] == "10000000"
    print("V2 买单结构签名验证通过:", signed_buy)

    # 构造卖单
    signed_sell = client._create_v2_signed_order("123456789", 0.45, 10.0, "SELL")
    assert signed_sell["makerAmount"] == "10000000"
    assert signed_sell["takerAmount"] == "4500000"
    print("V2 卖单结构签名验证通过:", signed_sell)

if __name__ == "__main__":
    test_client_v2_sign()
    print("All Native V2 Tests Passed!")
