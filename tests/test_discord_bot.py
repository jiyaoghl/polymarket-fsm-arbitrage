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


def test_bot_commands_execution_mocked():
    import asyncio
    async def _test():
        from polymarket.services.discord_bot import HAS_DISCORD_LIB
        if not HAS_DISCORD_LIB:
            return  # 若测试环境未安装 discord.py 则安全返回

        with patch("polymarket.services.discord_bot.DISCORD_ADMIN_IDS", ["12345"]):
            bot_service = DiscordInteractiveBot(token="mock_token_123", prefix="!")
            if not bot_service.bot:
                return

            # 模拟上下文 Context
            mock_ctx = MagicMock()
            mock_ctx.send = AsyncMock()
            mock_ctx.author.id = 12345

            # 模拟非管理员 Context
            mock_non_admin_ctx = MagicMock()
            mock_non_admin_ctx.send = AsyncMock()
            mock_non_admin_ctx.author.id = 99999

            # 1. 测试 help 指令
            help_cmd = bot_service.bot.get_command("help")
            if help_cmd:
                await help_cmd.callback(mock_ctx)
                assert mock_ctx.send.called

            # 2. 测试 status 指令
            status_cmd = bot_service.bot.get_command("status")
            if status_cmd:
                await status_cmd.callback(mock_ctx)
                assert mock_ctx.send.called

            # 3. 测试 balance 指令
            balance_cmd = bot_service.bot.get_command("balance")
            if balance_cmd:
                await balance_cmd.callback(mock_ctx)
                assert mock_ctx.send.called

            # 4. 测试非管理员触发 clean 被拦截
            clean_cmd = bot_service.bot.get_command("clean")
            if clean_cmd:
                await clean_cmd.callback(mock_non_admin_ctx)
                assert "权限不足" in mock_non_admin_ctx.send.call_args[0][0]

            # 5. 测试非管理员触发 pause / resume 被拦截
            pause_cmd = bot_service.bot.get_command("pause")
            if pause_cmd:
                await pause_cmd.callback(mock_non_admin_ctx)
                assert "权限不足" in mock_non_admin_ctx.send.call_args[0][0]

    asyncio.run(_test())


