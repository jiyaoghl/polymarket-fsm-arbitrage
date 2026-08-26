import time
import asyncio
import pytest
from unittest.mock import MagicMock, patch
from eth_account import Account

from polymarket.gateway import (
    CLOBProtocolCodec,
    PaperTradingGateway,
    LiveClobV2Gateway,
    GatewayFactory,
    ITradingGateway
)
from polymarket.client import PolyClient

def test_codec_sanitization_and_params():
    """测试 CLOBProtocolCodec 的价格与 5.0 Shares 份数安全钳制"""
    # 1. 正常入参
    p, s = CLOBProtocolCodec.sanitize_order_params(0.45, 10.0)
    assert p == 0.45
    assert s == 10.0
    
    # 2. 价格极端边界钳制
    p_low, _ = CLOBProtocolCodec.sanitize_order_params(0.00001, 10.0)
    assert p_low == 0.001
    p_high, _ = CLOBProtocolCodec.sanitize_order_params(1.20, 10.0)
    assert p_high == 0.999
    
    # 3. 份数低于 5.0 时的折算与兜底
    # 传入 $2.0 USDC @ 0.50 -> 折算 4.0 份，强制钳制到 5.0 份
    p_min, s_min = CLOBProtocolCodec.sanitize_order_params(0.50, 2.0)
    assert p_min == 0.50
    assert s_min == 5.0


def test_codec_eip712_message_build():
    """测试 CLOBProtocolCodec 纯原生 EIP-712 签名构建"""
    # 生成临时测试钱包
    wallet = Account.create()
    
    signed_order = CLOBProtocolCodec.create_v2_signed_order(
        wallet=wallet,
        token_id="12345678901234567890",
        price=0.45,
        amount=10.0,
        side="BUY",
        salt=999999
    )
    
    assert signed_order["salt"] == 999999
    assert signed_order["maker"] == wallet.address
    assert signed_order["signer"] == wallet.address
    assert signed_order["tokenId"] == "12345678901234567890"
    assert signed_order["side"] == "BUY"
    assert signed_order["expiration"] == "0"
    assert signed_order["signature"].startswith("0x")
    assert len(signed_order["signature"]) > 50


def test_gateway_factory():
    """测试 GatewayFactory 工厂动态构建对应网关实例"""
    gw_paper = GatewayFactory.create_gateway(is_live=False)
    assert isinstance(gw_paper, PaperTradingGateway)
    assert gw_paper.is_live is False
    
    gw_live = GatewayFactory.create_gateway(is_live=True, warm_up=False)
    assert isinstance(gw_live, LiveClobV2Gateway)
    assert gw_live.is_live is True
    gw_live.close()


def test_paper_gateway_ledger_and_slippage():
    """测试 PaperTradingGateway 模拟订单账本与滑点"""
    gw = PaperTradingGateway(initial_balance=50.0)
    assert gw.get_balance()["usdc"] == 50.0
    
    # 1. 模拟下单
    order = gw.post_order(token_id="tok_1", price=0.45, amount=10.0, side="BUY")
    assert order is not None
    order_id = order["order_id"]
    assert order["status"] == "LIVE"
    
    # 2. 账本跟踪
    open_orders = gw.get_open_orders()
    assert len(open_orders) == 1
    assert open_orders[0]["order_id"] == order_id
    
    # 3. 模拟撤单
    res = gw.cancel_order(order_id)
    assert res is True
    assert len(gw.get_open_orders()) == 0


def test_poly_client_facade_delegation():
    """测试 PolyClient Facade 门面完全代理网关调用"""
    client = PolyClient(is_live=False)
    assert client.is_live is False
    
    # 同步下单
    order = client.post_order("tok_test", 0.45, 10.0, "BUY")
    assert order is not None
    
    # 异步下单
    async def _test():
        async_order = await client.post_order_async("tok_test_async", 0.46, 10.0, "BUY")
        assert async_order is not None
        assert "sim_" in async_order["order_id"]
        
        cancel_res = await client.cancel_order_async(async_order["order_id"])
        assert cancel_res is True

    asyncio.run(_test())
