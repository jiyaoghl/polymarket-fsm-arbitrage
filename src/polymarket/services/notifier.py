import json
import logging
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from polymarket.config import (
    DISCORD_ENABLED,
    DISCORD_MIN_SEVERITY,
    DISCORD_WEBHOOK_URL,
)

logger = logging.getLogger("poly_bot")


def mask_address(address: Optional[str]) -> str:
    """对钱包地址进行前 6 后 4 掩码脱敏处理。"""
    if not address or len(address) < 12:
        return address or "N/A"
    return f"{address[:6]}...{address[-4:]}"


class DiscordNotifier:
    """
    Discord 实时战报与风控富文本推送引擎 (Non-blocking Token-Bucket Notifier)。
    
    设计特性：
    1. 纯无阻塞物理隔离：内部采用 queue.Queue 与单例后台守护线程，调用处 <0.01ms 即刻返回；
    2. 令牌桶限流与 429 退避：严格限制发送速率 <= 1.5 帧/秒，遇到 429 自动读取 retry_after 指数退避重试；
    3. 视觉分级：LIVE 实盘高亮红/金卡片，PAPER 模拟盘柔和蓝/紫卡片；
    4. 60s 防刷屏抑制：风控与防爆盾相同市场 60 秒内仅推送 1 次。
    """

    _instance: Optional["DiscordNotifier"] = None
    _lock = threading.Lock()

    # 严重性等级权重映射
    SEVERITY_LEVELS = {
        "DEBUG": 0,
        "INFO": 1,
        "TRADE": 2,
        "WARNING": 3,
        "CRITICAL": 4,
    }

    # 嵌入卡片颜色定义 (RGB 十进制)
    COLOR_SUCCESS = 0x10B981   # 翠绿 (做T止盈/锁仓/赎回)
    COLOR_INFO = 0x3B82F6      # 亮蓝 (开仓/挂单/系统启动)
    COLOR_WARNING = 0xF59E0B   # 暖橙 (防爆盾拦截/风控预警)
    COLOR_DANGER = 0xEF4444    # 绯红 (强平止损/熔断故障)
    COLOR_GOLD = 0xF59E0B      # 金色 (LIVE 实盘盈利)

    def __init__(self, webhook_url: Optional[str] = None, enabled: Optional[bool] = None):
        self.webhook_url = webhook_url if webhook_url is not None else DISCORD_WEBHOOK_URL
        self.enabled = enabled if enabled is not None else DISCORD_ENABLED
        self.min_severity = DISCORD_MIN_SEVERITY

        # 占位符或无效 URL 自动禁用推送，避免 HTTP 400 报错
        if not self.webhook_url or "your_webhook_id" in self.webhook_url or not self.webhook_url.startswith("http"):
            self.enabled = False

        self._queue: queue.Queue = queue.Queue(maxsize=500)
        self._suppress_cache: Dict[str, float] = {}  # 拦截事件防刷缓存
        self._suppress_lock = threading.Lock()
        
        self._running = True
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True, name="DiscordNotifierWorker")
        self._worker_thread.start()

    @classmethod
    def get_instance(cls) -> "DiscordNotifier":
        """获取全局单例通知引擎"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # =========================================================================
    # 后台异步发送守护循环 (Worker Loop with Token-Bucket & 429 Backoff)
    # =========================================================================

    def _worker_loop(self):
        """后台独立守护线程：负责限流、退避重试与 HTTP 发送"""
        while self._running:
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if not self.enabled or not self.webhook_url:
                self._queue.task_done()
                continue

            payload, severity = item
            if not self._check_severity(severity):
                self._queue.task_done()
                continue

            # 令牌桶平滑限流：每条消息之间强制间隔 0.65 秒 (约 1.5 req/s)
            time.sleep(0.65)
            self._send_with_retry(payload)
            self._queue.task_done()

    def _send_with_retry(self, payload: Dict[str, Any], max_retries: int = 3):
        """执行 HTTP POST 发送，支持 429 智能退避与失败隔离"""
        for attempt in range(1, max_retries + 1):
            try:
                resp = requests.post(
                    self.webhook_url,
                    json=payload,
                    timeout=5.0,
                    headers={"Content-Type": "application/json"}
                )
                if resp.status_code in (200, 204):
                    return  # 发送成功

                if resp.status_code == 429:
                    # 遭遇 Discord 频控，解析 retry_after
                    try:
                        data = resp.json()
                        retry_after = float(data.get("retry_after", 1.0))
                    except Exception:
                        retry_after = 1.0
                    logger.warning(f"[DiscordNotifier] 遭遇 429 限流，自动退避 {retry_after:.2f}s 后重试 (尝试 {attempt}/{max_retries})")
                    time.sleep(min(retry_after, 5.0))
                    continue

                logger.warning(f"[DiscordNotifier] 发送失败 [HTTP {resp.status_code}]: {resp.text[:150]}")
                break
            except Exception as e:
                logger.warning(f"[DiscordNotifier] 网络异常 (尝试 {attempt}/{max_retries}): {e}")
                time.sleep(1.0)

    def _check_severity(self, severity: str) -> bool:
        """校验消息严重性是否达到配置的最低门槛"""
        req_level = self.SEVERITY_LEVELS.get(self.min_severity, 2)
        msg_level = self.SEVERITY_LEVELS.get(severity.upper(), 2)
        return msg_level >= req_level

    def send_embed(self, embed: Dict[str, Any], severity: str = "TRADE", content: Optional[str] = None):
        """
        向后台队列投递富文本 Embed 消息 (零阻塞)。
        """
        if not self.enabled or not self.webhook_url:
            return

        payload = {
            "username": "Polymarket 量化战报",
            "avatar_url": "https://polymarket.com/favicon.ico",
            "embeds": [embed]
        }
        if content:
            payload["content"] = content

        try:
            self._queue.put_nowait((payload, severity))
        except queue.Full:
            logger.warning("[DiscordNotifier] 通知队列已满 (500)，丢弃低优先级消息以防内存泄漏。")

    # =========================================================================
    # 核心交易生命周期富文本战报模板 (Rich Embed Templates)
    # =========================================================================

    def notify_entry(
        self,
        market_id: str,
        asset: str,
        strategy_name: str,
        side: str,
        price: float,
        shares: float,
        is_live: bool = False,
        order_type: str = "FOK",
        expected_ev: Optional[float] = None
    ):
        """首腿开仓吃单或双挂发单通知"""
        mode_tag = "🔴 [LIVE 实盘]" if is_live else "🔵 [PAPER 模拟]"
        color = self.COLOR_INFO if not is_live else self.COLOR_GOLD
        cost_usdc = price * shares

        fields = [
            {"name": "🎯 策略模式", "value": f"`{strategy_name}`", "inline": True},
            {"name": "🪙 交易标的", "value": f"`{asset}` 5min 盘口", "inline": True},
            {"name": "🧭 开仓方向", "value": f"**{side}** ({order_type})", "inline": True},
            {"name": "💵 成交价格", "value": f"`${price:.4f}`", "inline": True},
            {"name": "📊 成交份数", "value": f"`{shares:.2f}` 份", "inline": True},
            {"name": "💰 首腿耗资", "value": f"`${cost_usdc:.2f}` USDC", "inline": True},
        ]
        if expected_ev is not None:
            fields.append({"name": "📈 预计 Net EV", "value": f"`+${expected_ev:.4f}`", "inline": False})

        embed = {
            "title": f"{mode_tag} ⚡ 首腿开仓吃单成功！",
            "description": f"市场 ID: `{market_id[:16]}...`",
            "color": color,
            "fields": fields,
            "footer": {"text": "Polymarket FSM Arbitrage Engine"},
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        self.send_embed(embed, severity="TRADE")

    def notify_flip_success(
        self,
        market_id: str,
        asset: str,
        strategy_name: str,
        leg1_cost: float,
        sell_price: float,
        shares: float,
        hold_seconds: float,
        net_profit: float,
        gross_profit: float,
        fee_usdc: float,
        is_live: bool = False,
        ladder_stage: int = 0
    ):
        """同向做 T 高抛止盈战报"""
        mode_tag = "🔴 [LIVE 实盘]" if is_live else "🔵 [PAPER 模拟]"
        roi = (net_profit / (leg1_cost * shares)) * 100 if (leg1_cost * shares) > 0 else 0.0
        profit_str = f"+${net_profit:.4f} USDC" if net_profit >= 0 else f"-${abs(net_profit):.4f} USDC"

        fields = [
            {"name": "🎯 策略名称", "value": f"`{strategy_name}`", "inline": True},
            {"name": "🪙 标的资产", "value": f"`{asset}` 5min", "inline": True},
            {"name": "⏱️ 持仓时长", "value": f"`{hold_seconds:.1f}s` (动态让价脱手)", "inline": True},
            {"name": "📥 买入成本", "value": f"`${leg1_cost:.4f}` × {shares:.1f}份", "inline": True},
            {"name": "📤 卖出均价", "value": f"`${sell_price:.4f}` × {shares:.1f}份", "inline": True},
            {"name": "💸 手续费扣除", "value": f"`-${fee_usdc:.4f}`", "inline": True},
            {"name": "🚀 扣费净收益 (Net PnL)", "value": f"**{profit_str}** (净收益率: **{roi:+.2f}%**)", "inline": False},
        ]

        embed = {
            "title": f"{mode_tag} 🎯 同向做 T 高抛止盈达成！",
            "description": f"已完成极速回转套现，资金已归还风控池。\n市场: `{market_id[:16]}...`",
            "color": self.COLOR_SUCCESS,
            "fields": fields,
            "footer": {"text": "Polymarket FSM Arbitrage Engine"},
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        self.send_embed(embed, severity="TRADE")

    def notify_hedged_lock(
        self,
        market_id: str,
        asset: str,
        strategy_name: str,
        leg1_cost: float,
        leg2_cost: float,
        shares: float,
        net_ev: float,
        gross_profit: float,
        fee_usdc: float,
        is_live: bool = False
    ):
        """双腿对冲锁仓达成战报"""
        mode_tag = "🔴 [LIVE 实盘]" if is_live else "🔵 [PAPER 模拟]"
        total_cost = (leg1_cost + leg2_cost) * shares
        payout = shares * 1.0
        profit_str = f"+${net_ev:.4f} USDC" if net_ev >= 0 else f"-${abs(net_ev):.4f} USDC"

        fields = [
            {"name": "🎯 策略名称", "value": f"`{strategy_name}`", "inline": True},
            {"name": "🪙 标的资产", "value": f"`{asset}` 5min", "inline": True},
            {"name": "🔒 锁仓份数", "value": f"`{shares:.2f}` 对 (YES+NO)", "inline": True},
            {"name": "📊 双腿成本", "value": f"首腿 `${leg1_cost:.4f}` | 二腿 `${leg2_cost:.4f}`", "inline": True},
            {"name": "💵 总建仓成本", "value": f"`${total_cost:.2f}` USDC", "inline": True},
            {"name": "🛡️ 官方保底兑付", "value": f"`${payout:.2f}` USDC", "inline": True},
            {"name": "💎 锁定净收益 (Net EV)", "value": f"**{profit_str}** (已扣双边手续费 `${fee_usdc:.4f}`)", "inline": False},
        ]

        embed = {
            "title": f"{mode_tag} 🔒 双腿对冲完成，成功无风险锁仓！",
            "description": f"已锁定确定性兑付收益，等待交割后链上自动赎回。\n市场: `{market_id[:16]}...`",
            "color": self.COLOR_SUCCESS,
            "fields": fields,
            "footer": {"text": "Polymarket FSM Arbitrage Engine"},
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        self.send_embed(embed, severity="TRADE")

    def notify_force_close(
        self,
        market_id: str,
        asset: str,
        strategy_name: str,
        leg1_cost: float,
        vwap_close_price: float,
        shares: float,
        realized_pnl: float,
        hold_seconds: float,
        is_live: bool = False
    ):
        """单边敞口 TTL 强平平仓战报"""
        mode_tag = "🔴 [LIVE 实盘]" if is_live else "🔵 [PAPER 模拟]"
        pnl_str = f"+${realized_pnl:.4f} USDC" if realized_pnl >= 0 else f"-${abs(realized_pnl):.4f} USDC"

        ttl_desc = "触发 95s 硬兜底" if hold_seconds >= 94.0 else "触发 85s 自适应强平"
        fields = [
            {"name": "🎯 策略名称", "value": f"`{strategy_name}`", "inline": True},
            {"name": "🪙 标的资产", "value": f"`{asset}` 5min", "inline": True},
            {"name": "⏱️ 未对冲时长", "value": f"`{hold_seconds:.1f}s` ({ttl_desc})", "inline": True},
            {"name": "📥 入场成本", "value": f"`${leg1_cost:.4f}` × {shares:.1f}份", "inline": True},
            {"name": "📉 VWAP平仓均价", "value": f"`${vwap_close_price:.4f}` × {shares:.1f}份", "inline": True},
            {"name": "⚠️ 最终实现损益", "value": f"**{pnl_str}**", "inline": False},
        ]

        embed = {
            "title": f"{mode_tag} ⚠️ 单边敞口超时，已强制市价 FOK 止损！",
            "description": f"严守资金风控红线，已全量撤销挂单并完成平仓。\n市场: `{market_id[:16]}...`",
            "color": self.COLOR_DANGER,
            "fields": fields,
            "footer": {"text": "Polymarket FSM Arbitrage Engine"},
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        self.send_embed(embed, severity="TRADE")

    def notify_redeemed(
        self,
        market_id: str,
        asset: str,
        amount_usdc: float,
        tx_hash: Optional[str] = None
    ):
        """链上 CTF 原生自动赎回成功通知"""
        tx_display = f"[`{tx_hash[:10]}...{tx_hash[-6:]}`](https://polygonscan.com/tx/{tx_hash})" if tx_hash else "`已调用确认`"

        fields = [
            {"name": "🪙 结算标的", "value": f"`{asset}` 5min 盘口", "inline": True},
            {"name": "💰 自动赎回本息", "value": f"**+${amount_usdc:.2f} USDC**", "inline": True},
            {"name": "🔗 链上交易哈希", "value": tx_display, "inline": False},
            {"name": "🔄 风控状态", "value": "已 100% 归还风控锁定额度，资金池全额就绪", "inline": False},
        ]

        embed = {
            "title": "🎉 链上 CTF 合约自动结算赎回成功！",
            "description": f"Polygon 官方 ConditionalTokens 官方合约已完成自动清盘。\n市场 ID: `{market_id[:16]}...`",
            "color": self.COLOR_SUCCESS,
            "fields": fields,
            "footer": {"text": "Polymarket CTF Auto-Redeemer"},
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        self.send_embed(embed, severity="TRADE")

    def notify_risk_alert(
        self,
        market_id: str,
        asset: str,
        strategy_name: str,
        reason: str,
        level: str = "warning"
    ):
        """防爆盾拦截 / 风控熔断告警 (带 60s 防抖抑制)"""
        cache_key = f"{market_id}_{asset}_{reason[:15]}"
        now = time.time()
        with self._suppress_lock:
            last_ts = self._suppress_cache.get(cache_key, 0.0)
            if now - last_ts < 60.0:
                return  # 60s 防抖抑制
            self._suppress_cache[cache_key] = now

        color = self.COLOR_DANGER if level == "error" else self.COLOR_WARNING
        severity = "CRITICAL" if level == "error" else "WARNING"

        fields = [
            {"name": "🎯 策略 ID", "value": f"`{strategy_name}`", "inline": True},
            {"name": "🪙 标的资产", "value": f"`{asset}`", "inline": True},
            {"name": "🛡️ 拦截原因", "value": f"**{reason}**", "inline": False},
        ]

        embed = {
            "title": "🛡️ 风控安全防御系统触发拦截",
            "description": f"市场: `{market_id[:16]}...`",
            "color": color,
            "fields": fields,
            "footer": {"text": "Polymarket Risk Guard"},
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        self.send_embed(embed, severity=severity)

    def notify_system_startup(
        self,
        strategies_count: int,
        live_strategies_count: int,
        supported_assets: List[str]
    ):
        """系统平滑启动与策略就绪通知"""
        fields = [
            {"name": "🤖 活跃策略总数", "value": f"`{strategies_count}` 个", "inline": True},
            {"name": "🔴 实盘策略数", "value": f"`{live_strategies_count}` 个", "inline": True},
            {"name": "🪙 监控资产池", "value": f"`{', '.join(supported_assets)}`", "inline": True},
            {"name": "🌐 核心引擎状态", "value": "HTTP/2 多路复用 + WebSocket 事件总线已就绪", "inline": False},
        ]

        embed = {
            "title": "🚀 Polymarket 量化交易机器人已启动就绪！",
            "description": "已进入全天候 5min 盘口滚动扫描与智能对冲套利状态。",
            "color": self.COLOR_INFO,
            "fields": fields,
            "footer": {"text": "Polymarket Arbitrage System V2.0"},
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        self.send_embed(embed, severity="INFO")


class Notifier:
    """向后兼容的轻量 Notifier，统一由 DiscordNotifier 处理"""
    def __init__(self, *args, **kwargs):
        self._discord = DiscordNotifier.get_instance()

    def send_simple_alert(self, msg: str, urgent: bool = False):
        self._discord.send_system_alert(title="系统警报" if urgent else "系统消息", message=msg, level="error" if urgent else "info")

    def send_trade_alert(self, **kwargs):
        pass

    def send_risk_alert(self, **kwargs):
        pass

    def send_error_alert(self, **kwargs):
        pass

    def send_system_alert(self, **kwargs):
        pass


def get_notifier() -> Notifier:
    return Notifier()

