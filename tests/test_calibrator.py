import os
import tempfile
import pytest

from scripts.calibrate_params import SnapshotFrame, MathSandbox, MultiMarketSimulator, OptunaOptimizer, SnapshotLoader


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




def test_multi_market_simulator_hedged_locked(sample_frames):
    """测试多市场并发模拟器能够正确模拟双买锁仓 (HEDGED_LOCKED)"""
    params = {
        "mm_min_bid": 0.38,
        "max_spread": 0.05,
        "obi_floor": -0.35,
        "initial_margin": 0.018,
        "amount": 10.0,
        "entry_max_price": 0.50,
        "entry_min_price": 0.28
    }
    res = MultiMarketSimulator.simulate(sample_frames, params, cooldown_sec=120.0)
    assert res.total_trades == 1
    assert res.hedged_locked_count == 1
    assert res.win_rate == 100.0
    assert res.total_net_ev > 0


def test_optuna_optimizer_trials(sample_frames):
    """测试 Optuna TPE 贝叶斯寻优器能正常运行多轮 Trial 并返回排序结果"""
    opt = OptunaOptimizer(sample_frames)
    results = opt.optimize(n_trials=5)
    assert len(results) == 5
    assert results[0].score >= results[-1].score
