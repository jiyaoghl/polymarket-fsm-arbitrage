import pytest
from polymarket.strategy_fsm import ArbitrageBotFSM
from polymarket.domain.fsm import TradeState

def test_arbitrage_bot_fsm_initialization():
    config = {
        "strategy_id": "test_bot",
        "is_live": False,
        "entry_max_price": 0.45,
        "reentry_trigger": 0.48,
        "amount": 10.0,
        "leg1_order_type": "GTC",
        "leg2_order_type": "GTC",
        "dual_bracket_entry": True
    }
    bot = ArbitrageBotFSM(config)
    assert bot.strategy_id == "test_bot"
    assert bot.is_live is False
    assert bot.dual_bracket_entry is True
    assert bot.repository is not None
    assert bot.risk_manager is not None

def test_arbitrage_bot_fsm_market_fsm_init():
    config = {
        "strategy_id": "test_bot_2",
        "is_live": False,
        "amount": 10.0
    }
    bot = ArbitrageBotFSM(config)
    fsm = bot._init_fsm_for_market("test_market_123")
    assert fsm.current_state == TradeState.IDLE
    assert fsm.market_id == "test_market_123"
