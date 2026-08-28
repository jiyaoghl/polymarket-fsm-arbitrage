from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from polymarket.services.discord_bot import DiscordInteractiveBot, is_admin


def test_is_admin():
    # 模拟配置了管理员列表
    with patch("polymarket.services.discord_bot.DISCORD_ADMIN_IDS", ["123456789", "987654321"]):
        assert is_admin(123456789) is True
        assert is_admin("123456789") is True
        assert is_admin(987654321) is True
        assert is_admin(111222333) is False

    # 模拟未配置管理员列表 (默认放行)
    with patch("polymarket.services.discord_bot.DISCORD_ADMIN_IDS", []):
        assert is_admin(999) is True


def test_bot_graceful_degradation():
    # 测试未提供 token 时的平滑降级
    bot_service = DiscordInteractiveBot(token="", prefix="!")
    assert bot_service.bot is None

    # start 不抛异常
    bot_service.start()


def test_dashboard_embed_generation():
    from polymarket.services.discord_bot import generate_dashboard_embed, HAS_DISCORD_LIB
    if not HAS_DISCORD_LIB:
        return
    embed = generate_dashboard_embed()
    assert embed is not None
    assert "Polymarket 实时量化监控与远程控制台" in embed.title


def test_button_view_structure():
    from polymarket.services.discord_bot import DashboardControlView, HAS_DISCORD_LIB
    if not HAS_DISCORD_LIB:
        return
    view = DashboardControlView()
    assert hasattr(view, "children")
    custom_ids = [btn.custom_id for btn in view.children]
    assert "btn_refresh_status" in custom_ids
    assert "btn_view_balance" in custom_ids
    assert "btn_view_logs" in custom_ids
    assert "btn_emergency_pause" in custom_ids
    assert "btn_resume_trading" in custom_ids
    assert "btn_onchain_redeem" in custom_ids
    assert "btn_clean_history" in custom_ids



