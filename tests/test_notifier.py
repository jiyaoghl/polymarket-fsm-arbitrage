import time
from unittest.mock import MagicMock, patch
import pytest

from polymarket.services.notifier import DiscordNotifier, mask_address


def test_mask_address():
    # 正常以太坊地址
    addr = "0x6F7FFCbB1234567890abcdef1234567890c69250"
    masked = mask_address(addr)
    assert masked == "0x6F7F...9250"

    # 空或极短地址
    assert mask_address(None) == "N/A"
    assert mask_address("0x123") == "0x123"


def test_severity_filter():
    notifier = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/mock/test", enabled=True)
    notifier.min_severity = "TRADE"

    # DEBUG 和 INFO 低于 TRADE 门槛
    assert notifier._check_severity("DEBUG") is False
    assert notifier._check_severity("INFO") is False

    # TRADE, WARNING, CRITICAL 高于或等于 TRADE 门槛
    assert notifier._check_severity("TRADE") is True
    assert notifier._check_severity("WARNING") is True
    assert notifier._check_severity("CRITICAL") is True


def test_embed_templates_generation():
    notifier = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/mock/test", enabled=True)

    with patch.object(notifier, "send_embed") as mock_send:
        # 1. 开仓通知
        notifier.notify_entry(
            market_id="0x_test_market_123",
            asset="BTC",
            strategy_name="标准EV策略",
            side="YES",
            price=0.45,
            shares=10.0,
            is_live=False,
            expected_ev=0.045
        )
        assert mock_send.called
        embed = mock_send.call_args[0][0]
        assert "首腿开仓吃单成功" in embed["title"]
        assert embed["fields"][0]["value"] == "`标准EV策略`"

        # 2. 做 T 止盈通知
        notifier.notify_flip_success(
            market_id="0x_test_market_123",
            asset="ETH",
            strategy_name="实盘保守策略",
            leg1_cost=0.42,
            sell_price=0.45,
            shares=10.0,
            hold_seconds=12.5,
            net_profit=0.255,
            gross_profit=0.30,
            fee_usdc=0.045,
            is_live=True
        )
        embed_flip = mock_send.call_args[0][0]
        assert "LIVE 实盘" in embed_flip["title"]
        assert "同向做 T 高抛止盈达成" in embed_flip["title"]

        # 3. 对冲锁仓通知
        notifier.notify_hedged_lock(
            market_id="0x_test_market_123",
            asset="SOL",
            strategy_name="标准双挂策略",
            leg1_cost=0.45,
            leg2_cost=0.50,
            shares=10.0,
            net_ev=0.50,
            gross_profit=0.50,
            fee_usdc=0.0,
            is_live=False
        )
        embed_lock = mock_send.call_args[0][0]
        assert "双腿对冲完成" in embed_lock["title"]

        # 4. 强平通知
        notifier.notify_force_close(
            market_id="0x_test_market_123",
            asset="BTC",
            strategy_name="实盘保守策略",
            leg1_cost=0.45,
            vwap_close_price=0.35,
            shares=10.0,
            realized_pnl=-1.045,
            hold_seconds=90.5,
            is_live=True
        )
        embed_force = mock_send.call_args[0][0]
        assert "强制市价 FOK 止损" in embed_force["title"]

        # 5. 链上赎回通知
        notifier.notify_redeemed(
            market_id="0x_test_market_123",
            asset="BTC",
            amount_usdc=10.0,
            tx_hash="0x1234567890abcdef1234567890abcdef"
        )
        embed_redeem = mock_send.call_args[0][0]
        assert "链上 CTF 合约自动结算赎回成功" in embed_redeem["title"]

        # 6. 系统启动通知
        notifier.notify_system_startup(
            strategies_count=5,
            live_strategies_count=1,
            supported_assets=["BTC", "ETH", "SOL"]
        )
        embed_start = mock_send.call_args[0][0]
        assert "Polymarket 量化交易机器人已启动就绪" in embed_start["title"]


def test_risk_alert_60s_suppression():
    notifier = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/mock/test", enabled=True)
    notifier._suppress_cache.clear()

    with patch.object(notifier, "send_embed") as mock_send:
        # 第一次触发
        notifier.notify_risk_alert(
            market_id="0x_m1",
            asset="BTC",
            strategy_name="test_strat",
            reason="单边波幅过大 (振幅 0.45%)"
        )
        assert mock_send.call_count == 1

        # 立即再次触发相同原因 -> 被 60s 防抖抑制
        notifier.notify_risk_alert(
            market_id="0x_m1",
            asset="BTC",
            strategy_name="test_strat",
            reason="单边波幅过大 (振幅 0.45%)"
        )
        assert mock_send.call_count == 1


def test_429_rate_limit_backoff():
    notifier = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/mock/test", enabled=True)

    # 模拟第一次返回 429，第二次返回 204
    mock_resp_429 = MagicMock()
    mock_resp_429.status_code = 429
    mock_resp_429.json.return_value = {"retry_after": 0.1}

    mock_resp_200 = MagicMock()
    mock_resp_200.status_code = 204

    with patch("requests.post", side_effect=[mock_resp_429, mock_resp_200]) as mock_post:
        with patch("time.sleep") as mock_sleep:
            notifier._send_with_retry({"test": "payload"}, max_retries=2)
            assert mock_post.call_count == 2
            # 确认调用了 sleep 退避
            mock_sleep.assert_called_with(0.1)
