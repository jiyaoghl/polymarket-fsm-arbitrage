"""
Polymarket 回测与离线标定领域数据模型。
"""

from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Any, Optional


@dataclass
class SnapshotFrame:
    """单帧不可变快照模型 (1 帧/秒 L2 订单簿深度与资产 K 线)"""
    ts: float
    token_id: str
    best_bid: Optional[float]
    best_ask: Optional[float]
    bids: List[Tuple[float, float]]
    asks: List[Tuple[float, float]]
    spread: float
    mid_price: float
    obi: float
    kline: Dict[str, Any] = field(default_factory=dict)
    asset: Optional[str] = None


@dataclass
class EvalResult:
    """参数组离线高保真评测指标结果"""
    params: Dict[str, Any]
    total_trades: int = 0
    hedged_locked_count: int = 0
    smart_flip_count: int = 0
    liquidated_count: int = 0
    total_net_ev: float = 0.0
    avg_net_margin: float = 0.0
    win_rate: float = 0.0
    score: float = 0.0
