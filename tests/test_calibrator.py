import os
import tempfile
import pytest

from scripts.calibrate_params import SnapshotFrame, MathSandbox, GridCalibrator, SnapshotLoader


@pytest.fixture
def sample_frames():
    """构造用于回放测试的单帧样本"""
    f1 = SnapshotFrame(
        ts=1788260000.0,
        token_id="tok_yes",
        best_bid=0.45,
        best_ask=0.46,
        bids=[(0.45, 50.0), (0.44, 50.0)],
        asks=[(0.46, 20.0), (0.47, 20.0)],
        spread=0.01,
        mid_price=0.455,
        obi=0.43,
        kline={"BTC": {"amplitude": 0.10, "is_choppy": True}}
    )
    f2 = SnapshotFrame(
        ts=1788260000.0,
        token_id="tok_no",
        best_bid=0.54,
        best_ask=0.55,
        bids=[(0.54, 50.0), (0.53, 50.0)],
        asks=[(0.55, 20.0), (0.56, 20.0)],
        spread=0.01,
        mid_price=0.545,
        obi=0.43,
        kline={"BTC": {"amplitude": 0.10, "is_choppy": True}}
    )
    return [f1, f2]


def test_math_sandbox_maker_eval_success(sample_frames):
    """测试在流动性良好盘口下，Maker 双挂机会成功触发并核算出正 EV"""
    f1, f2 = sample_frames
    params = {
        "mm_min_bid": 0.38,
        "max_spread": 0.05,
        "obi_floor": -0.35,
        "initial_margin": 0.015,
        "amount": 10.0,
        "entry_max_price": 0.50,
        "entry_min_price": 0.30
    }
    is_maker, net_ev, reason = MathSandbox.evaluate_maker_opportunity(f1, f2, params)
    assert is_maker is True
    assert net_ev > 0
    assert "成立" in reason


def test_math_sandbox_maker_eval_intercept_on_wide_spread(sample_frames):
    """测试买卖价差过大时被拦截"""
    f1, f2 = sample_frames
    f1.spread = 0.08  # 超过 0.05
    params = {"mm_min_bid": 0.38, "max_spread": 0.05}
    is_maker, net_ev, reason = MathSandbox.evaluate_maker_opportunity(f1, f2, params)
    assert is_maker is False
    assert "价差过大" in reason


def test_grid_calibrator_search_and_ranking(sample_frames):
    """测试网格搜索能正确评估并对参数组评分排序"""
    calibrator = GridCalibrator(sample_frames)
    param_grid = {
        "mm_min_bid": [0.35, 0.40],
        "max_spread": [0.03, 0.05],
        "initial_margin": [0.015],
    }
    results = calibrator.run_grid_search(param_grid, max_evals=10)
    assert len(results) == 4
    # 验证最高分排在前面
    assert results[0].score >= results[-1].score
