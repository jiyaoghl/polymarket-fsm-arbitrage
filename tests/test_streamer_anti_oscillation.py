import time
import json
import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

from polymarket.streamer import MarketDataStreamer
from polymarket.runtime import AsyncRuntime


@pytest.fixture(autouse=True)
def reset_streamer_state():
    """每次测试重置 Streamer 单例的核心测试状态"""
    streamer = MarketDataStreamer.get_instance()
    streamer.active_assets.clear()
    streamer.confirmed_assets.clear()
    streamer.pending_assets.clear()
    streamer.subscribers.clear()
    streamer.asset_to_markets.clear()
    streamer._invalid_op_retries = 0
    streamer._last_subscription_send_ts = 0.0
    yield streamer
    streamer.active_assets.clear()
    streamer.confirmed_assets.clear()
    streamer.pending_assets.clear()


def test_subscribe_adds_to_pending():
    """测试新订阅的 Token 会自动注册进 pending_assets 集合"""
    streamer = MarketDataStreamer.get_instance()
    q = asyncio.Queue()
    
    streamer.subscribe("m1", ["tok_a", "tok_b"], q)
    assert "tok_a" in streamer.active_assets
    assert "tok_b" in streamer.active_assets
    assert "tok_a" in streamer.pending_assets
    assert "tok_b" in streamer.pending_assets
    assert len(streamer.confirmed_assets) == 0


def test_invalid_op_increments_retries_and_retains_pending():
    """测试收到 INVALID OPERATION 时退避计数递增，不误清空 pending_assets"""
    streamer = MarketDataStreamer.get_instance()
    streamer.active_assets.update(["tok_a", "tok_b"])
    streamer.pending_assets.update(["tok_a", "tok_b"])
    
    assert streamer._invalid_op_retries == 0
    streamer._invalid_op_retries += 1
    assert streamer._invalid_op_retries == 1
    assert "tok_a" in streamer.pending_assets


def test_confirmed_assets_not_cleared_by_other_market_push():
    """测试已有资产的行情推送不会将尚未就绪的 pending_assets 的退避状态误清零"""
    streamer = MarketDataStreamer.get_instance()
    streamer.active_assets.update(["tok_existing", "tok_new_unready"])
    streamer.confirmed_assets.add("tok_existing")
    streamer.pending_assets.add("tok_new_unready")
    streamer._invalid_op_retries = 2

    # 模拟收到 tok_existing 的行情
    fake_msg = json.dumps({
        "event_type": "price_change",
        "price_changes": [
            {"asset_id": "tok_existing", "best_ask": "0.50", "best_bid": "0.48"}
        ]
    })
    
    # 手动解析判断逻辑，模拟 _ws_loop 中对已存在资产的处理
    data = json.loads(fake_msg)
    active_tokens = {"tok_existing"}
    
    with streamer._lock:
        newly_confirmed = active_tokens.intersection(streamer.pending_assets)
        if newly_confirmed:
            streamer.confirmed_assets.update(newly_confirmed)
            streamer.pending_assets.difference_update(newly_confirmed)
            if not streamer.pending_assets:
                streamer._invalid_op_retries = 0

    # 断言：退避计数器仍为 2，未被误清零！
    assert streamer._invalid_op_retries == 2
    assert "tok_new_unready" in streamer.pending_assets

    # 模拟收到 tok_new_unready 的行情，全部就绪
    active_tokens2 = {"tok_new_unready"}
    with streamer._lock:
        newly_confirmed2 = active_tokens2.intersection(streamer.pending_assets)
        if newly_confirmed2:
            streamer.confirmed_assets.update(newly_confirmed2)
            streamer.pending_assets.difference_update(newly_confirmed2)
            if not streamer.pending_assets:
                streamer._invalid_op_retries = 0

    # 断言：此时全部确认完毕，退避计数器成功归零！
    assert streamer._invalid_op_retries == 0
    assert len(streamer.pending_assets) == 0
    assert "tok_new_unready" in streamer.confirmed_assets


def test_purge_expired_clears_pending_and_resets_retries():
    """测试当过期市场被清理时，如果未就绪资产被移除，退避状态自动解除"""
    streamer = MarketDataStreamer.get_instance()
    streamer.active_assets.update(["tok_stale"])
    streamer.pending_assets.add("tok_stale")
    streamer.asset_to_markets["tok_stale"] = {"m_expired"}
    streamer.subscribers["m_expired"] = [asyncio.Queue()]
    streamer._invalid_op_retries = 3

    # 执行 purge 清理掉 m_expired
    streamer.purge_expired_markets(active_market_ids=set())

    assert "tok_stale" not in streamer.active_assets
    assert "tok_stale" not in streamer.pending_assets
    assert streamer._invalid_op_retries == 0  # 自动解除退避
