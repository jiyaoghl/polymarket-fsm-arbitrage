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


def test_render_progress_bar():
    from polymarket.services.discord_bot import render_progress_bar
    bar_0 = render_progress_bar(0, 100, length=8)
    assert bar_0 == "[░░░░░░░░] 0.0%"
    bar_50 = render_progress_bar(50, 100, length=8)
    assert bar_50 == "[▓▓▓▓░░░░] 50.0%"
    bar_100 = render_progress_bar(100, 100, length=8)
    assert bar_100 == "[▓▓▓▓▓▓▓▓] 100.0%"
    bar_zero_div = render_progress_bar(10, 0, length=8)
    assert bar_zero_div == "[░░░░░░░░] 0.0%"


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
    assert len(custom_ids) == 9
    assert "btn_refresh_status" in custom_ids
    assert "btn_view_balance" in custom_ids
    assert "btn_view_strategies" in custom_ids
    assert "btn_view_markets" in custom_ids
    assert "btn_view_logs" in custom_ids
    assert "btn_onchain_redeem" in custom_ids
    assert "btn_emergency_pause" in custom_ids
    assert "btn_resume_trading" in custom_ids
    assert "btn_clean_history" in custom_ids


def test_format_ansi_logs():
    from polymarket.services.discord_bot import format_ansi_logs
    raw = [
        "13:25:00 | ERROR | poly_bot | [LiveGateway] 下单失败: 401 Unauthorized",
        "13:25:01 | WARNING | poly_bot | [RiskManager] 拦截开仓: 波动率过大",
        "13:25:02 | INFO | poly_bot | [FSM] 状态流转: LOCKED 锁仓成功",
        "13:25:03 | INFO | poly_bot | 普通调试日志"
    ]
    ansi_res = format_ansi_logs(raw)
    assert "\u001b[31m" in ansi_res  # 包含红色
    assert "\u001b[33m" in ansi_res  # 包含黄色
    assert "\u001b[32m" in ansi_res  # 包含绿色


def test_confirm_clean_view_structure():
    from polymarket.services.discord_bot import ConfirmCleanHistoryView, HAS_DISCORD_LIB
    if not HAS_DISCORD_LIB:
        return
    view = ConfirmCleanHistoryView()
    assert hasattr(view, "children")
    custom_ids = [btn.custom_id for btn in view.children]
    assert "btn_confirm_clean_yes" in custom_ids
    assert "btn_confirm_clean_cancel" in custom_ids
    assert view.timeout == 30.0


def test_reset_startup_logs():
    import tempfile
    from pathlib import Path
    from unittest.mock import patch
    from polymarket.logger import reset_startup_logs

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        with patch("polymarket.paths.logs_dir", return_value=temp_path):
            trade_log = temp_path / "trade.log"
            trade_log.write_text("old legacy log line 1\nold legacy log line 2\n", encoding="utf-8")
            assert trade_log.exists()
            assert trade_log.stat().st_size > 0

            # 执行重启日志清理
            reset_startup_logs()

            # 验证原文件已被清空为 0 字节
            assert trade_log.exists()
            assert trade_log.stat().st_size == 0

            # 验证 archive 目录下生成了归档备份
            archive_dir = temp_path / "archive"
            assert archive_dir.exists()
            archived_files = list(archive_dir.glob("trade_*.log"))
            assert len(archived_files) == 1
            assert "old legacy log" in archived_files[0].read_text(encoding="utf-8")






