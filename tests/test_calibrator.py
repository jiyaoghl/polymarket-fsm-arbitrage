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
        best_bid=0.46,
        best_ask=0.47,
        bids=[(0.46, 50.0), (0.45, 50.0)],
        asks=[(0.47, 20.0), (0.48, 20.0)],
        spread=0.01,
        mid_price=0.465,
        obi=0.43,
        kline={"BTC": {"amplitude": 0.10, "is_choppy": True}}
    )
    f1_fut = SnapshotFrame(
        ts=1788260002.0,
        token_id="tok_yes",
        best_bid=0.44,
        best_ask=0.44,
        bids=[(0.44, 50.0)],
        asks=[(0.44, 20.0)],
        spread=0.00,
        mid_price=0.44,
        obi=0.43,
        kline={"BTC": {"amplitude": 0.10, "is_choppy": True}}
    )
    f2_fut = SnapshotFrame(
        ts=1788260002.0,
        token_id="tok_no",
        best_bid=0.44,
        best_ask=0.44,  # 卖一跌到 0.44 <= 0.451，成功击穿对冲！
        bids=[(0.44, 50.0)],
        asks=[(0.44, 20.0)],
        spread=0.00,
        mid_price=0.44,
        obi=0.43,
        kline={"BTC": {"amplitude": 0.10, "is_choppy": True}}
    )
    return [f1, f2, f1_fut, f2_fut]



def test_multi_market_simulator_hedged_locked(sample_frames):
    """测试多市场并发模拟器能够正确模拟时序双买锁仓 (HEDGED_LOCKED)"""
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


def test_multi_market_simulator_detects_real_liquidation(sample_frames):
    """测试当未来窗口未出现击穿且无法做 T 时，模拟器真实记录强平与扣费亏损"""
    # 仅传入第 1 帧，后续无卖盘跌破买价
    only_frame_1 = sample_frames[:2]
    params = {
        "mm_min_bid": 0.38,
        "max_spread": 0.05,
        "obi_floor": -0.35,
        "initial_margin": 0.018,
        "amount": 10.0,
        "entry_max_price": 0.50,
        "entry_min_price": 0.28
    }
    res = MultiMarketSimulator.simulate(only_frame_1, params, cooldown_sec=120.0)
    assert res.total_trades == 1
    assert res.liquidated_count == 1
    assert res.hedged_locked_count == 0
    assert res.total_net_ev < 0  # 真实计入强平与手续费亏损



def test_optuna_optimizer_trials(sample_frames):
    """测试 Optuna TPE 贝叶斯寻优器能正常运行多轮 Trial 并返回排序结果"""
    opt = OptunaOptimizer(sample_frames)
    results = opt.optimize(n_trials=5)
    assert len(results) == 5
    assert results[0].score >= results[-1].score


