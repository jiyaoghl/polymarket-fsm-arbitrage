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


def render_progress_bar(used: float, total: float, length: int = 8) -> str:
    """生成可视化的资金进度条文本，如 [▓▓░░░░░░] 25.0%"""
    if total <= 0:
        return f"[{'░' * length}] 0.0%"
    pct = min(max(used / total, 0.0), 1.0)
    filled = int(round(pct * length))
    bar = "▓" * filled + "░" * (length - filled)
    return f"[{bar}] {pct * 100:.1f}%"


def get_unified_dashboard_metrics() -> Dict[str, Any]:
    """统一从 api_status 提取权威大盘与策略指标，确保 Discord 与 Web 页面 100% 绝对一致"""
    try:
        from polymarket.apps.dashboard import api_status
        status_model = api_status()
        strategies = status_model.strategies
        current_markets = status_model.current_markets
        risk_metrics = status_model.risk_metrics
    except Exception:
        from polymarket.risk_manager import RiskManager
        return {
            "strategies": [],
            "total_trades": 0,
            "active_now_count": 0,
            "locked_trades": 0,
            "total_net_ev": 0.0,
            "total_fee": 0.0,
            "win_rate": 0.0,
            "current_markets": [],
            "risk_metrics": RiskManager().get_status()
        }

    total_trades = 0
    locked_trades = 0
    total_net_ev = 0.0
    total_fee = 0.0
    win_count = 0
    closed_count = 0
    active_now_count = 0

    strategy_items = []
    for s in strategies:
        strat_trades = s.active_trades
        total_trades += len(strat_trades)

        # 区分当前活跃持仓与历史订单
        strat_active_now = sum(1 for t in strat_trades if t.status in ("leg1_only", "locked", "pending_leg2", "pending_both") and (t.time_to_expiry is None or t.time_to_expiry > 0))
        active_now_count += strat_active_now

        for t in strat_trades:
            if t.status == 'locked':
                locked_trades += 1
            if t.status in ('locked', 'settled'):
                closed_count += 1
                if (t.profit_usdc or 0.0) > 0:
                    win_count += 1
            total_net_ev += float(t.profit_usdc or 0.0)
            total_fee += float(t.fee_usdc or 0.0)

        strategy_items.append({
            "strategy_id": s.strategy_id,
            "name": s.name,
            "is_live": s.is_live,
            "entry_max_price": s.entry_max_price,
            "amount": s.amount,
            "total_pnl": float(s.strategy_total_pnl or 0.0),
            "active_cnt": strat_active_now,
            "total_cnt": len(strat_trades)
        })

    # 追加熔断属性
    from polymarket.risk_manager import RiskManager
    rm = RiskManager()
    for s_item in strategy_items:
        sid = s_item["strategy_id"]
        with rm.lock:
            stats = rm._strategy_daily_stats.get(sid, {})
            cd = rm._strategy_cooldown_until.get(sid, 0.0)
            s_item["daily_loss"] = stats.get('loss', 0.0)
            s_item["consecutive_fc"] = stats.get('consecutive_fc', 0)
            s_item["cooldown_until"] = cd

    win_rate = (win_count / closed_count * 100) if closed_count > 0 else 0.0

    return {
        "strategies": strategy_items,
        "total_trades": total_trades,
        "active_now_count": active_now_count,
        "locked_trades": locked_trades,
        "total_net_ev": total_net_ev,
        "total_fee": total_fee,
        "win_rate": win_rate,
        "current_markets": current_markets,
        "risk_metrics": risk_metrics
    }


