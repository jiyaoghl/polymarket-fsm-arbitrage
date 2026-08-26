from abc import ABC, abstractmethod
from typing import Dict, Any

from polymarket.domain.fsm import TradeFSM
from polymarket.domain.models import TradeContext
from polymarket.services.handlers.context import StrategyParams, StrategyDependencies, TickBundle, TickFilterLogger

class BaseTickHandler(ABC):
    """
    状态处理器抽象基类 (Base State Handler)。
    规范单状态下的 Tick 决策与执行流。
    """

    @abstractmethod
    async def handle(
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
        处理单次 Tick 并推动状态机流转。
        """
        pass
