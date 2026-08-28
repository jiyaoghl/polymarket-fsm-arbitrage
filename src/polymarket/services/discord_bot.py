import asyncio
import functools
import logging
import threading
import time
from typing import Any, Dict, List, Optional

from polymarket.config import (
    DISCORD_ADMIN_IDS,
    DISCORD_BOT_TOKEN,
    DISCORD_COMMAND_PREFIX,
    SUPPORTED_ASSETS,
)

logger = logging.getLogger("poly_bot")

# 优雅尝试导入 discord.py，若缺失则平滑降级
try:
    import discord
    from discord.ext import commands
    HAS_DISCORD_LIB = True
except ImportError:
    discord = None
    commands = None
    HAS_DISCORD_LIB = False
    logger.debug("[DiscordBot] 未安装 discord.py 依赖，交互机器人功能将以静默模式降级。")


def is_admin(user_id: int) -> bool:
    """校验 Discord User ID 是否具备管理员权限。"""
    if not DISCORD_ADMIN_IDS:
        return True  # 若未配置管理员列表，则默认放行
    return str(user_id) in DISCORD_ADMIN_IDS or int(user_id) in [int(x) for x in DISCORD_ADMIN_IDS if x.isdigit()]


def generate_dashboard_embed() -> Optional[Any]:
    """生成统一的大盘状态富文本 Embed 卡片"""
    if not HAS_DISCORD_LIB or discord is None:
        return None

    from polymarket.risk_manager import RiskManager
    from polymarket.kline_analyzer import get_asset_status
    from polymarket.db import count_open_positions

    rm = RiskManager()
    status = rm.get_status()
    is_paused = getattr(rm, "is_emergency_halted", False)

    color = 0xEF4444 if is_paused else 0x10B981
    status_icon = "🔴 PAUSED (已暂停开仓)" if is_paused else "🟢 NORMAL (全天候套利中)"

    embed = discord.Embed(
        title="📊 Polymarket 实时量化监控与远程控制台",
        description=f"当前系统运行状态: **{status_icon}**",
        color=color,
        timestamp=discord.utils.utcnow()
    )

    embed.add_field(name="💰 链上真实余额", value=f"`${status.get('live_balance', 0.0):.2f}` USDC", inline=True)
    embed.add_field(name="🔒 活跃持仓总数", value=f"`{count_open_positions()}` 笔", inline=True)
    embed.add_field(name="🛡️ 实盘敞口占用", value=f"`${status.get('live_exposure', 0.0):.2f}` USDC", inline=True)
    embed.add_field(name="🔵 模拟资金池", value=f"`${status.get('paper_balance', 0.0):.2f}` USDC", inline=True)
    embed.add_field(name="📈 累计已实现盈亏", value=f"`${status.get('realized_pnl', 0.0):+.4f}` USDC", inline=True)

    # 标的波动率防爆盾状态
    chop_text = []
    for a in SUPPORTED_ASSETS:
        ast = get_asset_status(a)
        if ast.get("timestamp", 0) > 0:
            icon = "🟢 震荡" if ast.get("is_choppy") else "🔴 单边"
            chop_text.append(f"• **{a}**: 振幅 {ast.get('amplitude', 0.0):.2f}% ({icon})")
    if chop_text:
        embed.add_field(name="🪙 实时行情守门 (K线防爆盾)", value="\n".join(chop_text), inline=False)

    embed.set_footer(text="点击下方按钮直接操作 | Polymarket FSM Arbitrage Bot V2.0")
    return embed


# =============================================================================
# 持久化纯按钮交互控制面板 (Persistent Button-Driven View)
# =============================================================================

