import pytest
from polymarket.base_strategy import BaseStrategy


def test_vwap_calculation_and_slippage_rejection():
    """测试薄盘口下 VWAP 加权均价计算及滑点超限自动拒绝。"""
    # 模拟薄盘口：卖一价格极低但量极少，卖二卖三价格急剧飙升
    thin_asks = [
        {"price": "0.30", "size": "1.0"},   # 0.30 USDC, 1 份
        {"price": "0.45", "size": "5.0"},   # 2.25 USDC, 5 份
        {"price": "0.85", "size": "20.0"},  # 高价挂单
    ]

    # 尝试买入 10 USDC（必须吃穿到 0.85 档位）
    is_valid, vwap, filled, msg = BaseStrategy._check_orderbook_depth_and_vwap(
        asks=thin_asks,
        target_usdc_amount=10.0,
        max_price_threshold=0.55,
        max_slippage_tolerance=0.05
    )

    # 应当被拒绝，因为 VWAP 显著超出 0.55 且滑点巨大
    assert is_valid is False
    assert vwap > 0.55
    print(f"[PASS] 薄盘口成功拦截: {msg}")


def test_vwap_sufficient_liquidity():
    """测试充足流动性下的正常 VWAP 计算。"""
    deep_asks = [
        {"price": "0.40", "size": "100.0"},
        {"price": "0.41", "size": "200.0"},
    ]

    is_valid, vwap, filled, msg = BaseStrategy._check_orderbook_depth_and_vwap(
        asks=deep_asks,
        target_usdc_amount=10.0,
        max_price_threshold=0.50,
        max_slippage_tolerance=0.015
    )

    assert is_valid is True
    assert round(vwap, 4) == 0.4000
    assert round(filled, 2) == 10.0
    print("[PASS] 充足流动性计算正常通过")


def test_hedged_profitability_verification():
    """测试双腿锁仓套利确定性回报与净 EV 严格数学校验。"""
    # 案例 1: 完美正 EV 组合 (0.42 + 0.45 = 0.87 < 0.99, FOK+GTC 扣费后 1.26)
    ok1, ev1, msg1 = BaseStrategy._verify_hedged_profitability(
        leg1_cost=0.42,
        leg1_size=10.0,
        leg2_cost=0.45,
        leg2_size=10.0,
        min_profit_margin=0.01,
        leg1_order_type="FOK",
        leg2_order_type="GTC"
    )
    assert ok1 is True
    assert round(ev1, 2) == 1.26  # 1.30 - 0.042(Taker fee) = 1.258 -> 1.26


    # 案例 2: 滑点击穿导致总成本 >= 1.00 (锁亏场景 0.52 + 0.50 = 1.02)
    ok2, ev2, msg2 = BaseStrategy._verify_hedged_profitability(
        leg1_cost=0.52,
        leg1_size=10.0,
        leg2_cost=0.50,
        leg2_size=10.0,
        min_profit_margin=0.01
    )
    assert ok2 is False
    assert ev2 < 0
    print(f"[PASS] 成功拦截潜在锁亏交易: {msg2}")

    # 案例 3: 利润空间低于 1% 最低保底要求 (0.50 + 0.495 = 0.995)
    ok3, ev3, msg3 = BaseStrategy._verify_hedged_profitability(
        leg1_cost=0.50,
        leg1_size=10.0,
        leg2_cost=0.495,
        leg2_size=10.0,
        min_profit_margin=0.01
    )
    assert ok3 is False
    print(f"[PASS] 成功拦截低利润微利交易: {msg3}")


if __name__ == "__main__":
    test_vwap_calculation_and_slippage_rejection()
    test_vwap_sufficient_liquidity()
    test_hedged_profitability_verification()
    print("\n[SUCCESS] 所有执行层深度与数学套利验证测试全部通过！")
