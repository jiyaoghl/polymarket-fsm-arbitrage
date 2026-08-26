import asyncio
import json
import threading
from typing import Dict, Any, List, Set, Optional
import websockets

from polymarket.logger import logger
from polymarket.base_strategy import BaseStrategy
from polymarket.runtime import AsyncRuntime, BoundedDropOldestQueue
from polymarket.services.grid import OrderbookMemoryGrid

class MarketDataStreamer:
    """
    全异步单例数据总线 (Multiplexing Event Bus)。
    运行于 AsyncRuntime 全局统一事件循环中。
    全局只维护 1 条 WebSocket 连接，单次解析后通过同 Loop 队列向各市场 FSM 极速分发。
    """
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if not cls._instance:
                cls._instance = super(MarketDataStreamer, cls).__new__(cls)
                cls._instance._initialized = False
        return cls._instance

    def __init__(self, ws_uri: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"):
        with self._lock:
            if self._initialized:
                return
            self.ws_uri = ws_uri
            self.ws: Optional[websockets.WebSocketClientProtocol] = None
            self.active_assets: Set[str] = set()
            # 记录资产与所属市场: asset_id -> set(market_id)
            self.asset_to_markets: Dict[str, Set[str]] = {}
            # subscribers: market_id -> list of asyncio.Queue
            self.subscribers: Dict[str, List[asyncio.Queue]] = {}
            
            # 防抖重订阅 Handle
            self._resubscribe_handle = None
            
            # 接入全局统一异步运行时
            self.runtime = AsyncRuntime.get_instance()
            self.loop = self.runtime.get_loop()
            
            # 启动长驻 WS 监听协程任务
            self.runtime.spawn_task(self._ws_loop(), key="MarketDataStreamer_WS")
            self._initialized = True
            logger.info("[Streamer] 统一数据总线已在全局 AsyncRuntime 中挂载运行。")

    async def _ws_loop(self):
        retry_delay = 1.0
        max_delay = 60.0
        while True:
            try:
                from polymarket.config import HTTPS_PROXY
                logger.info("[Streamer] 统一数据总线正在连接 WS...")
                
                if HTTPS_PROXY:
                    ws_conn = await BaseStrategy._ws_connect_via_proxy(self.ws_uri)
                else:
                    ws_conn = await websockets.connect(self.ws_uri)
                    
                self.ws = ws_conn
                retry_delay = 1.0  # 连接成功，重置退避时间
                
                # 重新发送所有活跃资产订阅
                with self._lock:
                    assets = list(self.active_assets)
                if assets:
                    await self._send_subscription(self.ws, assets)
                    logger.info(f"[Streamer] 已恢复 {len(assets)} 个资产的订阅")

                while True:
                    try:
                        msg = await asyncio.wait_for(self.ws.recv(), timeout=5)
                    except asyncio.TimeoutError:
                        if self.ws:
                            try:
                                await self.ws.ping()
                            except Exception:
                                logger.warning("[Streamer] WS 心跳失败，准备重连...")
                                break
                        continue
                    except websockets.exceptions.ConnectionClosed as e:
                        logger.warning(f"[Streamer] 远端关闭 ({e.code}): {e.reason}")
                        break
                        
                    # 核心解析：全局只做一次
                    try:
                        if msg in ("OK", "PONG", ""):
                            continue
                        if msg == "INVALID OPERATION":
                            logger.warning("[Streamer] 远端返回了 INVALID OPERATION，忽略该消息")
                            continue
                        data = json.loads(msg)
                        # 更新全局共享盘口内存网格
                        OrderbookMemoryGrid.get_instance().update_from_ws(data)
                        prices = BaseStrategy._parse_ws_prices_full(data)
                        if not prices:
                            await asyncio.sleep(0)
                            continue
                    except Exception as e:
                        logger.error(f"[Streamer] 解析异常 msg={msg[:100]}: {e}")
                        await asyncio.sleep(0)
                        continue

                    bundle = {"data": data, "prices": prices}

                    # 同 Loop 微秒级直接投递给相关市场队列
                    dispatched_markets = set()
                    with self._lock:
                        for asset_id in prices.keys():
                            markets = self.asset_to_markets.get(asset_id, set())
                            dispatched_markets.update(markets)
                            
                        for market_id in dispatched_markets:
                            queues = self.subscribers.get(market_id, [])
                            for q in queues:
                                try:
                                    q.put_nowait(bundle)
                                except Exception as e:
                                    logger.warning(f"[Streamer] 推送队列异常 ({market_id}): {e}")

                    await asyncio.sleep(0)

            except Exception as e:
                logger.error(f"[Streamer] 异常崩溃: {e}")
            finally:
                if self.ws:
                    try:
                        await self.ws.close()
                    except Exception:
                        pass
                self.ws = None
            logger.info(f"[Streamer] 将在 {retry_delay} 秒后尝试重连...")
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, max_delay)  # 指数退避

    async def _send_subscription(self, ws: websockets.WebSocketClientProtocol, assets: List[str]):
        """发送订阅命令"""
        if not assets:
            logger.info("[Streamer] 活跃资产列表为空，跳过向远端发送空订阅。")
            return
        if not ws or getattr(ws, "closed", False):
            logger.warning("[Streamer] WebSocket 已断开，跳过发送订阅消息。")
            return
        msg = {
            "type": "market",
            "assets_ids": assets
        }
        try:
            await ws.send(json.dumps(msg))
        except Exception as e:
            logger.warning(f"[Streamer] 发送订阅消息异常: {e}")

    def _schedule_resubscribe(self):
        """防抖定时器：延迟 0.5s 发送聚合订阅，防止瞬间密集请求触发 INVALID OPERATION"""
        if self._resubscribe_handle is not None:
            self._resubscribe_handle.cancel()
            
        def _do_send():
            if self.ws:
                self.runtime.spawn_task(
                    self._send_subscription(self.ws, list(self.active_assets)),
                    key="Streamer_Resubscribe"
                )
        
        self._resubscribe_handle = self.loop.call_later(0.5, _do_send)

    def subscribe(
        self,
        market_id: str,
        assets: List[str],
        caller_queue: asyncio.Queue,
        caller_loop: Optional[asyncio.AbstractEventLoop] = None
    ):
        """策略端调用，注册一个队列"""
        with self._lock:
            # 加入资产映射
            for asset in assets:
                self.active_assets.add(asset)
                if asset not in self.asset_to_markets:
                    self.asset_to_markets[asset] = set()
                self.asset_to_markets[asset].add(market_id)
                
            # 加入订阅者
            if market_id not in self.subscribers:
                self.subscribers[market_id] = []
            if caller_queue not in self.subscribers[market_id]:
                self.subscribers[market_id].append(caller_queue)
            
            if self.ws:
                self._schedule_resubscribe()

    def unsubscribe(self, market_id: str, caller_queue: asyncio.Queue):
        """策略端注销"""
        with self._lock:
            subs = self.subscribers.get(market_id, [])
            self.subscribers[market_id] = [q for q in subs if q is not caller_queue]
            
            if not self.subscribers[market_id]:
                # 移除该市场的空订阅列表
                del self.subscribers[market_id]
                # 当前市场的所有策略都退出了，清理 asset_to_markets 映射
                assets_to_remove = set()
                for asset, markets in list(self.asset_to_markets.items()):
                    if market_id in markets:
                        markets.remove(market_id)
                        if not markets:
                            assets_to_remove.add(asset)
                            del self.asset_to_markets[asset]
                
                # 从 active_assets 中彻底移除这些没有任何市场关心的 token
                if assets_to_remove:
                    self.active_assets.difference_update(assets_to_remove)
                    if self.ws:
                        self._schedule_resubscribe()
