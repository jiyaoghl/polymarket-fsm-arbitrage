import json
import time
import threading
from typing import List, Dict, Any, Optional, Set

from polymarket.strategy_fsm import ArbitrageBotFSM as ArbitrageBot
from polymarket.client import PolyClient, get_client
from polymarket.trade_state import TradeStateStore
from polymarket.logger import logger
from polymarket.config import DAILY_MAX_DRAWDOWN, INITIAL_CAPITAL, STOP_LOSS_TIME_REMAINING
from polymarket import paths


def send_telegram_alert(message: str, bot_token: Optional[str], chat_id: Optional[str]) -> None:
    """发送 Telegram 通知。"""
    if not bot_token or not chat_id:
        return
    
    try:
        import requests
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        requests.post(url, json={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=5)
    except Exception as e:
        logger.error(f"发送 Telegram 通知失败：{e}")


class StrategyManager:
    """
    统一调度多策略实例：
    - 定时发现新盘并派发给各策略
    - 定时扫描已结束市场并执行 redeem
    - 风控管理：每日最大回撤限制
    - 通知告警：Telegram/Discord 推送
    """

    def __init__(self, config_path: str | None = None):
        if config_path is None:
            config_path = str(paths.configs_dir() / "strategies.json")
        self.config_path = config_path
        self.bots: List[ArbitrageBot] = []
        self.scanner = get_client(is_live=False)  # 扫描器只需读取权限
        self.redeem_client = get_client(is_live=False)
        self.current_markets: List[Dict[str, Any]] = []
        
        # 风控模块
        self.trade_state = TradeStateStore(initial_capital=INITIAL_CAPITAL)
        self.max_drawdown_pct = DAILY_MAX_DRAWDOWN
        self.is_paused = False  # 风控暂停标志
        
        # 风控监控配置
        import os
        self.risk_check_interval = 10.0  # 风控检查间隔（秒）
        self.max_position_time = 300  # 最大持仓时间（秒）
        self.max_single_loss_pct = float(os.getenv("MAX_SINGLE_LOSS_PCT", "0.15"))  # 单笔潜在亏损告警比例（15%）
        
        # 通知配置
        from polymarket.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
        self.telegram_token = TELEGRAM_BOT_TOKEN
        self.telegram_chat_id = TELEGRAM_CHAT_ID
        
        self.load_strategies()

    def load_strategies(self) -> None:
        """从 JSON 加载所有策略配置。"""
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                configs = json.load(f)

            self.bots = []
            is_any_live = False
            for cfg in configs:
                bot = ArbitrageBot(cfg)
                self.bots.append(bot)
                if cfg.get("is_live"):
                    is_any_live = True
                logger.info(
                    f"成功加载策略：{cfg.get('name')} "
                    f"(ID: {cfg.get('strategy_id')}, 实盘：{cfg.get('is_live')})"
                )

            # 根据策略是否包含实盘实例，动态绑定 redeem 客户端
            self.redeem_client = get_client(is_live=is_any_live)
            logger.info(f"结算客户端初始化完成：{'[实盘模式]' if is_any_live else '[模拟模式]'}")
        except Exception as e:
            logger.error(f"加载策略配置失败：{e}")

    def _on_pnl_updated(self) -> None:
        """PnL 变化后立即触发回撤检查，实现实盘秒级事件风控响应[P0修复]。"""
        if self._check_drawdown_limit():
            logger.warning("[风控] PnL 变动触发回撤阈值熔断，已暂停所有交易")

    def _check_drawdown_limit(self) -> bool:
        """检查是否触发每日最大回撤限制。"""
        if self.trade_state.should_pause_for_drawdown(self.max_drawdown_pct):
            self.is_paused = True
            message = f"⚠️ <b>风控告警</b>\n\n已触发每日最大回撤限制 ({self.max_drawdown_pct*100:.1f}%)\n已暂停所有策略交易"
            send_telegram_alert(message, self.telegram_token, self.telegram_chat_id)
            logger.warning(f"触发每日最大回撤 {self.max_drawdown_pct*100:.1f}%，暂停交易")
            return True
        return False

    @staticmethod
    def _parse_end_time(raw) -> float:
        """将 Gamma API 返回的 endDate 统一转为 Unix 时间戳。"""
        if raw is None:
            return 0.0
        if isinstance(raw, (int, float)):
            return float(raw)
        if isinstance(raw, str):
            from datetime import datetime, timezone
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                return dt.timestamp()
            except Exception:
                pass
            try:
                return float(raw)
            except Exception:
                pass
        return 0.0

    def _fetch_market_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        """根据 slug 从 Gamma API 获取市场数据，返回标准化的 market dict 或 None。"""
        url = f"{self.scanner.gamma_host}/markets/slug/{slug}"
        resp = self.scanner.session.get(url, timeout=15)
        if resp.status_code != 200:
            return None

        m = resp.json()
        if not m.get("active") or m.get("closed"):
            return None

        token_ids_str = m.get("clobTokenIds")
        if not token_ids_str:
            return None

        token_ids = json.loads(token_ids_str)
        if len(token_ids) < 2:
            return None

        return {
            "id": m.get("conditionId"),
            "description": m.get("question") or slug,
            "tokens": {
                "YES": token_ids[0],
                "NO": token_ids[1],
            },
            "expiry": self._parse_end_time(m.get("endDate")),
        }

    def _loop_discover_markets(self) -> None:
        """
        按 5 分钟窗口滚动发现 BTC 5min 新盘。

        每轮：
        1. 计算下一期时间戳 ts
        2. 在开盘前 5 秒醒来，拉取 slug=btc-updown-5m-{ts}
        3. 派发给所有策略（只派发一次）
        4. sleep 到下下期窗口
        """
        logger.info(f"启动 5min 市场滚动发现，共 {len(self.bots)} 个策略...")

        last_dispatched_ts = 0

        while True:
            if self.is_paused:
                logger.info("风控暂停中，跳过市场发现...")
                time.sleep(60)
                continue

            if self._check_drawdown_limit():
                time.sleep(60)
                continue

            now = int(time.time())
            next_ts = ((now // 300) + 1) * 300
            sleep_sec = max(0, next_ts - now - 5)

            if sleep_sec > 0:
                logger.info(f"距离下期 5min 盘 (ts={next_ts}) 还有 {sleep_sec + 5}s，等待中...")
                time.sleep(sleep_sec)

            if next_ts == last_dispatched_ts:
                time.sleep(10)
                continue

            from polymarket.config import SUPPORTED_ASSETS
            
            markets_found = []
            for asset in SUPPORTED_ASSETS:
                slug = f"{asset.lower()}-updown-5m-{next_ts}"
                logger.info(f"尝试定位市场 {asset} slug={slug}")

                retries = 3
                market = None
                for attempt in range(retries):
                    try:
                        market = self._fetch_market_by_slug(slug)
                        if market:
                            market["__asset_type"] = asset.upper()
                            break
                    except Exception as e:
                        logger.warning(f"获取 {slug} 失败 (attempt {attempt + 1}/{retries}): {e}")
                    time.sleep(1)

                if market:
                    markets_found.append(market)
                    logger.info(
                        f"已定位市场：{market['description']}，expiry={market['expiry']:.0f}，"
                        f"YES={market['tokens']['YES'][:12]}... NO={market['tokens']['NO'][:12]}..."
                    )
                else:
                    logger.warning(f"本周期未定位到 {asset} 市场 {slug}")

            if not markets_found:
                logger.warning(f"本周期未定位到任何配置的 5min 市场，等待下期")
                last_dispatched_ts = next_ts
                continue

            last_dispatched_ts = next_ts
            self.current_markets = markets_found

            for market in markets_found:
                for bot in self.bots:
                    def _safe_execute(b=bot, m=market):
                        try:
                            b.execute_strategy(m)
                        except Exception as e:
                            logger.critical(f"[策略派发致命异常] 策略 {b.strategy_id} 执行 {m.get('id')} 崩溃: {e}", exc_info=True)
                            from polymarket import risk_logger
                            risk_logger.push_risk_event(
                                market_id=m.get("id", "UNKNOWN"),
                                asset=m.get("__asset_type", "UNKNOWN"),
                                strategy=b.strategy_id,
                                reason=f"策略执行崩溃: {e}",
                                level="critical"
                            )

                    threading.Thread(
                        target=_safe_execute,
                        daemon=True,
                    ).start()

    def _get_traded_market_ids(self) -> set:
        """收集所有策略实际交易过的市场 ID。"""
        ids = set()
        for bot in self.bots:
            for market_id in bot._get_all_active_trades():
                ids.add(market_id)
            for market_id in list(bot.processed_markets):
                ids.add(market_id)
        return ids

    def _loop_redeem_closed_markets(self) -> None:
        """定期对自己交易过的已结束市场执行 redeem。"""
        logger.info("启动结算扫描循环...")
        redeemed = set()

        while True:
            traded_ids = self._get_traded_market_ids()
            if not traded_ids:
                time.sleep(60)
                continue

            for m_id in traded_ids:
                if m_id in redeemed:
                    continue
                try:
                    res = self.redeem_client.redeem(m_id)
                    if res.get("status") != "SIMULATED":
                        logger.info(f"市场 {m_id} redeem 结果：{res}")
                    redeemed.add(m_id)
                except Exception as e:
                    logger.warning(f"redeem {m_id} 失败：{e}")

            time.sleep(120)

    def _loop_risk_monitor(self) -> None:
        """
        独立风控监控线程。
        
        定期检查所有活跃仓位的风险状况：
        1. 持仓时间过长告警
        2. 即将到期止损提醒
        3. 单笔亏损过大告警
        4. 每日回撤检查
        """
        logger.info("启动风控监控线程...")
        
        while True:
            try:
                # 检查每日回撤
                if self._check_drawdown_limit():
                    logger.warning("风控监控：触发每日回撤限制")
                
                # 遍历所有策略的活跃仓位
                for bot in self.bots:
                    active_trades = bot._get_all_active_trades()
                    
                    for market_id, trade in active_trades.items():
                        self._check_position_risk(bot, market_id, trade)
                
            except Exception as e:
                logger.exception(f"风控监控异常：{e}")
            
            time.sleep(self.risk_check_interval)

    def _check_position_risk(self, bot: ArbitrageBot, market_id: str, trade: Dict[str, Any]) -> None:
        """
        检查单个仓位的风险状况。
        
        Args:
            bot: 策略实例
            market_id: 市场 ID
            trade: 交易信息
        """
        now = time.time()
        status = trade.get("status", "")
        
        # 只检查首腿持仓状态
        if status != "leg1_only":
            return
        
        # 计算持仓时间
        created_at = trade.get("created_at", now)
        holding_time = now - created_at
        time_to_expiry = trade.get("end_time", 0) - now
        
        # 1. 持仓时间过长告警
        if holding_time > self.max_position_time * 0.8:
            logger.warning(
                f"[风控] 策略 {bot.strategy_id} 市场 {market_id} "
                f"持仓时间过长: {holding_time:.1f}s / {self.max_position_time}s"
            )
        
        # 2. 即将到期止损提醒
        if time_to_expiry <= STOP_LOSS_TIME_REMAINING + 10 and time_to_expiry > STOP_LOSS_TIME_REMAINING:
            logger.warning(
                f"[风控] 策略 {bot.strategy_id} 市场 {market_id} "
                f"即将触发止损: 剩余 {time_to_expiry:.1f}s"
            )
            # 发送通知
            message = (
                f"⚠️ <b>止损预警</b>\n\n"
                f"策略: {bot.strategy_id}\n"
                f"市场: {market_id[:20]}...\n"
                f"剩余时间: {time_to_expiry:.1f}s\n"
                f"即将触发止损卖出"
            )
            send_telegram_alert(message, self.telegram_token, self.telegram_chat_id)
        
        # 3. 检查潜在亏损
        leg1 = trade.get("leg1", {})
        entry_price = leg1.get("cost", 0)
        position_size = leg1.get("size", 0)
        
        if entry_price > 0 and position_size > 0:
            # 获取当前价格估算潜在亏损
            try:
                token_id = leg1.get("token")
                if token_id:
                    prices = bot.client.get_market_price(token_id)
                    if prices:
                        current_bid = prices.get("bid", 0)
                        potential_loss = position_size * (entry_price - current_bid)
                        loss_pct = potential_loss / (position_size * entry_price) if entry_price > 0 else 0
                        
                        if loss_pct > self.max_single_loss_pct:
                            logger.warning(
                                f"[风控] 策略 {bot.strategy_id} 市场 {market_id} "
                                f"潜在亏损过大: {loss_pct*100:.2f}% (阈值 {self.max_single_loss_pct*100:.1f}%)"
                            )
                            message = (
                                f"⚠️ <b>亏损预警</b>\n\n"
                                f"策略: {bot.strategy_id}\n"
                                f"市场: {market_id[:20]}...\n"
                                f"潜在亏损: {potential_loss:.4f} USDC ({loss_pct*100:.2f}%)\n"
                                f"入场价: {entry_price:.4f}\n"
                                f"当前买价: {current_bid:.4f}"
                            )
                            send_telegram_alert(message, self.telegram_token, self.telegram_chat_id)
            except Exception as e:
                logger.debug(f"检查潜在亏损失败: {e}")

    def get_risk_status(self) -> Dict[str, Any]:
        """
        获取风控状态摘要。
        
        Returns:
            包含风控状态信息的字典
        """
        today_stats = self.trade_state.get_today_stats()
        
        # 统计活跃仓位
        total_positions = 0
        leg1_positions = 0
        locked_positions = 0
        
        for bot in self.bots:
            active_trades = bot._get_all_active_trades()
            total_positions += len(active_trades)
            for trade in active_trades.values():
                if trade.get("status") == "leg1_only":
                    leg1_positions += 1
                elif trade.get("status") == "locked":
                    locked_positions += 1
        
        return {
            "is_paused": self.is_paused,
            "max_drawdown_pct": self.max_drawdown_pct,
            "today_stats": today_stats,
            "total_positions": total_positions,
            "leg1_positions": leg1_positions,
            "locked_positions": locked_positions,
            "risk_check_interval": self.risk_check_interval,
        }

    def run_all(self) -> None:
        """并行运行：市场发现 + 结算扫描 + 风控监控。"""
        t1 = threading.Thread(target=self._loop_discover_markets, daemon=True)
        t2 = threading.Thread(target=self._loop_redeem_closed_markets, daemon=True)
        t3 = threading.Thread(target=self._loop_risk_monitor, daemon=True)
        t1.start()
        t2.start()
        t3.start()

        logger.info("策略管理器已启动：市场发现 + 结算扫描 + 风控监控")

        # 主线程保持存活，使用循环 timeout 允许响应 Ctrl+C 信号
        try:
            while t1.is_alive() or t2.is_alive() or t3.is_alive():
                t1.join(timeout=1.0)
        except KeyboardInterrupt:
            logger.info("接收到中断信号 (KeyboardInterrupt)，正在优雅停止策略管理器...")


if __name__ == "__main__":
    manager = StrategyManager()
    manager.run_all()