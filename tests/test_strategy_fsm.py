import time
import pytest
from unittest.mock import MagicMock, patch

from polymarket.strategy_fsm import ArbitrageBotFSM
from polymarket.fsm import TradeState

@pytest.fixture
def mock_strategy_config():
    return {
        "strategy_id": "test_fsm_bot",
        "entry_max_price": 0.50,
        "reentry_trigger": 0.40,
        "amount": 10.0,
        "leg1_max_unhedged_seconds": 1,  # 设为 1 秒以加速测试超时
    }

def test_fsm_timeout_daemon(mock_strategy_config):
    """
    测试全局超时守护线程能够在一秒钟后，主动将处于 LEG1_ONLY 状态的订单熔断，
    流转为 PENDING_LEG2（即准备发止损单）。
    """
    bot = ArbitrageBotFSM(mock_strategy_config)
    
    # 模拟进入某个市场
    market_id = "0xtestmarket123"
    fsm = bot._init_fsm_for_market(market_id)
    
    # 手动建立一个处于单边敞口的 trade 状态
    # 模拟在两秒前就已经成交了首腿，也就是绝对超时了
    past_time = time.time() - 2.0 
    bot.active_trades[market_id] = {
        "status": TradeState.LEG1_ONLY.value,
        "leg1_filled_time": past_time,
    }
    
    # 强制让 FSM 进入 LEG1_ONLY
    fsm.transition_to(TradeState.PENDING_LEG1)
    fsm.transition_to(TradeState.LEG1_ONLY)
    
    # 守护线程 _fsm_timeout_daemon 是 daemon，每 1s 扫一次
    # 等待 2.5 秒，让子线程有充足时间扫出超时
    time.sleep(2.5)
    
    # 如果超时监控运作正常，现在的状态应该由于超时被强行转化为了 PENDING_LEG2 (止损中)
    # 因为超时强制触发：fsm.transition_to(TradeState.PENDING_LEG2, is_stop_loss=True)
    assert fsm.current_state == TradeState.PENDING_LEG2
    # 并且 active_trades 中的状态也被钩子同步更新
    assert bot.active_trades[market_id]["status"] == TradeState.PENDING_LEG2.value
