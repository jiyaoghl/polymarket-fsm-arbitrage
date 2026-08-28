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


class DiscordInteractiveBot:
    """
    Discord 双向交互与远程控制机器人 (Interactive Command Bot)。
    
    架构特性：
    1. 零侵入与平滑降级：未配置 Token 或缺失依赖时不占用任何资源；
    2. 独立异步事件循环：在独立后台守护线程中运行，绝不阻塞主交易线程；
    3. 管理员鉴权：高危写操作 (clean, pause, redeem) 严格鉴权；
    4. 2000 字符分片保护：长文本自动分块发送。
    """

    _instance: Optional["DiscordInteractiveBot"] = None
    _lock = threading.Lock()

    def __init__(self, token: Optional[str] = None, prefix: Optional[str] = None):
        self.token = token if token is not None else DISCORD_BOT_TOKEN
        self.prefix = prefix if prefix is not None else DISCORD_COMMAND_PREFIX
        self.bot = None
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        if HAS_DISCORD_LIB and self.token:
            intents = discord.Intents.default()
            intents.message_content = True
            self.bot = commands.Bot(command_prefix=self.prefix, intents=intents, help_command=None)
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
        """注册所有前缀指令"""
        bot = self.bot
        if not bot:
            return

        @bot.event
        async def on_ready():
            logger.info(f"[DiscordBot] 交互机器人已成功登录: {bot.user.name} (ID: {bot.user.id})")
            activity = discord.Activity(type=discord.ActivityType.watching, name=f"Polymarket 5min 盘口 | {self.prefix}help")
            await bot.change_presence(status=discord.Status.online, activity=activity)

        @bot.command(name="help")
        async def cmd_help(ctx):
            """调出帮助菜单"""
            embed = discord.Embed(
                title="🤖 Polymarket 交互控制机器人指令手册",
                description=f"使用前缀 `{self.prefix}` 发送以下指令进行远程监控与运维：",
                color=0x3B82F6
            )
            embed.add_field(name=f"📊 `{self.prefix}status`", value="查询当前实时大盘、活跃 5min 盘口与各策略盈亏", inline=False)
            embed.add_field(name=f"💰 `{self.prefix}balance`", value="查询链上真实 USDC 余额与风控已用/剩余额度", inline=False)
            embed.add_field(name=f"🧹 `{self.prefix}clean`", value="⚠️ 【管理员】一键清空 VPS 历史交易数据并重置大盘", inline=False)
            embed.add_field(name=f"⏸️ `{self.prefix}pause`", value="⚠️ 【管理员】紧急熔断：暂停所有策略新市场开仓", inline=False)
            embed.add_field(name=f"▶️ `{self.prefix}resume`", value="⚠️ 【管理员】恢复开仓：解除暂停状态", inline=False)
            embed.add_field(name=f"🎉 `{self.prefix}redeem`", value="⚠️ 【管理员】手动触发链上已到期市场结算赎回", inline=False)
            embed.add_field(name=f"📜 `{self.prefix}logs [n]`", value="⚠️ 【管理员】查看 VPS 最新 N 行交易控制台日志", inline=False)
            embed.set_footer(text="Polymarket FSM Arbitrage Bot V2.0")
            await ctx.send(embed=embed)

        @bot.command(name="status")
        async def cmd_status(ctx):
            """查询大盘总览"""
            from polymarket.risk_manager import RiskManager
            from polymarket.kline_analyzer import get_asset_status
            from polymarket.db import count_open_positions
            
            rm = RiskManager()
            status = rm.get_status()
            
            embed = discord.Embed(
                title="📊 Polymarket 实时运行与量化大盘状态",
                color=0x10B981,
                timestamp=discord.utils.utcnow()
            )
            embed.add_field(name="💰 链上真实余额", value=f"`${status.get('live_balance', 0.0):.2f}` USDC", inline=True)
            embed.add_field(name="🔒 活跃持仓总数", value=f"`{count_open_positions()}` 笔", inline=True)
            embed.add_field(name="🛡️ 风控状态", value=f"`{'PAUSED (已暂停)' if rm.is_emergency_halted else 'NORMAL (正常运行)'}`", inline=True)

            # 波动率状态
            chop_text = []
            for a in SUPPORTED_ASSETS:
                ast = get_asset_status(a)
                if ast.get("timestamp", 0) > 0:
                    icon = "🟢 震荡" if ast.get("is_choppy") else "🔴 单边"
                    chop_text.append(f"• **{a}**: 振幅 {ast.get('amplitude', 0.0):.2f}% ({icon})")
            if chop_text:
                embed.add_field(name="📈 标的波动率守门", value="\n".join(chop_text), inline=False)

            embed.set_footer(text="数据来源: VPS 本地 SQLite WAL & 内存状态")
            await ctx.send(embed=embed)

        @bot.command(name="balance")
        async def cmd_balance(ctx):
            """查询余额与额度"""
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
            await ctx.send(embed=embed)

        @bot.command(name="clean")
        async def cmd_clean(ctx):
            """一键清理历史数据"""
            if not is_admin(ctx.author.id):
                await ctx.send(f"❌ 权限不足：只有管理员可以执行 `{self.prefix}clean` 指令。")
                return

            await ctx.send("🧹 正在执行 VPS 历史订单与交易数据清理...")
            try:
                from polymarket.db import clean_all_historical_trades
                counts = clean_all_historical_trades()
                embed = discord.Embed(
                    title="✅ VPS 历史交易数据已彻底清空并重置",
                    description=f"已清空表记录明细：\n`{counts}`\n大盘看板已恢复 $0.0000 初始纯净状态。",
                    color=0x10B981
                )
                await ctx.send(embed=embed)
            except Exception as e:
                await ctx.send(f"❌ 清理失败: {e}")

        @bot.command(name="pause")
        async def cmd_pause(ctx):
            """紧急熔断暂停开仓"""
            if not is_admin(ctx.author.id):
                await ctx.send("❌ 权限不足：只有管理员可以执行熔断操作。")
                return

            from polymarket.risk_manager import RiskManager
            rm = RiskManager()
            rm.is_emergency_halted = True
            embed = discord.Embed(
                title="⏸️ 策略开仓已紧急暂停 (Emergency Halted)",
                description="风控熔断开关已开启，所有策略将拒绝进入新市场开仓。",
                color=0xEF4444
            )
            await ctx.send(embed=embed)

        @bot.command(name="resume")
        async def cmd_resume(ctx):
            """恢复开仓"""
            if not is_admin(ctx.author.id):
                await ctx.send("❌ 权限不足：只有管理员可以执行恢复操作。")
                return

            from polymarket.risk_manager import RiskManager
            rm = RiskManager()
            rm.is_emergency_halted = False
            embed = discord.Embed(
                title="▶️ 策略开仓已成功恢复 (Resumed)",
                description="风控熔断已解除，系统恢复正常 5min 盘口套利扫描。",
                color=0x10B981
            )
            await ctx.send(embed=embed)

        @bot.command(name="redeem")
        async def cmd_redeem(ctx):
            """手动触发链上赎回"""
            if not is_admin(ctx.author.id):
                await ctx.send("❌ 权限不足：只有管理员可以触发链上赎回。")
                return

            await ctx.send("⏳ 正在扫描链上已到期市场并执行自动赎回...")
            try:
                from polymarket.services.onchain_redeemer import OnChainRedeemer
                from polymarket.client import get_client
                client = get_client(is_live=True)
                redeemer = OnChainRedeemer(client=client)
                res = redeemer.redeem_all_expired()
                await ctx.send(f"🎉 链上赎回执行完毕，结果: `{res}`")
            except Exception as e:
                await ctx.send(f"❌ 链上赎回执行异常: {e}")

        @bot.command(name="logs")
        async def cmd_logs(ctx, lines: int = 20):
            """查看 VPS 日志"""
            if not is_admin(ctx.author.id):
                await ctx.send("❌ 权限不足：只有管理员可以查看系统日志。")
                return

            lines = min(max(lines, 5), 50)
            from polymarket.config import paths
            from collections import deque
            log_path = paths.logs_dir() / "trade.log"

            if not log_path.exists():
                await ctx.send("❌ 日志文件不存在: `trade.log`")
                return

            try:
                with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                    tail_lines = list(deque(f, maxlen=lines))
                content = "".join(tail_lines)
                if not content.strip():
                    await ctx.send("📜 日志暂无内容。")
                    return

                # 分块发送防 2000 字符截断
                chunks = [content[i:i + 1800] for i in range(0, len(content), 1800)]
                for idx, chunk in enumerate(chunks):
                    await ctx.send(f"```text\n{chunk}\n```")
            except Exception as e:
                await ctx.send(f"❌ 读取日志失败: {e}")

    # =========================================================================
    # 启动与生命周期管理
    # =========================================================================

    def start(self):
        """在独立后台守护线程中启动 Discord 机器人"""
        if not HAS_DISCORD_LIB:
            logger.debug("[DiscordBot] 缺失 discord.py 库，跳过交互机器人启动。")
            return

        if not self.token:
            logger.debug("[DiscordBot] 未配置 DISCORD_BOT_TOKEN，跳过交互机器人启动。")
            return

        def _runner():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            try:
                logger.info("[DiscordBot] 正在启动 Discord 双向交互控制机器人...")
                self._loop.run_until_complete(self.bot.start(self.token))
            except Exception as e:
                logger.warning(f"[DiscordBot] 机器人后台线程异常退出: {e}")

        self._thread = threading.Thread(target=_runner, daemon=True, name="DiscordInteractiveBotWorker")
        self._thread.start()
