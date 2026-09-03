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

def test_calculate_parabolic_fee_official_benchmark():
    # 1. 官方白皮书基准: 100 份 @ 50¢ 加密货币市场费用为 $1.75
    fee_50c = PricingEngine.calculate_parabolic_fee(price=0.50, size=100.0, fee_rate=0.07)
    assert fee_50c == 1.75

    # 2. 抛物线对称性: 10¢ 与 90¢ 费用完全对称 (100 * 0.07 * 0.1 * 0.9 = 0.63)
    fee_10c = PricingEngine.calculate_parabolic_fee(price=0.10, size=100.0, fee_rate=0.07)
    fee_90c = PricingEngine.calculate_parabolic_fee(price=0.90, size=100.0, fee_rate=0.07)
    assert fee_10c == 0.63
    assert fee_90c == 0.63

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
    # 首腿 FOK (7% 抛物线费率)，二腿 GTC (0% 费率)
    # YES: 10 @ 0.45 -> fee = 10 * 0.07 * 0.45 * 0.55 = 0.17325 -> 0.1733
    # NO: 10 @ 0.50 -> fee = 0.0
    gross, fee, net = PricingEngine.calculate_net_ev(
        leg1_cost=0.45, leg1_size=10.0,
        leg2_cost=0.50, leg2_size=10.0,
        leg1_order_type="FOK", leg2_order_type="GTC"
    )
    assert gross == 0.50
    assert fee == 0.1733
    assert net == 0.3267

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
    # 盘口利差充裕：双边买一各 +0.001 贴盘挂单 (且均处于 entry_max_price 以内)
    yes_p, no_p, reason = PricingEngine.calculate_dual_bracket_prices(
        best_bid_yes=0.42,
        best_bid_no=0.56,
        entry_max_price=0.58,
        min_profit_margin=0.015
    )
    # target_yes = 0.421, target_no = 0.561, total_cost = 0.982 <= 0.985
    assert yes_p == 0.421
    assert no_p == 0.561
    assert reason is None

def test_calculate_dual_bracket_prices_overflow():
    # 利差不足 (0.495 + 0.495 = 0.990 > 0.985) 且强行压低单边导致另一侧超限或溢价拦截
    yes_p, no_p, reason = PricingEngine.calculate_dual_bracket_prices(
        best_bid_yes=0.495,
        best_bid_no=0.495,
        entry_max_price=0.45,
        min_profit_margin=0.015
    )
    assert yes_p is None
    assert no_p is None
    assert "溢价过高" in reason or "单边挂单价格超限" in reason


def test_calculate_obi():
    # 1. 深度充足且买盘占优
    bids = [(0.45, 60.0), (0.44, 40.0)]
    asks = [(0.46, 20.0), (0.47, 30.0)]
    obi, tot, valid = PricingEngine.calculate_obi(bids, asks, top_n_levels=5, min_total_shares=30.0)
    # bid_vol = 100, ask_vol = 50, total = 150, obi = (100-50)/150 = 0.3333
    assert valid is True
    assert tot == 150.0
    assert obi == 0.3333

    # 2. 卖盘严重压迫
    bids_thin = [(0.45, 10.0), (0.44, 10.0)]
    asks_heavy = [(0.46, 80.0), (0.47, 50.0)]
    obi, tot, valid = PricingEngine.calculate_obi(bids_thin, asks_heavy, top_n_levels=5, min_total_shares=30.0)
    # bid_vol = 20, ask_vol = 130, total = 150, obi = (20-130)/150 = -0.7333
    assert valid is True
    assert obi < -0.40

    # 3. 深度不足 (冷启动)
    bids_sparse = [(0.45, 5.0)]
    asks_sparse = [(0.46, 10.0)]
    obi, tot, valid = PricingEngine.calculate_obi(bids_sparse, asks_sparse, top_n_levels=5, min_total_shares=30.0)
    assert valid is False
    assert tot == 15.0

def test_calculate_decayed_margin_power_law():
    # 初始 0.025，保底 0.002，窗口 30s
    # t = 0s -> 0.025
    m0 = PricingEngine.calculate_decayed_margin(0.0, initial_margin=0.025, min_margin=0.002, decay_duration=30.0)
    assert m0 == 0.025

    # t = 10s (1/3 时间) -> 幂律衰减较少，保持较高利润
    m10 = PricingEngine.calculate_decayed_margin(10.0, initial_margin=0.025, min_margin=0.002, decay_duration=30.0)
    assert m10 > 0.020

    # t = 30s (到期) -> 达到最低保底利润 0.002
    m30 = PricingEngine.calculate_decayed_margin(30.0, initial_margin=0.025, min_margin=0.002, decay_duration=30.0)
    assert m30 == 0.002

def test_anti_pennying_best_ask_clamping():
    # 当买一 0.498，卖一 0.499 时，step=0.002 不得越过卖一价 (必须 <= 0.499 - 0.001 = 0.498)
    yes_p, no_p, reason = PricingEngine.calculate_dual_bracket_prices(
        best_bid_yes=0.40,
        best_bid_no=0.498,
        best_ask_yes=0.42,
        best_ask_no=0.499,
        anti_penny_step=0.002,
        min_profit_margin=0.015
    )
    assert no_p <= 0.498
    assert yes_p == 0.402
    assert reason is None

def test_calculate_adaptive_flip_duration():
    # 1. 超平稳期 (ratio <= 0.35) -> 适度拉长至 45s~50s
    dur_calm = PricingEngine.calculate_adaptive_flip_duration(
        base_duration=35.0, asset_amplitude=0.05, max_amplitude_threshold=0.30
    )
    assert dur_calm >= 40.0

    # 2. 微波动期 (ratio >= 0.70) -> 压缩至 18s~25s
    dur_volatile = PricingEngine.calculate_adaptive_flip_duration(
        base_duration=35.0, asset_amplitude=0.28, max_amplitude_threshold=0.30
    )
    assert dur_volatile <= 25.0

    # 3. 标准震荡期 -> 维持 35s
    dur_std = PricingEngine.calculate_adaptive_flip_duration(
        base_duration=35.0, asset_amplitude=0.15, max_amplitude_threshold=0.30
    )
    assert dur_std == 35.0