def generate_dashboard_embed() -> Optional[Any]:
    """生成统一的大盘状态富文本 Embed 卡片 (与 Web 页面 100% 统计绝对对齐)"""
    if not HAS_DISCORD_LIB or discord is None:
        return None

    from polymarket.risk_manager import RiskManager
    from polymarket.kline_analyzer import get_asset_status

    rm = RiskManager()
    status = rm.get_status()
    is_paused = getattr(rm, "is_emergency_halted", False)

    # 提取与 Web 端完全相同的指标
    metrics = get_unified_dashboard_metrics()
    total_pnl = float(metrics.get("total_net_ev", 0.0))
    total_trades_cnt = int(metrics.get("total_trades", 0))
    active_now_cnt = int(metrics.get("active_now_count", 0))
    win_rate = float(metrics.get("win_rate", 0.0))
    current_markets = metrics.get("current_markets", [])

    # 1. 实盘资金与敞口
    live_max = float(status.get("live_max_exposure", 0.0) or 0.0)
    live_used = float(status.get("live_used_exposure", 0.0) or 0.0)

    # 2. 模拟资金与敞口
    paper_max = float(status.get("paper_max_exposure", 1000.0) or 1000.0)
    paper_used = float(status.get("paper_used_exposure", 0.0) or 0.0)
    paper_avail = max(0.0, paper_max - paper_used)
    paper_bar = render_progress_bar(paper_used, paper_max, length=7)

    # 3. 活跃盘口追踪与倒计时
    tracking_assets = []
    earliest_remaining = None
    now = time.time()
    for m in current_markets:
        asset = m.get("asset") or m.get("__asset_type")
        if asset and asset not in tracking_assets:
            tracking_assets.append(asset)
        exp = m.get("end_time") or m.get("expiry") or 0
        if exp > now:
            rem = int(exp - now)
            if earliest_remaining is None or rem < earliest_remaining:
                earliest_remaining = rem

    color = 0xEF4444 if is_paused else 0x10B981
    status_icon = "🔴 PAUSED (已紧急熔断)" if is_paused else "🟢 NORMAL (全天候套利中)"

    embed = discord.Embed(
        title="📊 Polymarket 实时量化监控与远程控制台 V3.0",
        description=f"当前系统运行状态: **{status_icon}**",
        color=color,
        timestamp=discord.utils.utcnow()
    )

    embed.add_field(name="💰 实盘链上余额", value=f"`${live_max:.2f}` USDC (占用: `${live_used:.2f}`)", inline=True)
    embed.add_field(name="🔒 活跃/历史单", value=f"`{active_now_cnt}` 活跃 / `{total_trades_cnt}` 历史", inline=True)
    embed.add_field(name="📈 净净收益 (NET EV)", value=f"`{'+' if total_pnl >= 0 else '-'}${abs(total_pnl):.4f}` USDC (胜率 `{win_rate:.1f}%`)", inline=True)
    embed.add_field(name="🔵 模拟资金池", value=f"`${paper_avail:.2f} / ${paper_max:.0f}` USDC `{paper_bar}`", inline=False)

    # 当前盘口追踪状态
    if tracking_assets:
        rem_str = f" (⏳ 距离交割剩 `{earliest_remaining}s`)" if earliest_remaining is not None else ""
        embed.add_field(name="🎯 活跃 5min 标的", value=f"`{', '.join(tracking_assets)}`{rem_str}", inline=False)

    # 标的波动率防爆盾状态
    chop_text = []
    for a in SUPPORTED_ASSETS:
        ast = get_asset_status(a)
        if ast.get("timestamp", 0) > 0:
            icon = "🟢 震荡" if ast.get("is_choppy") else "🔴 单边"
            chop_text.append(f"• **{a}**: 振幅 {ast.get('amplitude', 0.0):.2f}% ({icon})")
    if chop_text:
        embed.add_field(name="🪙 实时行情守门 (K线防爆盾)", value="\n".join(chop_text), inline=False)

    embed.set_footer(text="3x3 九宫格直接点按控制 | Polymarket FSM Arbitrage Bot V3.0")
    return embed


def format_ansi_logs(raw_lines: List[str]) -> str:
    """将 VPS 日志转换为带有 Discord ANSI 彩色高亮的终端文本"""
    formatted = []
    for line in raw_lines:
        line_clean = line.strip("\r\n")
        if not line_clean:
            continue
        if any(w in line_clean for w in ("ERROR", "CRITICAL", "failed", "强平", "401", "Unauthorized", "Exception")):
            formatted.append(f"\u001b[31m{line_clean}\u001b[0m")
        elif any(w in line_clean for w in ("WARNING", "拦截", "溢价", "PAUSED", "熔断", "迟滞")):
            formatted.append(f"\u001b[33m{line_clean}\u001b[0m")
        elif any(w in line_clean for w in ("LOCKED", "锁仓", "结算", "成功", "FILLED", "NORMAL", "SETTLED", "已上线")):
            formatted.append(f"\u001b[32m{line_clean}\u001b[0m")
        elif any(w in line_clean for w in ("INFO", "poly_bot", "DiscordBot", "LiveGateway")):
            formatted.append(f"\u001b[34m{line_clean}\u001b[0m")
        else:
            formatted.append(line_clean)
    return "\n".join(formatted)


