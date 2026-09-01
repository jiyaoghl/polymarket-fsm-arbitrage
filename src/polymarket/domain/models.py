from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List
import time

@dataclass
class LegPosition:
    """单腿持仓/订单明细"""
    order_id: Optional[str] = None
    token: Optional[str] = None
    side: str = "BUY"
    cost: float = 0.0
    size: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "order_id": self.order_id,
            "token": self.token,
            "side": self.side,
            "cost": self.cost,
            "size": self.size,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> Optional["LegPosition"]:
        if not data:
            return None
        return cls(
            order_id=data.get("order_id"),
            token=data.get("token") or data.get("token_id"),
            side=data.get("side", "BUY"),
            cost=float(data.get("cost") or data.get("price") or 0.0),
            size=float(data.get("size") or data.get("amount") or 0.0),
        )

@dataclass
class TradeContext:
    """
    统一的交易上下文领域模型 (Single Source of Truth)。
    封装单个市场在 FSM 状态机流转期间的所有上下文状态。
    提供 100% 兼容旧版 Dashboard 的 to_dict() 序列化方法。
    """
    market_id: str
    status: str = "idle"
    asset: str = ""
    start_time: float = field(default_factory=time.time)
    end_time: float = 0.0
    tokens: Dict[str, str] = field(default_factory=dict)
    
    leg1: Optional[LegPosition] = None
    leg2: Optional[LegPosition] = None
    
    leg1_dir: str = ""
    leg2_dir: str = ""
    
    leg1_filled_time: Optional[float] = None
    leg2_issued_time: Optional[float] = None
    leg2_order_id: Optional[str] = None
    
    dual_orders: List[Dict[str, Any]] = field(default_factory=list)
    dual_issued_time: Optional[float] = None
    
    profit_usdc: float = 0.0
    gross_profit_usdc: float = 0.0
    fee_usdc: float = 0.0
    
    realized_pnl: Optional[float] = None
    settlement_price: Optional[float] = None
    settlement_type: Optional[str] = None  # HEDGED_LOCKED | FORCE_CLOSED | EXPIRY_RESOLVED | FAILED
    
    dynamic_ttl: Optional[float] = None
    dynamic_flip_timeout: Optional[float] = None
    last_reprice_time: Optional[float] = None
    reprice_count: int = 0
    reprice_history: List[Dict[str, Any]] = field(default_factory=list)
    filter_reason: Optional[str] = None
    exit_mode: str = "smart_flip"  # smart_flip | pair_only
    exit_stage: str = "init"       # init | flip_active | hedge_fallback | settled
    events: List[Dict[str, Any]] = field(default_factory=list)

    def add_event(self, state: str, description: str):
        """记录状态事件日志"""
        self.events.append({
            "timestamp": time.time(),
            "state": state,
            "description": description
        })

    def record_reprice(self, old_price: float, new_price: float, reason: str, token: str = "", timestamp: Optional[float] = None):
        """结构化记录二腿追单改价轨迹"""
        self.reprice_count += 1
        ts = timestamp if timestamp is not None else time.time()
        self.last_reprice_time = ts
        self.reprice_history.append({
            "timestamp": ts,
            "old_price": round(old_price, 4),
            "new_price": round(new_price, 4),
            "reason": reason,
            "token": str(token)
        })


    def to_dict(self) -> Dict[str, Any]:
        """向后兼容转换为旧版字典结构，供 Dashboard 和 DB 存储无缝读取"""
        now = time.time()
        time_to_expiry = max(0.0, self.end_time - now) if self.end_time > 0 else 0.0
        
        return {
            "market_id": self.market_id,
            "status": self.status,
            "asset": self.asset,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "time_to_expiry": round(time_to_expiry, 1),
            "tokens": self.tokens,
            "leg1": self.leg1.to_dict() if self.leg1 else None,
            "leg2": self.leg2.to_dict() if self.leg2 else None,
            "leg1_dir": self.leg1_dir,
            "leg2_dir": self.leg2_dir,
            "leg1_filled_time": self.leg1_filled_time,
            "leg2_issued_time": self.leg2_issued_time,
            "leg2_order_id": self.leg2_order_id,
            "dual_orders": self.dual_orders,
            "dual_issued_time": self.dual_issued_time,
            "profit_usdc": self.profit_usdc,
            "gross_profit_usdc": self.gross_profit_usdc,
            "fee_usdc": self.fee_usdc,
            "realized_pnl": self.realized_pnl,
            "settlement_price": self.settlement_price,
            "settlement_type": self.settlement_type,
            "dynamic_ttl": self.dynamic_ttl,
            "dynamic_flip_timeout": self.dynamic_flip_timeout,
            "last_reprice_time": self.last_reprice_time,
            "reprice_count": self.reprice_count,
            "reprice_history": self.reprice_history,
            "filter_reason": self.filter_reason,
            "exit_mode": self.exit_mode,
            "exit_stage": self.exit_stage,
            "events": self.events
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TradeContext":
        """从字典还原 TradeContext"""
        ctx = cls(
            market_id=data.get("market_id", ""),
            status=data.get("status", "idle"),
            asset=data.get("asset", ""),
            start_time=float(data.get("start_time", time.time())),
            end_time=float(data.get("end_time", 0.0)),
            tokens=data.get("tokens", {}),
            leg1=LegPosition.from_dict(data.get("leg1")),
            leg2=LegPosition.from_dict(data.get("leg2")),
            leg1_dir=data.get("leg1_dir", ""),
            leg2_dir=data.get("leg2_dir", ""),
            leg1_filled_time=data.get("leg1_filled_time"),
            leg2_issued_time=data.get("leg2_issued_time"),
            leg2_order_id=data.get("leg2_order_id"),
            dual_orders=data.get("dual_orders", []),
            dual_issued_time=data.get("dual_issued_time"),
            profit_usdc=float(data.get("profit_usdc", 0.0)),
            gross_profit_usdc=float(data.get("gross_profit_usdc", 0.0)),
            fee_usdc=float(data.get("fee_usdc", 0.0)),
            realized_pnl=data.get("realized_pnl"),
            settlement_price=data.get("settlement_price"),
            settlement_type=data.get("settlement_type"),
            dynamic_ttl=data.get("dynamic_ttl"),
            dynamic_flip_timeout=data.get("dynamic_flip_timeout"),
            last_reprice_time=data.get("last_reprice_time"),
            reprice_count=int(data.get("reprice_count", 0)),
            reprice_history=data.get("reprice_history", []),
            filter_reason=data.get("filter_reason"),
            exit_mode=data.get("exit_mode", "smart_flip"),
            exit_stage=data.get("exit_stage", "init"),
            events=data.get("events", [])
        )
        return ctx


