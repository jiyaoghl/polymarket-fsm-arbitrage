import time
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional, Any

from polymarket.logger import logger
from polymarket.services.pricing import PricingEngine

@dataclass(frozen=True)
class OrderbookSnapshot:
    """
    不可变强类型盘口快照 (Immutable Orderbook Snapshot)。
    支持多线程无锁零拷贝并发读取 (Lock-Free Read)。
    """
    token_id: str
    best_bid: Optional[float]
    best_ask: Optional[float]
    # bids: 按价格降序排列的 [(price, size), ...]
    bids: Tuple[Tuple[float, float], ...] = ()
    # asks: 按价格升序排列的 [(price, size), ...]
    asks: Tuple[Tuple[float, float], ...] = ()
    spread: float = 0.0
    mid_price: float = 0.0
    obi: float = 0.0  # 订单簿不平衡度 (Top 20 Levels)
    last_update_ts: float = field(default_factory=time.time)

    def is_stale(self, max_age_seconds: float = 10.0) -> bool:
        """检查快照是否陈旧过期"""
        if max_age_seconds <= 0:
            return False
        return (time.time() - self.last_update_ts) > max_age_seconds


class OrderbookMemoryGrid:
    """
    全局共享盘口内存网格 (Orderbook Memory Grid)。
    以单例模式运行。
    
    核心职责：
    1. 集中式维护所有活跃 Token 的 L2 订单簿深度与买卖一价；
    2. L2 档位动态校准器 (L2 Ladder Reconciler)：增量更新时自动修剪脏档位，杜绝盘口倒挂；
    3. 零 I/O 毫秒级本地 VWAP 与 OBI 计算；
    4. 时效性防爆盾 (Stale Guard) 与内存自动垃圾回收 (Auto-TTL GC)。
    """
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(OrderbookMemoryGrid, cls).__new__(cls)
                cls._instance._initialized = False
        return cls._instance

    def __init__(self, max_depth_levels: int = 20):
        with self._lock:
            if self._initialized:
                return

            self.max_depth_levels = max_depth_levels
            # 存储不可变快照映射: token_id -> OrderbookSnapshot
            self._books: Dict[str, OrderbookSnapshot] = {}
            self._write_lock = threading.Lock()
            self._last_gc_ts = time.time()
            self._initialized = True
            logger.info(f"[OrderbookGrid] 全局共享盘口内存网格初始化完毕 (Max Depth: {self.max_depth_levels})。")

    @classmethod
    def get_instance(cls) -> "OrderbookMemoryGrid":
        return cls()

    def update_from_ws(self, data: Any) -> List[str]:
        """
        解析 WS 消息（book 全量快照、增量、price_change）并原子更新内存网格。
        
        Returns:
            更新成功的 token_id 列表
        """
        now_ts = time.time()
        updated_tokens = []

        # 1. 处理 price_change 增量消息
        if isinstance(data, dict) and data.get("event_type") == "price_change":
            with self._write_lock:
                for pc in data.get("price_changes", []):
                    aid = str(pc.get("asset_id") or "")
                    if not aid:
                        continue
                    
                    raw_ask = pc.get("best_ask")
                    raw_bid = pc.get("best_bid")
                    
                    new_ask = float(raw_ask) if raw_ask is not None else None
                    new_bid = float(raw_bid) if raw_bid is not None else None
                    
                    # 获取该 Token 当前旧快照
                    old_snap = self._books.get(aid)
                    snapshot = self._reconcile_price_change(aid, new_bid, new_ask, old_snap, now_ts)
                    self._books[aid] = snapshot
                    updated_tokens.append(aid)

            self._maybe_gc(now_ts)
            return updated_tokens

        # 2. 处理 book 快照或消息列表
        items = data if isinstance(data, list) else [data] if isinstance(data, dict) else []

        with self._write_lock:
            for item in items:
                if not isinstance(item, dict):
                    continue

                aid = str(item.get("asset_id") or item.get("token_id") or "")
                if not aid:
                    continue

                raw_asks = item.get("asks", [])
                raw_bids = item.get("bids", [])

                # 解析 asks (升序)
                parsed_asks = []
                for a in raw_asks:
                    if isinstance(a, dict) and "price" in a:
                        p, s = float(a["price"]), float(a.get("size", 0.0))
                        if s > 0:
                            parsed_asks.append((p, s))
                    elif isinstance(a, (list, tuple)) and len(a) >= 2:
                        p, s = float(a[0]), float(a[1])
                        if s > 0:
                            parsed_asks.append((p, s))
                parsed_asks.sort(key=lambda x: x[0])
                parsed_asks = parsed_asks[:self.max_depth_levels]

                # 解析 bids (降序)
                parsed_bids = []
                for b in raw_bids:
                    if isinstance(b, dict) and "price" in b:
                        p, s = float(b["price"]), float(b.get("size", 0.0))
                        if s > 0:
                            parsed_bids.append((p, s))
                    elif isinstance(b, (list, tuple)) and len(b) >= 2:
                        p, s = float(b[0]), float(b[1])
                        if s > 0:
                            parsed_bids.append((p, s))
                parsed_bids.sort(key=lambda x: x[0], reverse=True)
                parsed_bids = parsed_bids[:self.max_depth_levels]

                best_ask = parsed_asks[0][0] if parsed_asks else None
                best_bid = parsed_bids[0][0] if parsed_bids else None

                # 计算价差与中间价
                spread = round(best_ask - best_bid, 4) if (best_ask is not None and best_bid is not None) else 0.0
                mid = round((best_ask + best_bid) / 2.0, 4) if (best_ask is not None and best_bid is not None) else 0.5

                # 计算 OBI (Top 20 档订单簿不平衡度)
                total_bid_size = sum(s for _, s in parsed_bids)
                total_ask_size = sum(s for _, s in parsed_asks)
                denom = total_bid_size + total_ask_size
                obi = round((total_bid_size - total_ask_size) / denom, 4) if denom > 0 else 0.0

                snapshot = OrderbookSnapshot(
                    token_id=aid,
                    best_bid=best_bid,
                    best_ask=best_ask,
                    bids=tuple(parsed_bids),
                    asks=tuple(parsed_asks),
                    spread=spread,
                    mid_price=mid,
                    obi=obi,
                    last_update_ts=now_ts
                )
                self._books[aid] = snapshot
                updated_tokens.append(aid)

        self._maybe_gc(now_ts)
        return updated_tokens

    def _reconcile_price_change(
        self,
        token_id: str,
        new_bid: Optional[float],
        new_ask: Optional[float],
        old_snap: Optional[OrderbookSnapshot],
        now_ts: float
    ) -> OrderbookSnapshot:
        """
        L2 档位动态校准器 (L2 Ladder Reconciler)。
        修剪旧深度中发生倒挂的脏档位，并更新最佳买卖一价。
        """
        cur_bids = list(old_snap.bids) if old_snap else []
        cur_asks = list(old_snap.asks) if old_snap else []

        effective_bid = new_bid if new_bid is not None else (old_snap.best_bid if old_snap else None)
        effective_ask = new_ask if new_ask is not None else (old_snap.best_ask if old_snap else None)

        # 1. 修剪与校准 asks (清除所有 <= effective_bid 的倒挂卖单)
        if effective_bid is not None:
            cur_asks = [lvl for lvl in cur_asks if lvl[0] > effective_bid]
        if effective_ask is not None:
            # 确保首档卖单为最新 best_ask
            if not cur_asks or cur_asks[0][0] != effective_ask:
                cur_asks = [(effective_ask, 10.0)] + [lvl for lvl in cur_asks if lvl[0] > effective_ask]

        # 2. 修剪与校准 bids (清除所有 >= effective_ask 的倒挂买单)
        if effective_ask is not None:
            cur_bids = [lvl for lvl in cur_bids if lvl[0] < effective_ask]
        if effective_bid is not None:
            # 确保首档买单为最新 best_bid
            if not cur_bids or cur_bids[0][0] != effective_bid:
                cur_bids = [(effective_bid, 10.0)] + [lvl for lvl in cur_bids if lvl[0] < effective_bid]

        cur_bids.sort(key=lambda x: x[0], reverse=True)
        cur_asks.sort(key=lambda x: x[0])

        spread = round(effective_ask - effective_bid, 4) if (effective_ask is not None and effective_bid is not None) else 0.0
        mid = round((effective_ask + effective_bid) / 2.0, 4) if (effective_ask is not None and effective_bid is not None) else 0.5

        total_bid_size = sum(s for _, s in cur_bids)
        total_ask_size = sum(s for _, s in cur_asks)
        denom = total_bid_size + total_ask_size
        obi = round((total_bid_size - total_ask_size) / denom, 4) if denom > 0 else 0.0

        return OrderbookSnapshot(
            token_id=token_id,
            best_bid=effective_bid,
            best_ask=effective_ask,
            bids=tuple(cur_bids[:self.max_depth_levels]),
            asks=tuple(cur_asks[:self.max_depth_levels]),
            spread=spread,
            mid_price=mid,
            obi=obi,
            last_update_ts=now_ts
        )

    def get_snapshot(self, token_id: str) -> Optional[OrderbookSnapshot]:
        """无锁快速获取指定 Token 的不可变盘口快照 (Lock-Free Read)"""
        return self._books.get(token_id)

    def get_prices(
        self, yes_token: str, no_token: str, max_staleness: float = 10.0
    ) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
        """
        一键获取双边买卖价格元组：(best_ask_yes, best_bid_yes, best_ask_no, best_bid_no)
        若快照过期则返回 None 触发安全防护。
        """
        snap_yes = self.get_snapshot(str(yes_token))
        snap_no = self.get_snapshot(str(no_token))

        if not snap_yes or snap_yes.is_stale(max_staleness):
            ask_yes, bid_yes = None, None
        else:
            ask_yes, bid_yes = snap_yes.best_ask, snap_yes.best_bid

        if not snap_no or snap_no.is_stale(max_staleness):
            ask_no, bid_no = None, None
        else:
            ask_no, bid_no = snap_no.best_ask, snap_no.best_bid

        return ask_yes, bid_yes, ask_no, bid_no

    def calculate_bid_vwap_and_marginal_local(
        self, token_id: str, target_shares: float, max_staleness: float = 5.0
    ) -> Tuple[Optional[float], Optional[float], float]:
        """
        基于本地内存 L2 深度穿透买盘，0 网络 I/O 计算:
        (vwap_price, marginal_price, filled_shares)
        若数据缺失或陈旧超过 5.0s，返回 None 触发上层安全降级至 REST。
        """
        snap = self.get_snapshot(str(token_id))
        if not snap or snap.is_stale(max_staleness) or not snap.bids:
            return None, None, 0.0

        return PricingEngine.calculate_bid_vwap_and_marginal(list(snap.bids), target_shares)

    def calculate_bid_vwap_local(
        self, token_id: str, target_shares: float, max_staleness: float = 5.0
    ) -> Optional[float]:
        """
        基于本地内存 L2 深度穿透买盘，0 网络 I/O 计算市价平仓 VWAP 加权均价。
        若数据缺失或陈旧超过 5.0s，返回 None 触发上层安全降级。
        """
        vwap, _, _ = self.calculate_bid_vwap_and_marginal_local(token_id, target_shares, max_staleness)
        return vwap

    def calculate_ask_vwap_local(
        self, token_id: str, target_shares: float, max_staleness: float = 5.0
    ) -> Optional[float]:
        """
        基于本地内存 L2 深度穿透卖盘，0 网络 I/O 计算买入吃单 VWAP 加权均价。
        """
        snap = self.get_snapshot(str(token_id))
        if not snap or snap.is_stale(max_staleness) or not snap.asks:
            return None

        return PricingEngine.calculate_vwap(list(snap.asks), target_shares)

    def get_obi(self, token_id: str, max_staleness: float = 10.0) -> float:
        """获取指定 Token 的订单簿失衡度 OBI ([-1.0, 1.0])，陈旧或无数据返回 0.0"""
        snap = self.get_snapshot(str(token_id))
        if not snap or snap.is_stale(max_staleness):
            return 0.0
        return snap.obi

    def get_depth_volume(self, token_id: str, max_staleness: float = 10.0) -> Tuple[float, float]:
        """获取指定 Token 的前 N 档总买盘份数与总卖盘份数 (total_bid_shares, total_ask_shares)"""
        snap = self.get_snapshot(str(token_id))
        if not snap or snap.is_stale(max_staleness):
            return 0.0, 0.0
        tot_bid = sum(s for _, s in snap.bids)
        tot_ask = sum(s for _, s in snap.asks)
        return round(tot_bid, 2), round(tot_ask, 2)

    def purge_stale_tokens(self, ttl_seconds: float = 900.0) -> int:
        """清理超过 ttl_seconds 未更新的过期 Token 盘口快照"""
        now_ts = time.time()
        purged = 0
        with self._write_lock:
            for token_id, snap in list(self._books.items()):
                if now_ts - snap.last_update_ts > ttl_seconds:
                    del self._books[token_id]
                    purged += 1
        if purged > 0:
            logger.info(f"[OrderbookGrid] 自动垃圾回收：清理了 {purged} 个过期 Token 盘口。")
        return purged

    def _maybe_gc(self, now_ts: float) -> None:
        """定期触发垃圾回收（每 3 分钟检查一次）"""
        if now_ts - self._last_gc_ts > 180.0:
            self._last_gc_ts = now_ts
            if len(self._books) > 20:
                self.purge_stale_tokens(ttl_seconds=600.0)

