import pytest
from polymarket.services.pricing import PricingEngine


def test_mqs_boost_when_depth_guard_passed():
    """测试高评分且深度满足 >= 80 份防线时，成功放大 1.5x 头寸"""
    score, mult, reason = PricingEngine.calculate_market_quality_score(
        spread=0.012,                # 满分 35
        total_depth_5_levels=120.0,  # 满分 35
        obi=0.05,                    # 满分 10
        asset_amplitude=0.05,        # 极平稳
        max_amplitude_threshold=0.35,
        min_depth_for_boost=80.0
    )
    assert score >= 85.0
    assert mult == 1.5
    assert "头寸放大 1.5x" in reason


def test_mqs_capped_at_1x_when_depth_below_guard():
    """测试即便综合得分极高 (>=80)，但深度 < 80 份时，物理硬防线强制钳制在 1.0x 基准规模"""
    score, mult, reason = PricingEngine.calculate_market_quality_score(
        spread=0.010,                # 满分 35
        total_depth_5_levels=75.0,   # 深度为 75 份，不足 80 份硬防线
        obi=0.02,                    # 满分 10
        asset_amplitude=0.04,        # 满分 20
        max_amplitude_threshold=0.35,
        min_depth_for_boost=80.0
    )
    assert score >= 80.0
    # 强制退回 1.0x 基准规模
    assert mult == 1.0
    assert "深度不足防线" in reason


def test_mqs_standard_market():
    """测试标准盘口 (50 <= score < 80) 维持 1.0x 头寸"""
    score, mult, reason = PricingEngine.calculate_market_quality_score(
        spread=0.030,
        total_depth_5_levels=50.0,
        obi=0.20,
        asset_amplitude=0.15,
        max_amplitude_threshold=0.35
    )
    assert 50.0 <= score < 80.0
    assert mult == 1.0
    assert "维持 1.0x 基准头寸" in reason


def test_mqs_thin_market_contracts_to_half():
    """测试薄弱盘口 (score < 65) 资金紧缩至 0.5x 防御试探"""
    score, mult, reason = PricingEngine.calculate_market_quality_score(
        spread=0.045,                # 宽价差
        total_depth_5_levels=28.0,   # 贴近 25 份低线
        obi=0.45,                    # 明显偏单边
        asset_amplitude=0.30,        # 接近波动上限
        max_amplitude_threshold=0.35
    )
    assert score < 65.0
    assert mult == 0.5
    assert "头寸紧缩为 0.5x" in reason
