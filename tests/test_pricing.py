import pytest
from polymarket.services.pricing import PricingEngine

def test_calculate_vwap_sufficient_depth():
    asks = [
        {"price": "0.40", "size": "5.0"},
        {"price": "0.42", "size": "10.0"},
    ]
    # 需要 10 份：5 份 @ 0.40 ($2.0) + 5 份 @ 0.42 ($2.1) = $4.1 / 10 = 0.41
    vwap = PricingEngine.calculate_vwap(asks, target_shares=10.0)
    assert vwap == 0.41

def test_calculate_vwap_insufficient_depth():
    asks = [
        {"price": "0.40", "size": "5.0"},
    ]
    vwap = PricingEngine.calculate_vwap(asks, target_shares=10.0)
    assert vwap is None

def test_calculate_net_ev_gtc_gtc():
    # 双方均为 GTC (Maker: 0% 费率)
    # 买入 YES: 10 份 @ 0.45 = $4.50
    # 买入 NO: 10 份 @ 0.50 = $5.00
    # 总成本 $9.50，兑付 $10.00，毛利 $0.50，费率 0
    gross, fee, net = PricingEngine.calculate_net_ev(
        leg1_cost=0.45, leg1_size=10.0,
        leg2_cost=0.50, leg2_size=10.0,
        leg1_order_type="GTC", leg2_order_type="GTC"
    )
    assert gross == 0.50
    assert fee == 0.0
    assert net == 0.50

def test_calculate_net_ev_fok_gtc():
    # 首腿 FOK (1% 费率)，二腿 GTC (0% 费率)
    # YES: 10 @ 0.45 -> fee = 0.045
    # NO: 10 @ 0.50 -> fee = 0.0
    gross, fee, net = PricingEngine.calculate_net_ev(
        leg1_cost=0.45, leg1_size=10.0,
        leg2_cost=0.50, leg2_size=10.0,
        leg1_order_type="FOK", leg2_order_type="GTC"
    )
    assert gross == 0.50
    assert fee == 0.045
    assert net == 0.455

def test_verify_hedged_profitability_pass():
    is_prof, net_ev, msg = PricingEngine.verify_hedged_profitability(
        leg1_cost=0.45, leg1_size=10.0,
        leg2_cost=0.52, leg2_size=10.0,
        min_profit_margin=0.015,
        leg1_order_type="GTC", leg2_order_type="GTC"
    )
    # 成本 0.97，利润 0.03 (3.0% > 1.5%)
    assert is_prof is True
    assert net_ev == 0.30

def test_verify_hedged_profitability_fail():
    is_prof, net_ev, msg = PricingEngine.verify_hedged_profitability(
        leg1_cost=0.49, leg1_size=10.0,
        leg2_cost=0.50, leg2_size=10.0,
        min_profit_margin=0.015,
        leg1_order_type="GTC", leg2_order_type="GTC"
    )
    # 成本 0.99，利润 0.01 (1.0% < 1.5%)
    assert is_prof is False
    assert net_ev == 0.10

def test_calculate_dual_bracket_prices_pass():
    # 盘口利差充裕：双边买一各 +0.001 贴盘挂单
    yes_p, no_p, reason = PricingEngine.calculate_dual_bracket_prices(
        best_bid_yes=0.42,
        best_bid_no=0.56,
        entry_max_price=0.45,
        min_profit_margin=0.015
    )
    # target_yes = 0.421, target_no = 0.561, total_cost = 0.982 <= 0.985
    assert yes_p == 0.421
    assert no_p == 0.561
    assert reason is None

def test_calculate_dual_bracket_prices_overflow():
    # 利差不足 (0.495 + 0.495 = 0.990 > 0.985) 且强行压低单边导致另一侧溢价拦截
    yes_p, no_p, reason = PricingEngine.calculate_dual_bracket_prices(
        best_bid_yes=0.495,
        best_bid_no=0.495,
        entry_max_price=0.45,
        min_profit_margin=0.015
    )
    assert yes_p is None
    assert no_p is None
    assert "溢价过高" in reason