if HAS_DISCORD_LIB and discord is not None:
    class DashboardControlView(discord.ui.View):
        """
        全功能持久化交互按钮面板。
        特性：
        1. timeout=None + 静态 custom_id，VPS 重启后按钮永久有效；
        2. defer(ephemeral=True) 避免 3 秒响应超时；
        3. 原位刷新 (In-Place Edit) 零刷屏。
        """

        def __init__(self):
            super().__init__(timeout=None)

        @discord.ui.button(label="🔄 刷新大盘", style=discord.ButtonStyle.success, custom_id="btn_refresh_status", row=0)
        async def on_refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
            embed = generate_dashboard_embed()
            if embed:
                await interaction.response.edit_message(embed=embed, view=self)

        @discord.ui.button(label="💰 资金余额", style=discord.ButtonStyle.primary, custom_id="btn_view_balance", row=0)
        async def on_balance(self, interaction: discord.Interaction, button: discord.ui.Button):
            from polymarket.risk_manager import RiskManager
            rm = RiskManager()
            st = rm.get_status()

            embed = discord.Embed(
                title="💰 资金池与风控额度概况",
                color=0x3B82F6,
                timestamp=discord.utils.utcnow()
            )
            embed.add_field(name="🔴 实盘链上余额", value=f"`${st.get('live_balance', 0.0):.4f}` USDC", inline=True)
            embed.add_field(name="🔒 实盘占用敞口", value=f"`${st.get('live_exposure', 0.0):.2f}` USDC", inline=True)
            embed.add_field(name="🔵 模拟资金池", value=f"`${st.get('paper_balance', 0.0):.2f}` USDC", inline=True)
            embed.add_field(name="📈 累计已实现盈亏", value=f"`${st.get('realized_pnl', 0.0):+.4f}` USDC", inline=False)
            await interaction.response.send_message(embed=embed, ephemeral=True)

        @discord.ui.button(label="📜 最新日志", style=discord.ButtonStyle.secondary, custom_id="btn_view_logs", row=0)
        async def on_logs(self, interaction: discord.Interaction, button: discord.ui.Button):
            if not is_admin(interaction.user.id):
                await interaction.response.send_message("❌ 权限不足：只有管理员可以查看控制台日志。", ephemeral=True)
                return

            await interaction.response.defer(ephemeral=True)
            from polymarket.config import paths
            from collections import deque
            log_path = paths.logs_dir() / "trade.log"

            if not log_path.exists():
                await interaction.followup.send("❌ 日志文件不存在: `trade.log`", ephemeral=True)
                return

            try:
                with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                    tail_lines = list(deque(f, maxlen=20))
                content = "".join(tail_lines)
                if not content.strip():
                    await interaction.followup.send("📜 日志暂无内容。", ephemeral=True)
                    return

                chunks = [content[i:i + 1800] for i in range(0, len(content), 1800)]
                for chunk in chunks:
                    await interaction.followup.send(f"```text\n{chunk}\n```", ephemeral=True)
            except Exception as e:
                await interaction.followup.send(f"❌ 读取日志失败: {e}", ephemeral=True)

        @discord.ui.button(label="⏸️ 紧急暂停", style=discord.ButtonStyle.danger, custom_id="btn_emergency_pause", row=1)
        async def on_pause(self, interaction: discord.Interaction, button: discord.ui.Button):
            if not is_admin(interaction.user.id):
                await interaction.response.send_message("❌ 权限不足：只有管理员可以执行熔断操作。", ephemeral=True)
                return

            from polymarket.risk_manager import RiskManager
            rm = RiskManager()
            rm.is_emergency_halted = True
            embed = generate_dashboard_embed()
            if embed:
                await interaction.response.edit_message(embed=embed, view=self)
                await interaction.followup.send("⏸️ 已触发紧急熔断：已暂停所有策略新开仓。", ephemeral=True)

        @discord.ui.button(label="▶️ 恢复开仓", style=discord.ButtonStyle.success, custom_id="btn_resume_trading", row=1)
        async def on_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
            if not is_admin(interaction.user.id):
                await interaction.response.send_message("❌ 权限不足：只有管理员可以执行恢复操作。", ephemeral=True)
                return

            from polymarket.risk_manager import RiskManager
            rm = RiskManager()
            rm.is_emergency_halted = False
            embed = generate_dashboard_embed()
            if embed:
                await interaction.response.edit_message(embed=embed, view=self)
                await interaction.followup.send("▶️ 策略已恢复：解除暂停状态，恢复 5min 套利扫描。", ephemeral=True)

        @discord.ui.button(label="🎉 链上赎回", style=discord.ButtonStyle.primary, custom_id="btn_onchain_redeem", row=1)
        async def on_redeem(self, interaction: discord.Interaction, button: discord.ui.Button):
            if not is_admin(interaction.user.id):
                await interaction.response.send_message("❌ 权限不足：只有管理员可以触发链上赎回。", ephemeral=True)
                return

            await interaction.response.defer(ephemeral=True)
            try:
                from polymarket.services.onchain_redeemer import OnChainRedeemer
                from polymarket.client import get_client
                client = get_client(is_live=True)
                redeemer = OnChainRedeemer(client=client)
                res = redeemer.redeem_all_expired()
                await interaction.followup.send(f"🎉 链上赎回执行完毕，结果: `{res}`", ephemeral=True)
            except Exception as e:
                await interaction.followup.send(f"❌ 链上赎回执行异常: {e}", ephemeral=True)

        @discord.ui.button(label="🧹 清空历史 (需管理员)", style=discord.ButtonStyle.danger, custom_id="btn_clean_history", row=2)
        async def on_clean(self, interaction: discord.Interaction, button: discord.ui.Button):
            if not is_admin(interaction.user.id):
                await interaction.response.send_message("❌ 权限不足：只有管理员可以清空历史数据。", ephemeral=True)
                return

            await interaction.response.defer(ephemeral=True)
            try:
                from polymarket.db import clean_all_historical_trades
                counts = clean_all_historical_trades()
                embed = generate_dashboard_embed()
                if embed:
                    await interaction.message.edit(embed=embed, view=self)
                await interaction.followup.send(f"✅ 历史订单数据已彻底清空并重置！明细: `{counts}`", ephemeral=True)
            except Exception as e:
                await interaction.followup.send(f"❌ 清理失败: {e}", ephemeral=True)
