import pytest
from unittest.mock import MagicMock

from polymarket.strategy import ArbitrageBot
from polymarket.client import PolyClient


def test_vwap_and_depth_check_success():
    """测试 Orderbook 深度充足且 VWAP 滑点在容忍度内的场景。"""
    asks = [
        {"price": "0.45", "size": "10.0"},  # 4.5 USDC
        {"price": "0.46", "size": "20.0"},  # 9.2 USDC
    ]

    is_valid, vwap, filled_usdc, reason = ArbitrageBot._check_orderbook_depth_and_vwap(
        asks=asks,
        target_usdc_amount=10.0,
        max_price_threshold=0.50,
        max_slippage_tolerance=0.015,
    )

    assert is_valid is True
    assert 0.45 < vwap < 0.46
    assert filled_usdc == 10.0
    assert reason == "OK"


def test_vwap_slippage_exceeded():
    """测试当盘口深度薄且跨度大导致 VWAP 滑点超标的场景。"""
    asks = [
        {"price": "0.40", "size": "2.0"},   # 0.8 USDC
        {"price": "0.50", "size": "20.0"},  # 10.0 USDC
    ]

    is_valid, vwap, filled_usdc, reason = ArbitrageBot._check_orderbook_depth_and_vwap(
        asks=asks,
        target_usdc_amount=10.0,
        max_price_threshold=0.50,
        max_slippage_tolerance=0.015,
    )

    assert is_valid is False
    assert "滑点" in reason or "超出" in reason


def test_insufficient_depth():
    """测试盘口 Ask 总深度低于拟投入金额的场景。"""
    asks = [
        {"price": "0.45", "size": "5.0"},  # 2.25 USDC total
    ]

    is_valid, vwap, filled_usdc, reason = ArbitrageBot._check_orderbook_depth_and_vwap(
        asks=asks,
        target_usdc_amount=10.0,
        max_price_threshold=0.50,
    )

    assert is_valid is False
    assert "深度不足" in reason
    assert filled_usdc == 2.25


def test_unhedged_trade_counter():
    """测试 ArbitrageBot 统计未对冲单腿数量的方法。"""
    strategy_cfg = {
        "strategy_id": "test_bot",
        "name": "测试策略",
        "amount": 10.0,
        "entry_max_price": 0.50,
        "entry_min_price": 0.25,
        "reentry_trigger": 0.40,
        "is_live": False,
        "leg1_order_type": "FOK",
        "leg2_order_type": "GTC",
        "leg2_price_mode": "bid",
        "exit_mode": "dual_exit",
        "initial_margin": 0.025,
        "breakeven_margin": 0.002,
        "flip_timeout_sec": 35.0,
        "leg2_cancel_before_expiry": 30,
        "leg2_fallback_to_maker": True,
    }
    bot = ArbitrageBot(strategy_cfg)

    # 初始状态为 0
    assert bot._get_unhedged_trade_count() == 0

    # 添加一个单腿交易
    bot._set_trade("m1", {"status": "leg1_only"})
    assert bot._get_unhedged_trade_count() == 1

    # 添加另一个已锁定对冲交易
    bot._set_trade("m2", {"status": "locked"})
    assert bot._get_unhedged_trade_count() == 1

    # 添加第二个未对冲交易
    bot._set_trade("m3", {"status": "monitoring"})
    assert bot._get_unhedged_trade_count() == 2


def test_client_post_batch_orders_simulated():
    """测试 PolyClient 的 post_batch_orders 模拟模式。"""
    client = PolyClient(is_live=False)
    orders = [
        {"token_id": "t1", "price": 0.45, "amount": 10.0, "side": "BUY"},
        {"token_id": "t2", "price": 0.38, "amount": 10.0, "side": "BUY"},
    ]

    result = client.post_batch_orders(orders)
    assert result is not None
    assert result.get("status") == "SIMULATED"
    assert len(result.get("orders", [])) == 2