# =============================================================================
# 持久化纯按钮交互控制面板 (Persistent Button-Driven View V3.1)
# =============================================================================

if HAS_DISCORD_LIB and discord is not None:
    class ConfirmCleanHistoryView(discord.ui.View):
        """清空历史订单二次防误触确认视图 (30s 自动超时自毁)"""
        def __init__(self, parent_view: Optional[discord.ui.View] = None):
            super().__init__(timeout=30.0)
            self.parent_view = parent_view

        async def on_timeout(self):
            for child in self.children:
                child.disabled = True

        @discord.ui.button(label="🔴 确认彻底清空历史", style=discord.ButtonStyle.danger, custom_id="btn_confirm_clean_yes")
        async def on_confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
            if not interaction.response.is_done(): await interaction.response.defer(ephemeral=True)
            if not is_admin(interaction.user.id):
                await interaction.followup.send("❌ 权限不足：只有管理员可以清空历史数据。", ephemeral=True)
                return

            await interaction.response.defer(ephemeral=True)
            try:
                from polymarket.db import clean_all_historical_trades
                counts = clean_all_historical_trades()
                for child in self.children:
                    child.disabled = True
                await interaction.edit_original_response(content=f"✅ 历史订单数据已彻底清空并重置为零历史状态！明细: `{counts}`", view=self)
            except Exception as e:
                await interaction.followup.send(f"❌ 清理失败: {e}", ephemeral=True)

        @discord.ui.button(label="🟢 取消返回", style=discord.ButtonStyle.secondary, custom_id="btn_confirm_clean_cancel")
        async def on_cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
            if not interaction.response.is_done(): await interaction.response.defer(ephemeral=True)
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(content="🛡️ 操作已取消，历史数据保持完好。", view=self)


    class DashboardControlView(discord.ui.View):
        """
        全功能持久化 3x3 九宫格交互按钮面板。
        特性：
        1. timeout=None + 静态 custom_id，VPS 重启后按钮永久有效；
        2. defer(ephemeral=True) 避免 3 秒响应超时；
        3. 原位刷新 (In-Place Edit) 零刷屏。
        """

        def __init__(self):
            super().__init__(timeout=None)

        # ---------------- 行 0：数据看板与盈亏查询 ----------------
        @discord.ui.button(label="🔄 刷新大盘", style=discord.ButtonStyle.success, custom_id="btn_refresh_status", row=0)
        async def on_refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
            if not interaction.response.is_done(): await interaction.response.defer(ephemeral=True)
            embed = generate_dashboard_embed()
            if embed:
                await interaction.response.edit_message(embed=embed, view=self)

        @discord.ui.button(label="💰 资金明细", style=discord.ButtonStyle.primary, custom_id="btn_view_balance", row=0)
        async def on_balance(self, interaction: discord.Interaction, button: discord.ui.Button):
            if not interaction.response.is_done(): await interaction.response.defer(ephemeral=True)
            from polymarket.risk_manager import RiskManager
            rm = RiskManager()
            st = rm.get_status()
            metrics = get_unified_dashboard_metrics()

            live_max = float(st.get("live_max_exposure", 0.0) or 0.0)
            live_used = float(st.get("live_used_exposure", 0.0) or 0.0)
            paper_max = float(st.get("paper_max_exposure", 1000.0) or 1000.0)
            paper_used = float(st.get("paper_used_exposure", 0.0) or 0.0)
            paper_avail = max(0.0, paper_max - paper_used)
            paper_bar = render_progress_bar(paper_used, paper_max, length=10)

            total_ev = float(metrics.get("total_net_ev", 0.0))
            total_fee = float(metrics.get("total_fee", 0.0))

            embed = discord.Embed(
                title="💰 资金池与风控额度明细概况",
                color=0x3B82F6,
                timestamp=discord.utils.utcnow()
            )
            embed.add_field(name="🔴 实盘真实余额", value=f"`${live_max:.4f}` USDC", inline=True)
            embed.add_field(name="🔒 实盘占用敞口", value=f"`${live_used:.2f}` USDC", inline=True)
            embed.add_field(name="🔵 模拟资金池使用率", value=f"`${paper_avail:.2f} / ${paper_max:.0f}` USDC\n`{paper_bar}`", inline=False)
            embed.add_field(name="📈 净净收益 (NET EV)", value=f"`{'+' if total_ev >= 0 else '-'}${abs(total_ev):.4f}` USDC", inline=True)
            embed.add_field(name="⛽ 累计手续费消耗", value=f"`-${total_fee:.4f}` USDC", inline=True)
            await interaction.followup.send(embed=embed, ephemeral=True)

        @discord.ui.button(label="📈 策略盈亏", style=discord.ButtonStyle.primary, custom_id="btn_view_strategies", row=0)
        async def on_strategies(self, interaction: discord.Interaction, button: discord.ui.Button):
            if not interaction.response.is_done(): await interaction.response.defer(ephemeral=True)
            metrics = get_unified_dashboard_metrics()
            strategy_items = metrics.get("strategies", [])

            embed = discord.Embed(
                title="📈 各策略独立持仓与盈亏排行榜 (与 Web 绝对对齐)",
                description=f"全组合统计：共 `{metrics.get('total_trades', 0)}` 笔订单 (含历史有效归档单)：",
                color=0x3B82F6,
                timestamp=discord.utils.utcnow()
            )

            for s in strategy_items:
                sname = s.get("name", s.get("strategy_id"))
                mode = "🔴 LIVE" if s.get("is_live") else "🔵 PAPER"
                pnl = float(s.get("total_pnl", 0.0))
                pnl_str = f"+${pnl:.4f}" if pnl >= 0 else f"-${abs(pnl):.4f}"


                import time
                cd_until = s.get("cooldown_until", 0.0)
                is_halted = cd_until > time.time()
                halt_str = ""
                if is_halted:
                    rem = int(cd_until - time.time())
                    halt_str = f"\n⚠️ **[策略熔断中]** 剩余冷却: `{rem//60}分{rem%60}秒`"
                else:
                    daily_loss = s.get("daily_loss", 0.0)
                    consecutive_fc = s.get("consecutive_fc", 0)
                    if daily_loss > 0 or consecutive_fc > 0:
                        halt_str = f"\n🛡️ 熔断水位: 连续强平 `{consecutive_fc}` 次 | 累计亏损 `${daily_loss:.2f}`"

                embed.add_field(
                    name=f"{mode} | {sname}{' ⛔(冷却中)' if is_halted else ''}",
                    value=f"• 活跃持仓: `{s.get('active_cnt', 0)}` 笔 | 历史单: `{s.get('total_cnt', 0)}` 笔\n• 策略净净收益: `{pnl_str}` USDC\n• 入场门槛: `≤{s.get('entry_max_price', 0):.3f}` | 单笔: `${s.get('amount', 0):.1f}`{halt_str}",
                    inline=False
                )

            total_ev = float(metrics.get("total_net_ev", 0.0))
            tot_str = f"+${total_ev:.4f}" if total_ev >= 0 else f"-${abs(total_ev):.4f}"
            embed.set_footer(text=f"全组合净净收益: {tot_str} USDC | 手续费: -${metrics.get('total_fee', 0.0):.4f} | 胜率: {metrics.get('win_rate', 0.0):.1f}%")
            await interaction.followup.send(embed=embed, ephemeral=True)

        # ---------------- 行 1：微观盘口、日志与链上结算 ----------------
        
        @discord.ui.button(label="🔍 订单透视", style=discord.ButtonStyle.secondary, custom_id="btn_inspector", row=0)
        async def on_inspect(self, interaction: discord.Interaction, button: discord.ui.Button):
            if not interaction.response.is_done(): await interaction.response.defer(ephemeral=True)
            await interaction.response.defer(ephemeral=True)
            from polymarket.apps.dashboard import api_status
            try:
                status_model = api_status()
            except Exception:
                await interaction.followup.send("❌ 获取状态失败。", ephemeral=True)
                return

            trades = []
            for s in status_model.strategies:
                trades.extend(s.active_trades)
                
            def get_attr(obj, k, d=None):
                if isinstance(obj, dict): return obj.get(k, d)
                return getattr(obj, k, d)
                
            trades.sort(key=lambda t: get_attr(t, 'end_time') or 0, reverse=True)
            valid_trades = [t for t in trades if get_attr(t, 'status') != "pending"][:25]
            
            if not valid_trades:
                await interaction.followup.send("⏳ 当前没有任何有效的订单记录可供透视。", ephemeral=True)
                return

            options = []
            for t in valid_trades:
                asset = get_attr(t, 'asset') or "UNKNOWN"
                status = str(get_attr(t, 'status', '')).upper()
                pnl = get_attr(t, 'profit_usdc')
                pnl_str = f" [${pnl:.3f}]" if pnl is not None else ""
                label = f"{asset} | {status}{pnl_str}"
                market_id = get_attr(t, 'market_id', '')
                desc = f"ID: {market_id[:20]} | Strat: {get_attr(t, 'strategy_id')}"[:100]
                options.append(discord.SelectOption(
                    label=label[:100],
                    description=desc,
                    value=market_id
                ))

            class InspectorSelect(discord.ui.Select):
                def __init__(self):
                    super().__init__(placeholder="请选择要透视的订单...", min_values=1, max_values=1, options=options)

                async def callback(self, inter: discord.Interaction):
                    market_id = self.values[0]
                    from polymarket.apps.dashboard import get_trade_detail
                    import time
                    try:
                        detail = get_trade_detail(market_id)
                        if isinstance(detail, dict) and "error" in detail:
                            await inter.response.defer(ephemeral=True)
                    await inter.followup.send(f"❌ {detail['error']}", ephemeral=True)
                            return
                        
                        embed = discord.Embed(
                            title="🔍 交易生命周期透视 (Trade Inspector)",
                            description=f"市场 ID: `{market_id}`",
                            color=0x8B5CF6,
                            timestamp=discord.utils.utcnow()
                        )
                        def _get(obj, key, default=None):
                            if hasattr(obj, 'model_dump'): obj = obj.model_dump()
                            if isinstance(obj, dict): return obj.get(key, default)
                            return getattr(obj, key, default)

                        embed.add_field(name="状态", value=f"`{_get(detail, 'status')}`", inline=True)
                        embed.add_field(name="结算方式", value=f"`{_get(detail, 'settlement_type') or '--'}`", inline=True)
                        
                        pnl = _get(detail, 'profit_usdc')
                        if pnl is not None:
                            embed.add_field(name="净收益", value=f"`{'+' if pnl>=0 else '-'}${abs(pnl):.4f}`", inline=True)
                            
                        latency = _get(detail, 'latency_ms')
                        if latency is not None:
                            embed.add_field(name="执行延迟", value=f"`{latency} ms`", inline=True)
                            
                        leg1 = _get(detail, 'leg1')
                        if leg1:
                            embed.add_field(name="Leg1 (首腿)", value=f"`{_get(leg1, 'side')} {_get(leg1, 'size',0):.2f}份 @ {_get(leg1, 'cost',0):.4f}`", inline=False)
                        
                        leg2 = _get(detail, 'leg2')
                        if leg2:
                            embed.add_field(name="Leg2 (二腿)", value=f"`{_get(leg2, 'side')} {_get(leg2, 'size',0):.2f}份 @ {_get(leg2, 'cost',0):.4f}`", inline=False)
                            
                        reprice = _get(detail, 'reprice_history')
                        if reprice:
                            rp_str = ""
                            for rp in reprice[-5:]: 
                                ts = time.strftime('%H:%M:%S', time.localtime(_get(rp, 'timestamp', 0)))
                                rp_str += f"`[{ts}]` {_get(rp, 'old_price')} -> **{_get(rp, 'new_price')}** ({_get(rp, 'reason')})\n"
                            embed.add_field(name=f"改价轨迹 (共 {len(reprice)} 次)", value=rp_str[:1024], inline=False)
                            
                        await inter.response.defer(ephemeral=True)
                    await inter.followup.send(embed=embed, ephemeral=True)
                    except Exception as e:
                        await inter.response.defer(ephemeral=True)
                    await inter.followup.send(f"❌ 透视失败: {e}", ephemeral=True)

            class InspectorView(discord.ui.View):
                def __init__(self):
                    super().__init__(timeout=60)
                    self.add_item(InspectorSelect())

            await interaction.followup.send("🔍 请选择您想透视溯源的订单记录：", view=InspectorView(), ephemeral=True)

        @discord.ui.button(label="🎯 活跃盘口"
, style=discord.ButtonStyle.primary, custom_id="btn_view_markets", row=1)
        async def on_markets(self, interaction: discord.Interaction, button: discord.ui.Button):
            if not interaction.response.is_done(): await interaction.response.defer(ephemeral=True)
            from polymarket.apps.dashboard import manager
            current_markets = getattr(manager, "current_markets", [])
            
            if not current_markets:
                await interaction.followup.send("⏳ 当前处于 5min 盘口交割切换期，系统正在滚动探测下期盘口...", ephemeral=True)
                return

            embed = discord.Embed(
                title="🎯 当前活跃 5min 套利盘口深度与价差",
                description=f"全天候微观监控：共 **{len(current_markets)}** 个 5min 盘口",
                color=0x8B5CF6,
                timestamp=discord.utils.utcnow()
            )
            now = time.time()
            for m in current_markets:
                asset = m.get("asset") or m.get("__asset_type", "UNKNOWN")
                desc = m.get("description", m.get("id", ""))
                exp = m.get("end_time") or m.get("expiry", 0)
                remaining = max(0, int(exp - now))
                tokens = m.get("tokens", [])
                yes_tok = tokens[0].get("token_id") if len(tokens) > 0 else ""
                no_tok = tokens[1].get("token_id") if len(tokens) > 1 else ""

                embed.add_field(
                    name=f"🪙 [{asset}] {desc[:40]}",
                    value=f"• 距离交割: `{remaining}s` ({remaining//60}分{remaining%60}秒) | 状态: `🟢 活跃追踪`\n• YES Token: `{yes_tok[:12]}...`\n• NO Token: `{no_tok[:12]}...`",
                    inline=False
                )
            await interaction.followup.send(embed=embed, ephemeral=True)

        @discord.ui.button(label="📜 最新日志", style=discord.ButtonStyle.secondary, custom_id="btn_view_logs", row=1)
        async def on_logs(self, interaction: discord.Interaction, button: discord.ui.Button):
            if not interaction.response.is_done(): await interaction.response.defer(ephemeral=True)
            if not is_admin(interaction.user.id):
                await interaction.followup.send("❌ 权限不足：只有管理员可以查看控制台日志。", ephemeral=True)
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
                ansi_content = format_ansi_logs(tail_lines)
                if not ansi_content.strip():
                    await interaction.followup.send("📜 日志暂无内容。", ephemeral=True)
                    return

                chunks = [ansi_content[i:i + 1800] for i in range(0, len(ansi_content), 1800)]
                for chunk in chunks:
                    await interaction.followup.send(f"```ansi\n{chunk}\n```", ephemeral=True)
            except Exception as e:
                await interaction.followup.send(f"❌ 读取日志失败: {e}", ephemeral=True)

        @discord.ui.button(label="🎉 链上赎回", style=discord.ButtonStyle.primary, custom_id="btn_onchain_redeem", row=1)
        async def on_redeem(self, interaction: discord.Interaction, button: discord.ui.Button):
            if not interaction.response.is_done(): await interaction.response.defer(ephemeral=True)
            if not is_admin(interaction.user.id):
                await interaction.followup.send("❌ 权限不足：只有管理员可以触发链上赎回。", ephemeral=True)
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

        # ---------------- 行 2：风控熔断与数据管理 ----------------
        @discord.ui.button(label="🛡️ 熔断管理", style=discord.ButtonStyle.primary, custom_id="btn_circuit_breaker", row=2)
        async def on_circuit_breaker(self, interaction: discord.Interaction, button: discord.ui.Button):
            if not interaction.response.is_done(): await interaction.response.defer(ephemeral=True)
            await interaction.response.defer(ephemeral=True)
            if not is_admin(interaction.user.id):
                await interaction.followup.send("❌ 权限不足。", ephemeral=True)
                return
                
            metrics = get_unified_dashboard_metrics()
            strategy_items = metrics.get("strategies", [])
            options = []
            import time
            now = time.time()
            for s in strategy_items:
                sid = s.get("strategy_id")
                name = s.get("name", sid)
                cd = s.get("cooldown_until", 0.0)
                is_halted = cd > now
                status = "🔴 熔断中" if is_halted else "🟢 运行中"
                options.append(discord.SelectOption(
                    label=f"{status} | {name}"[:100],
                    value=sid
                ))
                
            if not options:
                await interaction.followup.send("❌ 无可用策略。", ephemeral=True)
                return
                
            class CBSelect(discord.ui.Select):
                def __init__(self):
                    super().__init__(placeholder="请选择要 强制冷却/恢复 的策略...", options=options)
                    
                async def callback(self, inter: discord.Interaction):
                    sid = self.values[0]
                    from polymarket.risk_manager import RiskManager
                    rm = RiskManager()
                    import time
                    now = time.time()
                    with rm.lock:
                        cd = rm._strategy_cooldown_until.get(sid, 0.0)
                        if cd > now:
                            # 恢复
                            del rm._strategy_cooldown_until[sid]
                            if sid in rm._strategy_daily_stats:
                                rm._strategy_daily_stats[sid] = {'loss': 0.0, 'consecutive_fc': 0}
                            msg = f"✅ 已为您强制恢复策略 `{sid}`，熔断冷却和水位已清零！"
                        else:
                            # 手动熔断 2 小时
                            rm._strategy_cooldown_until[sid] = now + 7200
                            msg = f"⛔ 已为您强制熔断策略 `{sid}` 2 小时！"
                    await inter.response.defer(ephemeral=True)
                    await inter.followup.send(msg, ephemeral=True)
                    
            class CBView(discord.ui.View):
                def __init__(self):
                    super().__init__(timeout=60)
                    self.add_item(CBSelect())
                    
            await interaction.followup.send("🛡️ **单策略精细化熔断控制**：\n选中运行中的策略可将其强制熔断 2 小时；\n选中冷却中的策略可一键解除封印并清空风险水位。", view=CBView(), ephemeral=True)

        @discord.ui.button(label="⏸️ 紧急暂停", style=discord.ButtonStyle.danger, custom_id="btn_emergency_pause", row=2)
        async def on_pause(self, interaction: discord.Interaction, button: discord.ui.Button):
            if not interaction.response.is_done(): await interaction.response.defer(ephemeral=True)
            if not is_admin(interaction.user.id):
                await interaction.followup.send("❌ 权限不足：只有管理员可以执行熔断操作。", ephemeral=True)
                return

            from polymarket.risk_manager import RiskManager
            rm = RiskManager()
            rm.is_emergency_halted = True
            embed = generate_dashboard_embed()
            if embed:
                await interaction.response.edit_message(embed=embed, view=self)
                await interaction.followup.send("⏸️ 已触发紧急熔断：已暂停所有策略新开仓。", ephemeral=True)

        @discord.ui.button(label="▶️ 恢复开仓", style=discord.ButtonStyle.success, custom_id="btn_resume_trading", row=2)
        async def on_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
            if not interaction.response.is_done(): await interaction.response.defer(ephemeral=True)
            if not is_admin(interaction.user.id):
                await interaction.followup.send("❌ 权限不足：只有管理员可以执行恢复操作。", ephemeral=True)
                return

            from polymarket.risk_manager import RiskManager
            rm = RiskManager()
            rm.is_emergency_halted = False
            embed = generate_dashboard_embed()
            if embed:
                await interaction.response.edit_message(embed=embed, view=self)
                await interaction.followup.send("▶️ 策略已恢复：解除暂停状态，恢复 5min 套利扫描。", ephemeral=True)

        @discord.ui.button(label="🧹 清空历史", style=discord.ButtonStyle.danger, custom_id="btn_clean_history", row=2)
        async def on_clean(self, interaction: discord.Interaction, button: discord.ui.Button):
            if not interaction.response.is_done(): await interaction.response.defer(ephemeral=True)
            if not is_admin(interaction.user.id):
                await interaction.followup.send("❌ 权限不足：只有管理员可以清空历史数据。", ephemeral=True)
                return

            confirm_view = ConfirmCleanHistoryView(parent_view=self)
            await interaction.followup.send(
                "⚠️ **高危操作防误触确认**\n您确定要彻底清空 SQLite 数据库中的所有历史订单、已实现盈亏统计和缓存吗？\n（此操作不可逆，30 秒未操作将自动失效）",
                view=confirm_view,
                ephemeral=True
            )
else:
    class ConfirmCleanHistoryView:
        pass

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

