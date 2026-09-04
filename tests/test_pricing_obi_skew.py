import pytest
from polymarket.services.pricing import PricingEngine


def test_balanced_obi_symmetric_quotes():
    """测试均衡盘口 (OBI=0.0) 下执行标准对称贴盘"""
    yes_p, no_p, err = PricingEngine.calculate_dual_bracket_prices(
        best_bid_yes=0.450,
        best_bid_no=0.450,
        entry_max_price=0.480,
        entry_min_price=0.280,
        min_profit_margin=0.020,
        best_ask_yes=0.460,
        best_ask_no=0.460,
        anti_penny_step=0.001,
        obi_yes=0.0
    )
    assert err is None
    # 对称贴买一 + 0.001
    assert yes_p == pytest.approx(0.451, abs=1e-4)
    assert no_p == pytest.approx(0.451, abs=1e-4)
    assert (yes_p + no_p) <= 1.0 - 0.020


def test_yes_dominant_obi_skews_no_downward():
    """测试 YES 强势 (OBI=+0.50) 时，劣势侧 NO 挂单下退，YES 不追高防毒流"""
    # 均衡时
    yes_bal, no_bal, _ = PricingEngine.calculate_dual_bracket_prices(
        best_bid_yes=0.420,
        best_bid_no=0.440,
        entry_max_price=0.460,
        entry_min_price=0.280,
        min_profit_margin=0.020,
        best_ask_yes=0.435,
        best_ask_no=0.455,
        anti_penny_step=0.001,
        obi_yes=0.0
    )
    
    # 倾斜时: YES 强势
    yes_skew, no_skew, err = PricingEngine.calculate_dual_bracket_prices(
        best_bid_yes=0.420,
        best_bid_no=0.440,
        entry_max_price=0.460,
        entry_min_price=0.280,
        min_profit_margin=0.020,
        best_ask_yes=0.435,
        best_ask_no=0.455,
        anti_penny_step=0.001,
        obi_yes=0.50
    )
    assert err is None
    # 优势侧 YES 保持紧贴买一不追高
    assert yes_skew == yes_bal
    # 劣势侧 NO 挂单向下让步，低于原对称挂单
    assert no_skew < no_bal
    assert (yes_skew + no_skew) <= 1.0 - 0.020


def test_no_dominant_obi_skews_yes_downward():
    """测试 NO 强势 (OBI=-0.50) 时，劣势侧 YES 挂单下退"""
    yes_bal, no_bal, _ = PricingEngine.calculate_dual_bracket_prices(
        best_bid_yes=0.440,
        best_bid_no=0.420,
        entry_max_price=0.460,
        entry_min_price=0.280,
        min_profit_margin=0.020,
        best_ask_yes=0.455,
        best_ask_no=0.435,
        anti_penny_step=0.001,
        obi_yes=0.0
    )
    
    yes_skew, no_skew, err = PricingEngine.calculate_dual_bracket_prices(
        best_bid_yes=0.440,
        best_bid_no=0.420,
        entry_max_price=0.460,
        entry_min_price=0.280,
        min_profit_margin=0.020,
        best_ask_yes=0.455,
        best_ask_no=0.435,
        anti_penny_step=0.001,
        obi_yes=-0.50
    )
    assert err is None
    # 劣势侧 YES 挂单向下让步
    assert yes_skew < yes_bal
    # 优势侧 NO 保持贴买一
    assert no_skew == no_bal
    assert (yes_skew + no_skew) <= 1.0 - 0.020


def test_obi_skew_respects_entry_limits_and_profit_margin():
    """测试在极端失衡时仍严格服从保利底线与 entry_max_price / entry_min_price"""
    yes_p, no_p, err = PricingEngine.calculate_dual_bracket_prices(
        best_bid_yes=0.460,
        best_bid_no=0.460,
        entry_max_price=0.470,
        entry_min_price=0.280,
        min_profit_margin=0.030,
        best_ask_yes=0.475,
        best_ask_no=0.475,
        anti_penny_step=0.001,
        obi_yes=0.85
    )
    assert err is None
    assert yes_p <= 0.470
    assert no_p <= 0.470
    assert yes_p >= 0.280
    assert no_p >= 0.280
    assert (yes_p + no_p) <= 1.0 - 0.030 + 1e-6
