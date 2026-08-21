import time
from typing import List, Dict, Any
from collections import deque
import threading

# 线程安全的风控事件队列，最多保留 50 条最新记录，防刷屏
_risk_events_lock = threading.Lock()
_risk_events = deque(maxlen=50)

def push_risk_event(market_id: str, asset: str, strategy: str, reason: str, level: str = "warning"):
    """
    将由于风控导致的静默拦截或防爆盾事件压入全局队列，透传给 Dashboard。
    level: "info", "warning", "error" (前端按此染色)
    """
    with _risk_events_lock:
        _risk_events.append({
            "timestamp": int(time.time()),
            "market_id": market_id,
            "asset": asset,
            "strategy": strategy,
            "reason": reason,
            "level": level
        })

def get_recent_risk_events() -> List[Dict[str, Any]]:
    """获取前端展示用的近期风控事件日志（按时间正序，最旧的在前面，最新的在末尾）。"""
    with _risk_events_lock:
        return list(_risk_events)
