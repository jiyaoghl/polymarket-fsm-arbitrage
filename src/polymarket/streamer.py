import asyncio
import json
import threading
import time
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
    _lock = threading.RLock()

    @classmethod
    def get_instance(cls, ws_uri: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market") -> "MarketDataStreamer":
        """获取或创建全局单例数据总线实例。"""
        return cls(ws_uri=ws_uri)

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
            
            # 订阅补偿与静默看门狗状态
            self._invalid_op_retries = 0
            self._last_market_data_ts = time.time()
            
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
                self._invalid_op_retries = 0
                self._last_market_data_ts = time.time()
                
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
                        # ── 静默看门狗 (Silence Watchdog) ──
                        # 若存在活跃资产但超过 15 秒完全未收到任何盘口数据，触发看门狗主动重新下发订阅
                        with self._lock:
                            has_assets = bool(self.active_assets)
                        now_ts = time.time()
                        if has_assets and (now_ts - self._last_market_data_ts > 15.0):
                            logger.warning(
                                "[Streamer] 活跃市场静默超过 15s 未收到任何盘口数据，触发看门狗主动补偿重新订阅..."
                            )
                            self._last_market_data_ts = now_ts  # 重置避免高频触发
                            self._schedule_resubscribe(delay=0.2)
                        continue
                    except websockets.exceptions.ConnectionClosed as e:
                        logger.warning(f"[Streamer] 远端关闭 ({e.code}): {e.reason}")
                        break
                        
                    # 核心解析：全局只做一次
                    try:
                        if msg in ("OK", "PONG", ""):
                            continue
                        if msg == "INVALID OPERATION":
                            self._invalid_op_retries += 1
                            # 指数退避: 1.5s -> 2.25s -> 3.38s -> 上限 5.0s
                            backoff_delay = min(1.5 * (1.5 ** (self._invalid_op_retries - 1)), 5.0)
                            with self._lock:
                                cur_assets = list(self.active_assets)
                            if self._invalid_op_retries <= 5:
                                logger.warning(
                                    f"[Streamer] 远端返回 INVALID OPERATION (撮合端初始化时延，重试第 {self._invalid_op_retries} 次)，"
                                    f"将在 {backoff_delay:.2f}s 后自动补偿重新订阅 (当前监控 {len(cur_assets)} 个资产)..."
                                )
                                if self._invalid_op_retries >= 3:
                                    logger.warning(f"[Streamer] [诊断详情] 当前重试订阅的资产 Token 列表: {cur_assets}")
                                self._schedule_resubscribe(delay=backoff_delay)
                            else:
                                logger.error(
                                    f"[Streamer] 连续 {self._invalid_op_retries} 次收到 INVALID OPERATION，暂停高频重试，"
                                    f"降级为 15s 周期保底探针。资产列表: {cur_assets}"
                                )
                                self._schedule_resubscribe(delay=15.0)
                            continue

                        data = json.loads(msg)
                        # 成功接收到正常行情，刷新看门狗并解除异常退避状态
                        self._last_market_data_ts = time.time()
                        if self._invalid_op_retries > 0:
                            logger.info("[Streamer] 成功接收到正常盘口行情，INVALID OPERATION 状态解除，重置补偿重试计数器。")
                            self._invalid_op_retries = 0

                        # 更新全局共享盘口内存网格
                        updated_tokens = OrderbookMemoryGrid.get_instance().update_from_ws(data)
                        prices = BaseStrategy._parse_ws_prices_full(data)
                        
                        if not prices and updated_tokens:
                            prices = {}
                            for tid in updated_tokens:
                                snap = OrderbookMemoryGrid.get_instance().get_snapshot(tid)
                                if snap and snap.best_bid is not None and snap.best_ask is not None:
                                    prices[tid] = {"ask": snap.best_ask, "bid": snap.best_bid}

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

    def _schedule_resubscribe(self, delay: float = 0.5):
        """防抖与补偿定时器：跨线程安全调度，支持自定义延迟合并订阅，防止瞬间密集请求触发 INVALID OPERATION"""
        def _arm_timer():
            if self._resubscribe_handle is not None:
                try:
                    self._resubscribe_handle.cancel()
                except Exception:
                    pass
            
            def _do_send():
                if self.ws:
                    with self._lock:
                        assets = list(self.active_assets)
                    if assets:
                        self.runtime.spawn_task(
                            self._send_subscription(self.ws, assets),
                            key="Streamer_Resubscribe"
                        )
            
            self._resubscribe_handle = self.loop.call_later(delay, _do_send)

        try:
            self.loop.call_soon_threadsafe(_arm_timer)
        except Exception as e:
            logger.warning(f"[Streamer] 跨线程调度订阅异常: {e}")

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

    def purge_expired_markets(self, active_market_ids: Set[str]):
        """主动清理已到期/已不在活跃列表中的市场和资产，防止陈旧 Token 引发 INVALID OPERATION 订阅被拒。"""
        with self._lock:
            stale_markets = set(self.subscribers.keys()) - set(active_market_ids)
            for sm in stale_markets:
                self.subscribers.pop(sm, None)

            new_asset_to_markets = {}
            for asset, markets in self.asset_to_markets.items():
                alive_markets = markets.intersection(active_market_ids)
                if alive_markets:
                    new_asset_to_markets[asset] = alive_markets
            self.asset_to_markets = new_asset_to_markets
            self.active_assets = set(self.asset_to_markets.keys())
            
            if self.ws:
                self._schedule_resubscribe()
