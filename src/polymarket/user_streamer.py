import asyncio
import json
import threading
import time
from typing import Dict, Any, Optional, Set
import websockets

from polymarket.logger import logger
from polymarket import config
from polymarket.base_strategy import BaseStrategy
from polymarket.runtime import AsyncRuntime

USER_WS_URI = "wss://ws-subscriptions-clob.polymarket.com/ws/user"

class UserOrderStreamer:
    """
    Polymarket 私有 WebSocket 用户订单与成交事件流 (User Channel)。
    运行于 AsyncRuntime 全局统一事件循环中。
    
    职责：
    1. 自动维护私有 WS 鉴权长连接与心跳保活。
    2. 接收毫秒级订单状态推送 (PLACEMENT, UPDATE, CANCELLATION, FILL)。
    3. 为上层策略提供毫秒级 `wait_for_order_fill(order_id)` 极速唤醒机制。
    4. 模拟模式与异常场景下提供安全降级机制。
    """
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if not cls._instance:
                cls._instance = super(UserOrderStreamer, cls).__new__(cls)
                cls._instance._initialized = False
        return cls._instance

    def __init__(self, ws_uri: str = USER_WS_URI):
        with self._lock:
            if self._initialized:
                return
            self.ws_uri = ws_uri
            self.ws: Optional[websockets.WebSocketClientProtocol] = None
            
            # 监听中的订单: order_id -> asyncio.Event
            self._pending_fill_events: Dict[str, asyncio.Event] = {}
            self._fill_results: Dict[str, Dict[str, Any]] = {}
            
            # 运行状态
            self.is_running = False
            self.is_authenticated = False
            self._auth_failed = False
            
            # 接入全局统一异步运行时
            self.runtime = AsyncRuntime.get_instance()
            self.loop = self.runtime.get_loop()
            
            # 挂载常驻监听任务
            self.runtime.spawn_task(self._ws_loop(), key="UserOrderStreamer_WS")
            self._initialized = True
            logger.info("[UserStreamer] 私有订单事件流单例已挂载至全局 AsyncRuntime。")

    @classmethod
    def get_instance(cls) -> "UserOrderStreamer":
        return cls()

    async def _ws_loop(self):
        retry_delay = 1.0
        max_delay = 60.0
        self.is_running = True

        while self.is_running:
            # 检查是否有 API Key 凭证
            api_key = config.API_KEY
            api_secret = config.API_SECRET
            api_passphrase = config.API_PASSPHRASE

            if not api_key or not api_secret or not api_passphrase:
                # 未配置 API Key (如纯模拟盘)，每隔 10 秒检测一次
                await asyncio.sleep(10.0)
                continue

            try:
                from polymarket.config import HTTPS_PROXY
                logger.info("[UserStreamer] 正在连接私有订单 WebSocket 频道...")
                
                if HTTPS_PROXY:
                    ws_conn = await BaseStrategy._ws_connect_via_proxy(self.ws_uri)
                else:
                    ws_conn = await websockets.connect(self.ws_uri)
                    
                self.ws = ws_conn
                
                # 发送私有鉴权订阅帧
                auth_payload = {
                    "type": "user",
                    "auth": {
                        "apiKey": api_key,
                        "secret": api_secret,
                        "passphrase": api_passphrase
                    },
                    "markets": []  # 全局监听当前用户的所有市场订单
                }
                await self.ws.send(json.dumps(auth_payload))
                self.is_authenticated = True
                retry_delay = 1.0
                logger.info("[UserStreamer] 私有订单 WebSocket 鉴权订阅成功，长连接已建立。")

                while self.is_running:
                    try:
                        msg = await asyncio.wait_for(self.ws.recv(), timeout=8)
                    except asyncio.TimeoutError:
                        if self.ws and not getattr(self.ws, "closed", False):
                            try:
                                await self.ws.ping()
                            except Exception:
                                logger.warning("[UserStreamer] 私有 WS 心跳失败，准备重连...")
                                break
                        continue
                    except websockets.exceptions.ConnectionClosed as e:
                        logger.warning(f"[UserStreamer] 远端关闭 ({e.code}): {e.reason}")
                        break

                    # 处理订单事件
                    if msg in ("OK", "PONG", ""):
                        continue
                    if msg == "INVALID OPERATION":
                        logger.warning("[UserStreamer] 远端返回 INVALID OPERATION (鉴权或格式异常)")
                        continue

                    try:
                        data = json.loads(msg)
                        self._handle_incoming_event(data)
                    except Exception as e:
                        logger.error(f"[UserStreamer] 解析私有事件异常 ({msg[:100]}): {e}")

            except Exception as e:
                logger.error(f"[UserStreamer] 私有 WebSocket 异常: {e}")
            finally:
                self.is_authenticated = False
                if self.ws:
                    try:
                        await self.ws.close()
                    except Exception:
                        pass
                self.ws = None

            logger.info(f"[UserStreamer] 将在 {retry_delay:.1f} 秒后尝试重新连接...")
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, max_delay)

    def _handle_incoming_event(self, data: Any):
        """处理来自 Polymarket User WebSocket 的事件"""
        if isinstance(data, list):
            for item in data:
                self._process_single_event(item)
        elif isinstance(data, dict):
            self._process_single_event(data)

    def _process_single_event(self, event: Dict[str, Any]):
        """解析单条订单推送事件并唤醒等待协程"""
        order_id = str(event.get("order_id") or event.get("id") or event.get("orderID") or "")
        status = str(event.get("status") or event.get("event_type") or "").upper()
        
        # 兼容 trades 推送
        if "trade" in status.lower() or status in ("FILLED", "MATCHED", "TRADE"):
            status = "FILLED"

        logger.info(f"[UserStreamer] 收到订单事件: order_id={order_id}, status={status}")

        with self._lock:
            # 检查是否有协程在等待该订单
            matched_id = None
            if order_id in self._pending_fill_events:
                matched_id = order_id
            else:
                # 模糊匹配 (防止大小写或 0x 前缀差异)
                for pid in self._pending_fill_events.keys():
                    if pid.lower() == order_id.lower() or pid in order_id or order_id in pid:
                        matched_id = pid
                        break

            if matched_id:
                self._fill_results[matched_id] = {
                    "order_id": order_id,
                    "status": status,
                    "size": float(event.get("size") or event.get("amount") or event.get("matched_size") or 0.0),
                    "price": float(event.get("price") or 0.0),
                    "raw": event,
                    "timestamp": time.time(),
                }
                event_obj = self._pending_fill_events[matched_id]
                self.loop.call_soon_threadsafe(event_obj.set)

    async def wait_for_order_fill(self, order_id: str, timeout: float = 10.0) -> Optional[Dict[str, Any]]:
        """
        异步等待指定订单成交 (由私有 WebSocket 事件直接驱动)。
        
        Returns:
            Dict 包含成交信息，若超时或未成交则返回 None。
        """
        if not order_id:
            return None

        # 创建针对此订单的等待 Event
        fill_event = asyncio.Event()
        with self._lock:
            self._pending_fill_events[order_id] = fill_event
            # 若已有历史缓存结果
            if order_id in self._fill_results:
                res = self._fill_results.pop(order_id)
                self._pending_fill_events.pop(order_id, None)
                return res

        try:
            # 等待事件触发
            await asyncio.wait_for(fill_event.wait(), timeout=timeout)
            with self._lock:
                return self._fill_results.pop(order_id, None)
        except asyncio.TimeoutError:
            return None
        finally:
            with self._lock:
                self._pending_fill_events.pop(order_id, None)
