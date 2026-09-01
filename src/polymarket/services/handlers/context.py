import time
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, Callable, List

from polymarket.logger import logger
from polymarket import risk_logger
from polymarket.client import PolyClient
from polymarket.risk_manager import RiskManager
from polymarket.services.repository import TradeRepository
from polymarket.domain.models import TradeContext

@dataclass(frozen=True)
class StrategyParams:
    """
    不可变策略参数模型 (Strategy Configuration Parameters)。
    """
    strategy_id: str
    amount: float
    entry_max_price: float
    entry_min_price: float
    reentry_trigger: float
    is_live: bool
    leg1_order_type: str
    leg2_order_type: str
    leg2_price_mode: str
    dual_bracket_entry: bool
    max_slippage_tolerance: float
    leg1_max_unhedged_seconds: float
    max_concurrent_unhedged_trades: int
    exit_mode: str
    initial_margin: float
    breakeven_margin: float
    flip_timeout_sec: float
    min_time_to_expiry_entry: float
    # ── 新增微观结构可配置门槛 (带安全默认值) ──
    open_silence_sec: float = 15.0
    max_spread: float = 0.05
    mm_min_bid: float = 0.38
    obi_floor: float = -0.40
    base_opp_depth: float = 20.0
    opp_depth_amp_mult: float = 1.5

@dataclass
class StrategyDependencies:
    """
    策略运行时依赖的基础设施与上下文存取回调 (Dependency Injection Bundle)。
    """
    client: PolyClient
    risk_manager: RiskManager
    repository: TradeRepository
    get_trade: Callable[[str], Optional[Dict[str, Any]]]
    set_trade: Callable[[str, Dict[str, Any]], None]
    add_trade_event: Callable[[str, str, str], None]
    update_trade_status: Callable[..., None]
    get_unhedged_count: Callable[[], int]

@dataclass
class TickBundle:
    """
    单次 Tick 盘口快照数据包 (Market Tick Data Bundle)。
    """
    yes_token: str
    no_token: str
    best_ask_yes: float
    best_bid_yes: Optional[float]
    best_ask_no: float
    best_bid_no: Optional[float]
    now_ts: float = field(default_factory=time.time)

class TickFilterLogger:
    """
    跨切面统一静默拦截与风控节流器 (Cross-Cutting Filter Logger)。
    内置 30 秒防刷屏频控，自动记录事件日志并推送到 Risk Logger。
    """
    def __init__(self, strategy_id: str):
        self.strategy_id = strategy_id
        self._last_silent_log: Dict[str, float] = {}

    def intercept(
        self,
        market_id: str,
        asset_type: str,
        reason: str,
        ctx: TradeContext,
        deps: StrategyDependencies,
        level: str = "warning"
    ) -> None:
        """记录静默拦截原因，更新上下文并推送到看板"""
        now_ts = time.time()
        ctx.filter_reason = reason
        deps.set_trade(market_id, ctx.to_dict())

        # 30 秒节流打印与事件记录
        last_log = self._last_silent_log.get(market_id, 0.0)
        if now_ts - last_log > 30.0:
            logger.info(f"[{self.strategy_id}] [静默拦截] {market_id} {reason}")
            deps.add_trade_event(market_id, ctx.status, f"静默拦截: {reason}")
            self._last_silent_log[market_id] = now_ts

        risk_logger.push_risk_event(
            market_id=market_id,
            asset=asset_type or "UNKNOWN",
            strategy=self.strategy_id,
            reason=reason,
            level=level
        )
