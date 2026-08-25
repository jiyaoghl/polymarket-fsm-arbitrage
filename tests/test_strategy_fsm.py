import pytest
from polymarket.strategy_fsm import ArbitrageBotFSM
from polymarket.domain.fsm import TradeState

def make_fsm_config(**kwargs):
    cfg = {
        "strategy_id": "test_bot",
        "name": "测试FSM策略",
        "amount": 10.0,
        "entry_max_price": 0.45,
        "entry_min_price": 0.25,
        "reentry_trigger": 0.48,
        "is_live": False,
        "leg1_order_type": "GTC",
        "leg2_order_type": "GTC",
        "leg2_price_mode": "bid",
        "exit_mode": "dual_exit",
        "initial_margin": 0.025,
        "breakeven_margin": 0.002,
        "flip_timeout_sec": 35.0,
        "leg2_cancel_before_expiry": 30,
        "leg2_fallback_to_maker": True,
        "dual_bracket_entry": True
    }
    cfg.update(kwargs)
    return cfg

def test_arbitrage_bot_fsm_initialization():
    config = make_fsm_config()
    bot = ArbitrageBotFSM(config)
    assert bot.strategy_id == "test_bot"
    assert bot.is_live is False
    assert bot.dual_bracket_entry is True
    assert bot.repository is not None
    assert bot.risk_manager is not None

def test_arbitrage_bot_fsm_market_fsm_init():
    config = make_fsm_config(strategy_id="test_bot_2")
    bot = ArbitrageBotFSM(config)
    fsm = bot._init_fsm_for_market("test_market_123")
    assert fsm.current_state == TradeState.IDLE
    assert fsm.market_id == "test_market_123"
