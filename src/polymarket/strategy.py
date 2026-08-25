"""向后兼容桥接模块：将旧版 ArbitrageBot 映射为重构后的 ArbitrageBotFSM"""
from polymarket.strategy_fsm import ArbitrageBotFSM as ArbitrageBot
from polymarket.base_strategy import BaseStrategy

__all__ = ["ArbitrageBot", "BaseStrategy"]
