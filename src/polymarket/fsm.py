from enum import Enum
from typing import Set, Dict
from polymarket.logger import logger

class TradeState(str, Enum):
    IDLE = "idle"                        # 初始状态
    PENDING_LEG1 = "pending"             # 首腿下单中 (向后兼容旧的 pending 语义)
    PENDING_BOTH_LEGS = "pending_both"   # 双腿限价单并发挂单中 (Dual-GTC Bracket)
    LEG1_ONLY = "leg1_only"              # 单边已成交，存在单边敞口
    PENDING_LEG2 = "pending_leg2"        # 二腿追单/挂单中
    LOCKED = "locked"                    # 双腿均已全量成交，成功锁仓
    SETTLED = "settled"                  # 已结算 / 生命周期结束
    FAILED = "failed"                    # 流程失败强制退出

class FSMTransitionError(Exception):
    pass

class TradeFSM:
    """
    量化套利机器人的有限状态机引擎 (Finite State Machine)。
    用于规范订单状态的流转，防止出现非法的状态跳跃（如从 IDLE 直接跳到 LOCKED）。
    """
    
    # 定义合法的状态流转图
    VALID_TRANSITIONS: Dict[TradeState, Set[TradeState]] = {
        TradeState.IDLE: {
            TradeState.PENDING_LEG1,
            TradeState.PENDING_BOTH_LEGS,
            TradeState.SETTLED, # 比如未成交直接废弃
            TradeState.FAILED
        },
        TradeState.PENDING_BOTH_LEGS: {
            TradeState.LOCKED,     # 双腿均被吃单，秒级完成套利
            TradeState.LEG1_ONLY,  # 单腿先成交，另一腿仍在排队或需追单
            TradeState.SETTLED,
            TradeState.FAILED      # 临期未成交批量撤单
        },
        TradeState.PENDING_LEG1: {
            TradeState.LEG1_ONLY, 
            TradeState.SETTLED, # 发送订单失败或未成交即过期
            TradeState.FAILED
        },
        TradeState.LEG1_ONLY: {
            TradeState.PENDING_LEG2,
            TradeState.LOCKED,    # 处于双挂转单边时二腿也迅速成交
            TradeState.SETTLED,   # 比如单腿触发超时止损并成交
            TradeState.FAILED
        },
        TradeState.PENDING_LEG2: {
            TradeState.LOCKED,
            TradeState.LEG1_ONLY, # 二腿撤单或者超时，可能退回 LEG1_ONLY
            TradeState.SETTLED,   # 或者在这期间超时平仓
            TradeState.FAILED
        },
        TradeState.LOCKED: {
            TradeState.SETTLED,
            TradeState.FAILED
        },
        TradeState.SETTLED: set(), # 终态
        TradeState.FAILED: set(),  # 终态
    }

    def __init__(self, market_id: str, initial_state: TradeState = TradeState.IDLE):
        self.market_id = market_id
        self._state = initial_state
        self._handlers = {}  # event_name -> callback

    @property
    def current_state(self) -> TradeState:
        return self._state

    def register_transition_hook(self, target_state: TradeState, callback):
        """注册状态切入时的回调。"""
        self._handlers[target_state] = callback

    def transition_to(self, new_state: TradeState, **kwargs) -> bool:
        """
        验证并执行状态转移，若合法则触发注册的回调函数。
        """
        if self._state == new_state:
            return True

        allowed = self.VALID_TRANSITIONS.get(self._state, set())
        if new_state not in allowed:
            logger.error(
                f"[FSM] 拒绝非法状态流转 ({self.market_id}): {self._state.value} -> {new_state.value}"
            )
            return False

        old_state = self._state
        self._state = new_state
        logger.info(f"[FSM] 状态已流转 ({self.market_id}): {old_state.value} -> {new_state.value}")
        
        # 触发该状态的钩子函数
        handler = self._handlers.get(new_state)
        if handler:
            try:
                handler(self, **kwargs)
            except Exception as e:
                logger.error(f"[FSM] 状态 {new_state.value} 钩子执行异常 ({self.market_id}): {e}")
        return True

