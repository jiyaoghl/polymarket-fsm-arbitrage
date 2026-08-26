from typing import Dict, Any, Optional

from polymarket.domain.fsm import TradeFSM, TradeState
from polymarket.domain.models import TradeContext
from polymarket.services.handlers.base import BaseTickHandler
from polymarket.services.handlers.idle_handler import IdleTickHandler
from polymarket.services.handlers.pending_both_handler import PendingBothLegsTickHandler
from polymarket.services.handlers.leg1_only_handler import Leg1OnlyTickHandler
from polymarket.services.handlers.pending_leg2_handler import PendingLeg2TickHandler
from polymarket.services.handlers.context import StrategyParams, StrategyDependencies, TickBundle, TickFilterLogger
from polymarket.metrics import metrics

class MarketTickDispatcher:
    """
    状态处理器注册表与 Tick 分发器 (State Registry Dispatcher)。
    基于当前状态 O(1) 路由至对应的专业 Handler 执行。
    """

    def __init__(self):
        self._handlers: Dict[TradeState, BaseTickHandler] = {
            TradeState.IDLE: IdleTickHandler(),
            TradeState.PENDING_BOTH_LEGS: PendingBothLegsTickHandler(),
            TradeState.LEG1_ONLY: Leg1OnlyTickHandler(),
            TradeState.PENDING_LEG2: PendingLeg2TickHandler(),
        }

    def register_handler(self, state: TradeState, handler: BaseTickHandler) -> None:
        """注册自定义状态处理器"""
        self._handlers[state] = handler

    async def dispatch(
        self,
        market: Dict[str, Any],
        fsm: TradeFSM,
        ctx: TradeContext,
        tick: TickBundle,
        params: StrategyParams,
        deps: StrategyDependencies,
        filter_logger: TickFilterLogger
    ) -> None:
        """
        分发单次 Tick 到当前状态对应的 Handler。
        """
        handler = self._handlers.get(fsm.current_state)
        if handler is not None:
            async with metrics.timer(metrics.tick_process_latency_seconds, labels={"state": fsm.current_state.value}):
                await handler.handle(
                    market=market,
                    fsm=fsm,
                    ctx=ctx,
                    tick=tick,
                    params=params,
                    deps=deps,
                    filter_logger=filter_logger
                )
