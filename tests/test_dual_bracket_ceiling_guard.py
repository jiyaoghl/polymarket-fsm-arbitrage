import pytest
from src.polymarket.services.pricing import PricingEngine
from src.polymarket.services.pegging import MakerPeggingService



def test_calculate_dual_bracket_prices_rejects_single_side_above_entry_max():
    """验证当不对称盘口导致某一边计算价格超过 entry_max_price 时，必须被 100% 拒绝"""
    # 模拟导致此前线上暴雷的盘口 (0.45 / 0.53)，若不加防线，NO 侧会挂出 0.52+
    yes_p, no_p, err = PricingEngine.calculate_dual_bracket_prices(
        best_bid_yes=0.45,
        best_bid_no=0.53,
        entry_max_price=0.47,
        entry_min_price=0.28,
        min_profit_margin=0.025,
        best_ask_yes=0.47,
        best_ask_no=0.55
    )
    assert yes_p is None
    assert no_p is None
    assert err is not None
    assert "单边挂单价格超限" in err


def test_calculate_dual_bracket_prices_accepts_healthy_symmetric_bracket():
    """验证健康且双边均在安全通道内的盘口正常生成双挂定价"""
    yes_p, no_p, err = PricingEngine.calculate_dual_bracket_prices(
        best_bid_yes=0.46,
        best_bid_no=0.46,
        entry_max_price=0.48,
        entry_min_price=0.28,
        min_profit_margin=0.025,
        best_ask_yes=0.48,
        best_ask_no=0.48
    )
    assert err is None
    assert yes_p is not None and yes_p <= 0.48
    assert no_p is not None and no_p <= 0.48
    assert round(yes_p + no_p, 4) <= round(1.0 - 0.025, 4)


def test_order_pegging_strictly_obeys_entry_max_price():
    """验证动态贴盘跟价在买一持续抬升时，绝不突破 entry_max_price 挂单"""
    # 尝试把当前 0.46 的挂单推升到超过 0.47
    should_repeg, new_yes, new_no, reason = MakerPeggingService.calculate_dual_bracket_repeg_prices(
        current_yes_price=0.46,


        current_no_price=0.46,
        best_bid_yes=0.465,
        best_bid_no=0.53,  # 对手 NO 侧大幅走高
        entry_max_price=0.47,
        entry_min_price=0.28,
        min_profit_margin=0.025,
        best_ask_yes=0.49,
        best_ask_no=0.55
    )
    # 因为 NO 侧超限，必须坚决拒绝改单，保持原挂单或等待
    assert should_repeg is False
    assert new_yes == 0.46
    assert new_no == 0.46