else:
    class DashboardControlView:
        pass


class DiscordInteractiveBot:
    """
    Discord 纯按钮交互与远程控制机器人 (Button-Driven Interactive Bot)。
    """

    _instance: Optional["DiscordInteractiveBot"] = None
    _lock = threading.Lock()

    def __init__(self, token: Optional[str] = None, prefix: Optional[str] = None):
        raw_token = token if token is not None else DISCORD_BOT_TOKEN
        self.token = raw_token.strip().strip("'\"") if raw_token else ""
        self.prefix = prefix if prefix is not None else DISCORD_COMMAND_PREFIX
        self.bot = None
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._is_started = False

        # 检查是否为真实合法 Token (拦截占位符)
        is_valid_token = bool(
            self.token
            and not self.token.startswith("your_")
            and "GxXxXx" not in self.token
            and len(self.token) >= 30
        )

        if HAS_DISCORD_LIB and is_valid_token and discord is not None:
            intents = discord.Intents.default()
            try:
                intents.message_content = True
            except Exception:
                pass

            prefixes = [self.prefix, "!", "！", "/", "$", "p!"] if self.prefix else ["!", "！", "/"]
            unique_prefixes = list(dict.fromkeys(prefixes))
            self.bot = commands.Bot(
                command_prefix=commands.when_mentioned_or(*unique_prefixes),
                intents=intents,
                help_command=None
            )
            self._register_commands()

    @classmethod
    def get_instance(cls) -> "DiscordInteractiveBot":
        """获取全局单例机器人"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def _register_commands(self):
        """注册指令与持久化 View 绑定"""
        bot = self.bot
        if not bot:
            return

        @bot.event
        async def on_ready():
            guild_names = [f"{g.name} (ID: {g.id}, 成员数: {g.member_count})" for g in bot.guilds]
            logger.info(f"[DiscordBot] 纯按钮交互机器人已成功登录网关: {bot.user.name} (ID: {bot.user.id}) | 已加入 {len(bot.guilds)} 个服务器: {guild_names}")
            
            # 注册持久化视图，确保重启后所有卡片上的按钮永久生效
            bot.add_view(DashboardControlView())
            activity = discord.Activity(type=discord.ActivityType.watching, name="Polymarket 5min 盘口 | 点击按钮控制")
            await bot.change_presence(status=discord.Status.online, activity=activity)

            if not bot.guilds:
                logger.warning("[DiscordBot] ⚠️ 警告：机器人尚未加入任何 Discord 服务器！请访问 OAuth2 邀请链接将机器人拉入您的 Discord 群组。")
                return

            # 启动时主动向所在服务器的首个可写频道投递最新的控制台卡片
            for guild in bot.guilds:
                posted = False
                for channel in guild.text_channels:
                    perms = channel.permissions_for(guild.me)
                    if perms.send_messages and perms.embed_links:
                        try:
                            embed = generate_dashboard_embed()
                            view = DashboardControlView()
                            await channel.send("🚀 **Polymarket 量化控制台已上线**，请直接点击下方按钮进行操作：", embed=embed, view=view)
                            logger.info(f"[DiscordBot] 已自动向服务器 [{guild.name}] 的频道 [{channel.name}] 投递纯按钮控制面板！")
                            posted = True
                            break
                        except Exception as e:
                            logger.warning(f"[DiscordBot] 向频道 [{channel.name}] 投递控制台异常: {e}")
                if not posted:
                    logger.warning(f"[DiscordBot] ⚠️ 在服务器 [{guild.name}] 中未找到具备 发送消息与嵌入链接 权限的文字频道！请检查机器人角色权限。")

        @bot.event
        async def on_message(message):
            if message.author.bot:
                return
            logger.info(f"[DiscordBot] 监听到频道消息 [{message.author}]: '{message.content}'")
            content_clean = message.content.strip().lower()
            # 智能唤出：被 @ 或发送 panel/status/help/大盘/控制台 时直接投递控制面板
            trigger_words = ("!panel", "！panel", "/panel", "!status", "！status", "/status", "!help", "！help", "/help", "panel", "status", "help", "控制台", "大盘")
            if (bot.user and bot.user.mentioned_in(message)) or content_clean in trigger_words or content_clean.startswith(tuple(self.prefix)):
                embed = generate_dashboard_embed()
                view = DashboardControlView()
                await message.channel.send(embed=embed, view=view)
                return
            await bot.process_commands(message)

        @bot.command(name="panel", aliases=["status", "help", "menu", "p", "dashboard"])
        async def cmd_panel(ctx):
            """唤出纯按钮交互控制台"""
            embed = generate_dashboard_embed()
            view = DashboardControlView()
            await ctx.send(embed=embed, view=view)

    def start(self):
        """在独立后台守护线程中启动 Discord 机器人 (幂等防并发)"""
        with self._lock:
            if self._is_started:
                return  # 已经启动过，直接幂等跳过
            self._is_started = True

        if not HAS_DISCORD_LIB:
            logger.debug("[DiscordBot] 缺失 discord.py 库，跳过交互机器人启动。")
            return

        if not self.token or self.token.startswith("your_") or "GxXxXx" in self.token:
            logger.info("[DiscordBot] 未配置有效的 DISCORD_BOT_TOKEN (或为占位符)，跳过交互机器人启动。")
            return

        if not self.bot:
            logger.warning("[DiscordBot] DISCORD_BOT_TOKEN 格式不合法 (长度不足 30 字符)，跳过启动。请检查 .env 中的 Bot Token。")
            return

        def _runner():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            try:
                logger.info("[DiscordBot] 正在连接 Discord 官方网关并启动纯按钮交互机器人...")
                loop.run_until_complete(self.bot.start(self.token))
            except Exception as e:
                logger.warning(f"[DiscordBot] 机器人登录网关失败: {e} (请检查 .env 中的 DISCORD_BOT_TOKEN 是否正确无空格)")
            finally:
                try:
                    loop.close()
                except Exception:
                    pass

        self._thread = threading.Thread(target=_runner, daemon=True, name="DiscordInteractiveBotWorker")
        self._thread.start()