def test_multi_asset_gk_volatility_filtering():
    """测试多资产动态匹配与 GK+EWMA 极值波动率精确拦截与放行"""
    f1 = SnapshotFrame(
        ts=1788260000.0, token_id="tok_eth_y", asset="ETH",
        best_bid=0.45, best_ask=0.46,
        bids=[(0.45, 50.0)], asks=[(0.46, 20.0)],
        spread=0.01, mid_price=0.455, obi=0.43,
        kline={
            "BTC": {"gk_volatility": 0.01, "net_change": 0.01},
            "ETH": {"gk_volatility": 0.04, "net_change": 0.02},  # ETH 真实 GK 波动率 0.04%
        }
    )
    f2 = SnapshotFrame(
        ts=1788260000.0, token_id="tok_eth_n", asset="ETH",
        best_bid=0.46, best_ask=0.47,
        bids=[(0.46, 50.0)], asks=[(0.47, 20.0)],
        spread=0.01, mid_price=0.465, obi=0.43,
        kline={
            "BTC": {"gk_volatility": 0.01, "net_change": 0.01},
            "ETH": {"gk_volatility": 0.04, "net_change": 0.02},
        }
    )
    # 未来击穿帧
    f1_fut = SnapshotFrame(
        ts=1788260002.0, token_id="tok_eth_y", asset="ETH",
        best_bid=0.44, best_ask=0.44, bids=[(0.44, 50.0)], asks=[(0.44, 20.0)],
        spread=0.0, mid_price=0.44, obi=0.43,
        kline={"ETH": {"gk_volatility": 0.04, "net_change": 0.02}}
    )
    f2_fut = SnapshotFrame(
        ts=1788260002.0, token_id="tok_eth_n", asset="ETH",
        best_bid=0.44, best_ask=0.44, bids=[(0.44, 50.0)], asks=[(0.44, 20.0)],
        spread=0.0, mid_price=0.44, obi=0.43,
        kline={"ETH": {"gk_volatility": 0.04, "net_change": 0.02}}
    )
    frames = [f1, f2, f1_fut, f2_fut]

    # 1. 阈值 0.03% (低于 ETH 的 0.04%): 必须精准拦截 ETH 盘口 (即使 BTC=0.01%)
    params_strict = {
        "mm_min_bid": 0.38, "max_spread": 0.05, "obi_floor": -0.35, "initial_margin": 0.018,
        "amount": 10.0, "entry_max_price": 0.50, "entry_min_price": 0.28,
        "max_amplitude": 0.03, "max_net_change": 0.05
    }
    res_strict = MultiMarketSimulator.simulate(frames, params_strict)
    assert res_strict.total_trades == 0, "ETH GK波动率=0.04% > 阈值0.03%，必须被多资产动态拦截"

    # 2. 阈值 0.05% (高于 ETH 的 0.04%): 必须安全放行并完成锁仓
    params_relaxed = dict(params_strict, max_amplitude=0.05)
    res_relaxed = MultiMarketSimulator.simulate(frames, params_relaxed)
    assert res_relaxed.total_trades == 1
    assert res_relaxed.hedged_locked_count == 1


def test_dual_mode_legacy_snapshot_normalization():
    """测试历史旧快照 (仅含收盘价 3σ amplitude) 自动按 0.35x 理论系数折算为 GK 尺度"""
    # 历史旧快照：无 gk_volatility，仅含旧 amplitude=0.20%
    f1 = SnapshotFrame(
        ts=1788260000.0, token_id="tok_y",
        best_bid=0.45, best_ask=0.46,
        bids=[(0.45, 50.0)], asks=[(0.46, 20.0)],
        spread=0.01, mid_price=0.455, obi=0.43,
        kline={"BTC": {"amplitude": 0.20, "net_change": 0.08}}
    )
    f2 = SnapshotFrame(
        ts=1788260000.0, token_id="tok_n",
        best_bid=0.46, best_ask=0.47,
        bids=[(0.46, 50.0)], asks=[(0.47, 20.0)],
        spread=0.01, mid_price=0.465, obi=0.43,
        kline={"BTC": {"amplitude": 0.20, "net_change": 0.08}}
    )
    # 折算后等效 GK 振幅: 0.20 * 0.35 = 0.0700%
    # 当 max_amplitude = 0.06 时应当拦截
    params_strict = {
        "mm_min_bid": 0.38, "max_spread": 0.05, "obi_floor": -0.35, "initial_margin": 0.018,
        "amount": 10.0, "entry_max_price": 0.50, "entry_min_price": 0.28,
        "max_amplitude": 0.06, "max_net_change": 0.05
    }
    res_strict = MultiMarketSimulator.simulate([f1, f2], params_strict)
    assert res_strict.total_trades == 0

    # 当 max_amplitude = 0.08 时放行 (模拟强平测试)
    params_relaxed = dict(params_strict, max_amplitude=0.08, max_net_change=0.05)
    res_relaxed = MultiMarketSimulator.simulate([f1, f2], params_relaxed)
    assert res_relaxed.total_trades == 1

