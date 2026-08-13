import asyncio
import json
import threading
from typing import Dict, Any, List, Set
import websockets

from polymarket.logger import logger
from polymarket.base_strategy import BaseStrategy

class MarketDataStreamer:
    """
    全异步单例数据总线 (Multiplexing Event Bus)。
    全局只维护 1 条 WebSocket 连接。一次解析，分发给多个 FSM 的 asyncio 队列。
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
            self.active_assets: Set[str] = set()
            # 记录资产与所属市场: asset_id -> set(market_id)
            self.asset_to_markets: Dict[str, Set[str]] = {}
            # subscribers: market_id -> list of (queue, loop)
            self.subscribers: Dict[str, List[Dict[str, Any]]] = {}
            
            # 后台守护事件循环
            self.loop = asyncio.new_event_loop()
            self.thread = threading.Thread(target=self._run_loop, daemon=True, name="MarketDataStreamer")
            self.thread.start()
            
            self.ws = None
            self._initialized = True

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._ws_loop())

    async def _ws_loop(self):
        while True:
            try:
                logger.info("[Streamer] 统一数据总线正在连接 WS...")
                async with websockets.connect(self.ws_uri) as ws:
                    self.ws = ws
                    
                    # 重新发送所有活跃的订阅
                    with self._lock:
                        assets = list(self.active_assets)
                    if assets:
                        await self._send_subscription(ws, assets)
                        logger.info(f"[Streamer] 已恢复 {len(assets)} 个资产的订阅")

                    while True:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=5)
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
                            if msg == "OK":
                                continue
                            data = json.loads(msg)
                            prices = BaseStrategy._parse_ws_prices_full(data)
                            if not prices:
                                await asyncio.sleep(0)
                                continue
                        except Exception as e:
                            # 如果确实是解析异常，把原始消息打出来排查
                            logger.error(f"[Streamer] 解析异常 msg={msg[:100]}: {e}")
                            await asyncio.sleep(0)
                            continue

                        bundle = {"data": data, "prices": prices}

                        # 分发给相关的市场队列
                        dispatched_markets = set()
                        with self._lock:
                            for asset_id in prices.keys():
                                markets = self.asset_to_markets.get(asset_id, set())
                                dispatched_markets.update(markets)
                                
                            for market_id in dispatched_markets:
                                subs = self.subscribers.get(market_id, [])
                                for sub in subs:
                                    # 安全地跨线程投递 (fire and forget)
                                    try:
                                        asyncio.run_coroutine_threadsafe(sub["queue"].put(bundle), sub["loop"])
                                    except Exception as e:
                                        logger.warning(f"[Streamer] 跨线程推送队列异常 ({market_id}): {e}")

                        await asyncio.sleep(0)

            except Exception as e:
                logger.error(f"[Streamer] 异常崩溃: {e}")
                
            self.ws = None
            await asyncio.sleep(1)  # 退避重连

    async def _send_subscription(self, ws: websockets.WebSocketClientProtocol, assets: List[str]):
        """发送订阅/重置命令，Polymarket 会全量覆盖"""
        if not assets:
            logger.info("[Streamer] 活跃资产列表为空，跳过向远端发送空订阅。")
            return
        msg = {
            "type": "market",
            "assets_ids": assets,
            "custom_feature_enabled": True
        }
        await ws.send(json.dumps(msg))

    def subscribe(self, market_id: str, assets: List[str], caller_queue: asyncio.Queue, caller_loop: asyncio.AbstractEventLoop):
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
            self.subscribers[market_id].append({"queue": caller_queue, "loop": caller_loop})
            
            # 无论远端是 append 还是 overwrite 模式，直接全量发送 active_assets 最为稳妥
            if self.ws:
                asyncio.run_coroutine_threadsafe(self._send_subscription(self.ws, list(self.active_assets)), self.loop)
                
    def unsubscribe(self, market_id: str, caller_queue: asyncio.Queue):
        """策略端注销"""
        with self._lock:
            subs = self.subscribers.get(market_id, [])
            # 过滤掉退出的队列
            self.subscribers[market_id] = [s for s in subs if s["queue"] is not caller_queue]
            
            if not self.subscribers[market_id]:
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
                    # 重新发送最新的活跃资产列表给远端（Polymarket 重新订阅会全量覆盖）
                    if self.ws:
                        asyncio.run_coroutine_threadsafe(
                            self._send_subscription(self.ws, list(self.active_assets)), 
                            self.loop
                        )
