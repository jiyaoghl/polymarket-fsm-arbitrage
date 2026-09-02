import pytest
from polymarket.services.pricing import PricingEngine

def test_evaluate_taker_ev_opportunity_profitable_yes():
    # 场景 1: YES 卖一出现 0.400 (错配低价)，NO 买一为 0.560 (总成本 0.960，在抛物线扣费后仍有丰厚 Net EV)
    # 吃 YES @ 0.400 (FOK) + 挂 NO @ 0.560 (GTC)
    is_opp, side, p, ev, msg = PricingEngine.evaluate_taker_ev_opportunity(
        best_ask_yes=0.400,
        best_bid_yes=0.390,
        best_ask_no=0.580,
        best_bid_no=0.560,
        min_profit_margin=0.010,
        leg1_amount=10.0
    )
    assert is_opp is True
    assert side == 'YES'
    assert p == 0.400
    assert ev > 0.15

def test_evaluate_taker_ev_opportunity_profitable_no():
    # 场景 2: NO 卖一出现 0.400 (错配低价)，YES 买一为 0.560
    is_opp, side, p, ev, msg = PricingEngine.evaluate_taker_ev_opportunity(
        best_ask_yes=0.580,
        best_bid_yes=0.560,
        best_ask_no=0.400,
        best_bid_no=0.390,
        min_profit_margin=0.010,
        leg1_amount=10.0
    )
    assert is_opp is True
    assert side == 'NO'
    assert p == 0.400
    assert ev > 0.15

def test_evaluate_taker_ev_opportunity_unprofitable_symmetric():
    # 场景 3: 对称平价盘口 YES Ask 0.510 / NO Ask 0.500
    is_opp, side, p, ev, msg = PricingEngine.evaluate_taker_ev_opportunity(
        best_ask_yes=0.510,
        best_bid_yes=0.500,
        best_ask_no=0.500,
        best_bid_no=0.490,
        entry_max_price=0.45,
        min_profit_margin=0.015,
        leg1_amount=10.0
    )
    assert is_opp is False

def test_evaluate_taker_ev_opportunity_deep_oversold():
    # 场景 4: 极端单边超跌 YES Ask 0.380
    is_opp, side, p, ev, msg = PricingEngine.evaluate_taker_ev_opportunity(
        best_ask_yes=0.380,
        best_bid_yes=0.360,
        best_ask_no=0.630,
        best_bid_no=0.610,
        entry_max_price=0.40,
        min_profit_margin=0.015,
        leg1_amount=10.0
    )
    assert is_opp is True
    assert side == 'YES'
    assert p == 0.380
