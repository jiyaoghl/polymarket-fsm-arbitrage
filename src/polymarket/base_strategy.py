import json
import time
import threading
import asyncio
from typing import Dict, Any, Optional, List, Tuple

import websockets

from polymarket.client import PolyClient
from polymarket.config import (
    INITIAL_ENTRY_MAX_PRICE,
    REENTRY_TRIGGER_PRICE,
    STOP_LOSS_TIME_REMAINING,
    ORDER_AMOUNT,
    MAX_SLIPPAGE_TOLERANCE,
    LEG1_MAX_UNHEDGED_SECONDS,
    MAX_CONCURRENT_UNHEDGED_TRADES,
    HTTPS_PROXY,
)
from polymarket.logger import logger


class BaseStrategy:
    """
    5Min Symmetric Bot 的单策略实例。

    - 首腿：买入当前 ASK 最低一边（≤ entry_max_price）
    - 二腿：另一边 ASK < reentry_trigger 且 距离结束 > 10s 时，原子 batch 补仓
    - 尾盘：剩余时间 ≤ STOP_LOSS_TIME_REMAINING 且 > 10s 时做止损卖出首腿
    """

    # WebSocket 配置
    WS_URI = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    WS_RECONNECT_MAX_ATTEMPTS = 5
    WS_RECONNECT_BASE_DELAY = 1.0  # 基础重连延迟（秒）
    WS_RECONNECT_MAX_DELAY = 30.0  # 最大重连延迟（秒）

    def __init__(self, strategy_config: Dict[str, Any]):
        self.config = strategy_config
        self.strategy_id = strategy_config.get("strategy_id", "default")
        self.is_live = strategy_config.get("is_live", False)
        self.client = PolyClient(is_live=self.is_live)
        self.active_trades: Dict[str, Dict[str, Any]] = {}

        # 线程安全锁
        self._trades_lock = threading.RLock()
        self._markets_lock = threading.RLock()

        # 策略参数注入
        self.entry_max_price = strategy_config.get(
            "entry_max_price", INITIAL_ENTRY_MAX_PRICE
        )
        self.reentry_trigger = strategy_config.get(
            "reentry_trigger", REENTRY_TRIGGER_PRICE
        )
        self.order_amount = strategy_config.get("amount", ORDER_AMOUNT)
        self.max_slippage_tolerance = strategy_config.get("max_slippage_tolerance", MAX_SLIPPAGE_TOLERANCE)
        self.leg1_max_unhedged_seconds = strategy_config.get("leg1_max_unhedged_seconds", LEG1_MAX_UNHEDGED_SECONDS)
        self.max_concurrent_unhedged_trades = strategy_config.get("max_concurrent_unhedged_trades", MAX_CONCURRENT_UNHEDGED_TRADES)
        self.processed_markets = set()
        # ── P0新增：启动时从 DB 自动恢复 processed_markets ──────────────────
        try:
            from polymarket.config import DB_PATH
            from polymarket import db as _db
            self._db_path = DB_PATH
            _db.init_db(self._db_path)
            with _db.get_conn(self._db_path) as conn:
                rows = conn.execute(
                    "SELECT market_id FROM processed_markets WHERE strategy_id=?",
                    (self.strategy_id,)
                ).fetchall()
            for r in rows:
                self.processed_markets.add(r[0])
            if self.processed_markets:
                logger.info(
                    f"[策略：{self.strategy_id}] 从 DB 恢复 {len(self.processed_markets)} 个已处理市场"
                )
        except Exception as e:
            logger.warning(f"[策略：{self.strategy_id}] 启动从 DB 恢复 processed_markets 失败: {e}")
        
        # 订单确认配置
        self.order_confirm_timeout = 5.0  # 订单确认超时（秒）
        self.order_confirm_interval = 0.2  # 订单确认轮询间隔（秒）
        
        # 下单方式配置（新增）
        self.leg1_order_type = strategy_config.get("leg1_order_type", "FOK")  # 首腿订单类型：FOK(吃单) / GTC(挂单)
        self.leg2_order_type = strategy_config.get("leg2_order_type", "GTC")  # 二腿订单类型：FOK(吃单) / GTC(挂单)
        self.leg2_price_mode = strategy_config.get("leg2_price_mode", "bid")   # 二腿价格模式：ask(吃单价) / bid(挂单价)
        self.leg2_cancel_before_expiry = strategy_config.get("leg2_cancel_before_expiry", 30)  # 二腿挂单到期前取消时间
        self.leg2_fallback_to_taker = strategy_config.get("leg2_fallback_to_taker", True)  # 挂单取消后是否改为吃单
        
        # 挂单状态跟踪
        self.pending_orders: Dict[str, Dict[str, Any]] = {}  # market_id -> order_info
        self._orders_lock = threading.RLock()

    @staticmethod
    def _parse_ws_prices(data) -> Dict[str, float]:
        """
        从 Polymarket WS 消息中提取所有 asset 的 best_ask。

        消息格式：
        1) book 快照 (list):  [{"asset_id": "...", "asks": [...], "event_type": "book"}, ...]
        2) book 更新 (dict):  {"asset_id": "...", "asks": [...], "event_type": "book"}
        3) price_change (dict): {"event_type": "price_change", "price_changes": [
               {"asset_id": "...", "best_ask": "0.51", ...}, ...]}

        Returns:
            {asset_id: best_ask} 映射，可能包含多个 token
        """
        results: Dict[str, float] = {}

        if isinstance(data, dict) and data.get("event_type") == "price_change":
            for pc in data.get("price_changes", []):
                aid = pc.get("asset_id")
                raw_ask = pc.get("best_ask")
                if aid and raw_ask is not None:
                    try:
                        results[str(aid)] = float(raw_ask)
                    except (ValueError, TypeError):
                        pass
            return results

        items = data if isinstance(data, list) else [data] if isinstance(data, dict) else []

        for item in items:
            if not isinstance(item, dict):
                continue

            asset_id = item.get("asset_id") or item.get("token_id")
            asks = item.get("asks")

            if not asset_id or not asks:
                continue

            try:
                prices = [float(a["price"]) for a in asks if isinstance(a, dict) and "price" in a]
                if prices:
                    results[str(asset_id)] = min(prices)
            except (ValueError, TypeError, KeyError):
                continue

        return results

    @staticmethod
    def _parse_ws_prices_full(data) -> Dict[str, Dict[str, float]]:
        """
        从 Polymarket WS 消息中提取所有 asset 的 best_ask 和 best_bid。

        Returns:
            {asset_id: {"ask": best_ask, "bid": best_bid}} 映射
        """
        results: Dict[str, Dict[str, float]] = {}

        items = data if isinstance(data, list) else [data] if isinstance(data, dict) else []

        for item in items:
            if not isinstance(item, dict):
                continue

            asset_id = item.get("asset_id") or item.get("token_id")
            if not asset_id:
                continue

            asks = item.get("asks", [])
            bids = item.get("bids", [])

            try:
                best_ask = min((float(a["price"]) for a in asks if isinstance(a, dict) and "price" in a), default=1.0)
                best_bid = max((float(b["price"]) for b in bids if isinstance(b, dict) and "price" in b), default=0.0)
                results[str(asset_id)] = {"ask": best_ask, "bid": best_bid}
            except (ValueError, TypeError, KeyError):
                continue

        return results

    @staticmethod
    def _check_orderbook_depth_and_vwap(
        asks: List[Dict[str, Any]],
        target_usdc_amount: float,
        max_price_threshold: float,
        max_slippage_tolerance: float = 0.015,
    ) -> Tuple[bool, float, float, str]:
        """
        根据盘口 Ask 深度计算买入指定 USDC 金额时的加权平均成交价 (VWAP)。

        Args:
            asks: Orderbook 中的 asks 列表 [{"price": "0.45", "size": "20"}, ...]
            target_usdc_amount: 拟投入的 USDC 金额
            max_price_threshold: 最高可接受单点价格 (如 0.50)
            max_slippage_tolerance: 相对 best ask 允许的最大滑点比例

        Returns:
            (is_valid, vwap_price, filled_usdc, reason_msg)
        """
        if not asks or target_usdc_amount <= 0:
            return False, 1.0, 0.0, "Ask 盘口为空或投入金额不合法"

        # 按价格升序排序
        valid_asks = []
        for a in asks:
            if isinstance(a, dict) and "price" in a and "size" in a:
                try:
                    p = float(a["price"])
                    s = float(a["size"])
                    if p > 0 and s > 0:
                        valid_asks.append((p, s))
                except (ValueError, TypeError):
                    continue

        valid_asks.sort(key=lambda x: x[0])
        if not valid_asks:
            return False, 1.0, 0.0, "无有效的 Ask 报价"

        best_ask = valid_asks[0][0]
        if best_ask > max_price_threshold:
            return False, best_ask, 0.0, f"Best ask {best_ask:.4f} 已超过最高限价 {max_price_threshold:.4f}"

        remaining_usdc = target_usdc_amount
        total_tokens = 0.0
        total_spent_usdc = 0.0

        for price, size in valid_asks:
            layer_max_usdc = price * size
            if remaining_usdc <= layer_max_usdc:
                # 这一层足以覆盖剩余部分
                tokens_bought = remaining_usdc / price
                total_tokens += tokens_bought
                total_spent_usdc += remaining_usdc
                remaining_usdc = 0.0
                break
            else:
                total_tokens += size
                total_spent_usdc += layer_max_usdc
                remaining_usdc -= layer_max_usdc

        if remaining_usdc > 0.001:
            return False, best_ask, total_spent_usdc, f"盘口深度不足，仅可买入 {total_spent_usdc:.2f}/{target_usdc_amount:.2f} USDC"

        vwap = total_spent_usdc / total_tokens if total_tokens > 0 else 1.0
        slippage = (vwap - best_ask) / best_ask if best_ask > 0 else 0.0

        if vwap > max_price_threshold:
            return False, vwap, total_spent_usdc, f"VWAP {vwap:.4f} 超出最高买入限价 {max_price_threshold:.4f}"

        if slippage > max_slippage_tolerance:
            return False, vwap, total_spent_usdc, f"VWAP 滑点 {slippage:.2%} 超出容忍上限 {max_slippage_tolerance:.2%}"

        return True, vwap, total_spent_usdc, "OK"

    def _get_unhedged_trade_count(self) -> int:
        """获取当前处于未对冲（leg1_only 或 monitoring）状态的交易数量。"""
        with self._trades_lock:
            count = 0
            for trade in self.active_trades.values():
                status = trade.get("status")
                if status in ("leg1_only", "monitoring", "leg1_filled"):
                    count += 1
            return count

    def _calculate_backoff_delay(self, attempt: int) -> float:
        """
        计算指数退避延迟。
        
        公式：min(base_delay * 2^attempt, max_delay)
        """
        delay = self.WS_RECONNECT_BASE_DELAY * (2 ** attempt)
        return min(delay, self.WS_RECONNECT_MAX_DELAY)

    @staticmethod
    def _build_ws_proxy_kwargs() -> dict:
        """根据 HTTPS_PROXY 配置构建 websockets 代理参数。"""
        if not HTTPS_PROXY:
            return {}
        try:
            from urllib.parse import urlparse
            parsed = urlparse(HTTPS_PROXY)
            host = parsed.hostname
            port = parsed.port
            if not host or not port:
                return {}
            import python_socks  # noqa: F401 — 检查依赖是否安装
            from websockets.asyncio.client import connect as ws_connect  # noqa: F401
            import socks
            return {
                "sock": None,
                "additional_headers": {},
            }
        except ImportError:
            return {}

    async def _ws_connect_with_retry(
        self, 
        uri: str, 
        max_attempts: int = None
    ) -> Optional[websockets.WebSocketClientProtocol]:
        """
        带重试的 WebSocket 连接，支持 HTTP 代理。
        
        Args:
            uri: WebSocket URI
            max_attempts: 最大重试次数，None 表示无限重试
            
        Returns:
            成功连接的 WebSocket 对象，失败返回 None
        """
        if max_attempts is None:
            max_attempts = self.WS_RECONNECT_MAX_ATTEMPTS
            
        for attempt in range(max_attempts):
            try:
                if HTTPS_PROXY:
                    ws = await self._ws_connect_via_proxy(uri)
                else:
                    ws = await websockets.connect(uri, open_timeout=20)
                if attempt > 0:
                    logger.info(f"[策略：{self.strategy_id}] [WS] 重连成功 (attempt={attempt + 1})")
                return ws
            except Exception as e:
                delay = self._calculate_backoff_delay(attempt)
                logger.warning(
                    f"[策略：{self.strategy_id}] [WS] 连接失败 (attempt={attempt + 1}/{max_attempts}): {e}, "
                    f"{delay:.1f}s 后重试"
                )
                await asyncio.sleep(delay)
        
        logger.error(f"[策略：{self.strategy_id}] [WS] 重连失败，已达最大尝试次数 ({max_attempts})")
        return None

    @staticmethod
    async def _ws_connect_via_proxy(uri: str):
        """通过 HTTP CONNECT 代理建立 WebSocket 连接。"""
        import ssl
        import socket
        from urllib.parse import urlparse

        proxy = urlparse(HTTPS_PROXY)
        target = urlparse(uri)
        target_host = target.hostname
        target_port = target.port or 443

        sock = socket.create_connection((proxy.hostname, proxy.port), timeout=20)
        connect_req = (
            f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
            f"Host: {target_host}:{target_port}\r\n\r\n"
        )
        sock.sendall(connect_req.encode())

        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("Proxy closed connection during CONNECT")
            response += chunk

        status_line = response.split(b"\r\n")[0].decode()
        if "200" not in status_line:
            sock.close()
            raise ConnectionError(f"Proxy CONNECT failed: {status_line}")

        # HTTP CONNECT 代理隧道建立完成，将原始 TCP socket 传给 websockets.connect，
        # 由 websockets 库原生完成 TLS/wss 握手，解决 Socket cannot be of type SSLSocket 报错
        return await websockets.connect(
            uri,
            sock=sock,
            open_timeout=20,
        )

    def _is_market_processed(self, market_id: str) -> bool:
        """线程安全地检查市场是否已处理。"""
        with self._markets_lock:
            return market_id in self.processed_markets

    def _mark_market_processed(self, market_id: str) -> None:
        """线程安全地标记市场已处理（内存 + DB 双写持久化）[P0修复]。"""
        with self._markets_lock:
            self.processed_markets.add(market_id)
        try:
            from polymarket import db as _db
            _db.mark_market_processed(market_id, self.strategy_id, self._db_path)
        except Exception as e:
            logger.warning(f"[策略：{self.strategy_id}] 持久化 processed_markets 失败: {e}")

    def _get_trade(self, market_id: str) -> Optional[Dict[str, Any]]:
        """线程安全地获取交易。"""
        with self._trades_lock:
            return self.active_trades.get(market_id)

    def _set_trade(self, market_id: str, trade: Dict[str, Any]) -> None:
        """线程安全地设置交易。"""
        with self._trades_lock:
            self.active_trades[market_id] = trade

    def _update_trade_status(self, market_id: str, status: str, **kwargs) -> None:
        """线程安全地更新交易状态。"""
        with self._trades_lock:
            trade = self.active_trades.get(market_id)
            if trade:
                if status is not None:
                    trade["status"] = status
                for key, value in kwargs.items():
                    trade[key] = value

    def _get_all_active_trades(self) -> Dict[str, Dict[str, Any]]:
        """线程安全地获取所有活跃交易（副本）。"""
        with self._trades_lock:
            return {k: v.copy() for k, v in self.active_trades.items()}

    def _confirm_order_filled(self, order_id: str) -> bool:
        """
        确认订单是否已成交。
        
        Args:
            order_id: 订单 ID
            
        Returns:
            True 如果订单已成交，False 否则
        """
        if not self.is_live:
            # 模拟模式直接返回成功
            return True
            
        start_time = time.time()
        while time.time() - start_time < self.order_confirm_timeout:
            try:
                # 查询订单状态
                order_status = self.client.get_order_status(order_id)
                if order_status == "FILLED":
                    logger.info(f"[策略：{self.strategy_id}] 订单 {order_id} 已确认成交")
                    return True
                elif order_status in ("CANCELLED", "EXPIRED", "REJECTED"):
                    logger.warning(f"[策略：{self.strategy_id}] 订单 {order_id} 状态异常: {order_status}")
                    return False
            except Exception as e:
                logger.warning(f"[策略：{self.strategy_id}] 查询订单状态失败: {e}")
            
            time.sleep(self.order_confirm_interval)
        
        logger.error(f"[策略：{self.strategy_id}] 订单 {order_id} 确认超时")
        return False

    def _check_order_filled_once(self, order_id: str) -> str:
        """
        非阻塞检查一次订单状态。
        返回 "FILLED", "FAILED", "PENDING" 之一。
        """
        if not self.is_live:
            return "FILLED"
        try:
            order_status = self.client.get_order_status(order_id)
            if order_status == "FILLED":
                logger.info(f"[策略：{self.strategy_id}] 订单 {order_id} 已确认成交")
                return "FILLED"
            elif order_status in ("CANCELLED", "EXPIRED", "REJECTED"):
                logger.warning(f"[策略：{self.strategy_id}] 订单 {order_id} 状态异常: {order_status}")
                return "FAILED"
            else:
                return "PENDING"
        except Exception as e:
            logger.warning(f"[策略：{self.strategy_id}] 查询订单状态失败: {e}")
            return "PENDING"

    def _calculate_dynamic_stop_price(self, token_id: str) -> float:
        """
        计算动态止损价格。
        
        作为买入（BUY）方吃单止损，基于当前盘口的 ask（卖一）价格计算，并加上适当滑点（例如 5% 溢价）。
        FOK 市价单只要出价 >= 卖一，就会以最优价格成交。
        
        Args:
            token_id: Token ID
            
        Returns:
            止损价格
        """
        try:
            prices = self.client.get_market_price(token_id)
            if prices and prices.get("ask", 0) > 0:
                # 取当前 ask 的 105%，但最高不超过 0.99
                stop_price = min(prices["ask"] * 1.05, 0.99)
                logger.debug(f"[策略：{self.strategy_id}] 动态止损价格: {stop_price:.4f} (ask={prices['ask']:.4f})")
                return stop_price
        except Exception as e:
            logger.warning(f"[策略：{self.strategy_id}] 计算动态止损价格失败: {e}")
            
        # 如果出错了，默认最高价吃单
        return 0.99

