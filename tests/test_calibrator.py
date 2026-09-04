import os
import tempfile
import pytest

from src.polymarket.services.pricing import PricingEngine
from src.polymarket.services.backtest import (
    SnapshotFrame,
    EvalResult,
    MultiMarketSimulator,
    OptunaOptimizer,
    SnapshotLoader,
)
# 验证向后兼容脚本别名导出
from scripts.calibrate_params import MathSandbox


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
    assert res.total_net_ev < 0


def test_liquidation_parabolic_fee_exact_deduction():
    """测试强平出场时 2026 抛物线手续费被精确扣除，验证参数未倒置"""
    # 理论推导: price=0.40, size=25.0, fee_rate=0.07
    # raw_fee = 25 * 0.07 * 0.40 * 0.60 = 0.4200
    expected_fee = PricingEngine.calculate_parabolic_fee(0.40, 25.0, fee_rate=0.07)
    assert expected_fee == 0.4200

    # 验证旧 Bug 倒置时的极端误差 (传入 25.0 作为价格将被 clamp 为 0.999 导致费率归零)
    wrong_fee = PricingEngine.calculate_parabolic_fee(25.0, 0.40, fee_rate=0.07)
    assert wrong_fee < 0.001, "旧传参方式曾导致手续费被归零"

    # 构造精确强平帧
    f1 = SnapshotFrame(
        ts=1788260000.0, token_id="t1",
        best_bid=0.40, best_ask=0.41,
        bids=[(0.40, 100.0)], asks=[(0.41, 100.0)],
        spread=0.01, mid_price=0.405, obi=0.0,
        kline={"BTC": {"amplitude": 0.05, "is_choppy": True}}
    )
    f2 = SnapshotFrame(
        ts=1788260000.0, token_id="t2",
        best_bid=0.40, best_ask=0.41,
        bids=[(0.40, 100.0)], asks=[(0.41, 100.0)],
        spread=0.01, mid_price=0.405, obi=0.0,
        kline={"BTC": {"amplitude": 0.05, "is_choppy": True}}
    )

    params = {
        "mm_min_bid": 0.35, "max_spread": 0.05, "obi_floor": -0.50, "initial_margin": 0.018,
        "amount": 10.0, "entry_max_price": 0.50, "entry_min_price": 0.28
    }
    # 仅一帧无后续打穿，必然走向超时市价强平
    res = MultiMarketSimulator.simulate([f1, f2], params)
    assert res.liquidated_count == 1
    # 校验累计亏损真实包含了抛物线手续费
    # shares = round(10.0 / 0.401, 2) ≈ 24.94
    # exit_price = 0.40 (从 bids 深度加权取得)
    # gross_loss = 24.94 * (0.40 - 0.401) = -0.0249
    # liq_fee = 24.94 * 0.07 * 0.40 * 0.60 ≈ 0.4190
    # net_loss ≈ -0.4439
    assert res.total_net_ev < -0.40, f"真实强平亏损必须严惩扣除 ~0.42U 抛物线手续费，实际={res.total_net_ev}"


def test_liquidation_vwap_depth_slippage():
    """测试当买盘深度不足时，强平能够穿透买盘并施加深度枯竭滑点惩罚"""
    # 构造买盘深度极薄的盘口: 仅有一档 2.0 份，而强平需要卖出约 25 份
    f1 = SnapshotFrame(
        ts=1788260000.0, token_id="t1",
        best_bid=0.40, best_ask=0.41,
        bids=[(0.40, 2.0)],  # 深度极薄！
        asks=[(0.41, 100.0)],
        spread=0.01, mid_price=0.405, obi=0.0,
        kline={"BTC": {"amplitude": 0.05, "is_choppy": True}}
    )
    f2 = SnapshotFrame(
        ts=1788260000.0, token_id="t2",
        best_bid=0.40, best_ask=0.41,
        bids=[(0.40, 100.0)], asks=[(0.41, 100.0)],
        spread=0.01, mid_price=0.405, obi=0.0,
        kline={"BTC": {"amplitude": 0.05, "is_choppy": True}}
    )

    params = {
        "mm_min_bid": 0.35, "max_spread": 0.05, "obi_floor": -0.50, "initial_margin": 0.018,
        "amount": 10.0, "entry_max_price": 0.50, "entry_min_price": 0.28
    }
    res = MultiMarketSimulator.simulate([f1, f2], params)
    assert res.liquidated_count == 1
    # 深度枯竭惩罚会使有效割肉价跌至约 0.38，导致总亏损显著加大
    assert res.total_net_ev < -0.80, f"流动性荒漠强平必须产生深层滑点亏损，实际={res.total_net_ev}"


def test_strict_passive_fill_mode():
    """测试 strict_fill 严格被动成交模式：必须有卖盘打穿才算首腿成交"""
    # 帧1：卖一 0.46 高于买入限价 0.451
    f1 = SnapshotFrame(
        ts=1788260000.0, token_id="t1",
        best_bid=0.45, best_ask=0.46,
        bids=[(0.45, 50.0)], asks=[(0.46, 20.0)],
        spread=0.01, mid_price=0.455, obi=0.43,
        kline={"BTC": {"amplitude": 0.05, "is_choppy": True}}
    )
    f2 = SnapshotFrame(
        ts=1788260000.0, token_id="t2",
        best_bid=0.45, best_ask=0.46,
        bids=[(0.45, 50.0)], asks=[(0.46, 20.0)],
        spread=0.01, mid_price=0.455, obi=0.43,
        kline={"BTC": {"amplitude": 0.05, "is_choppy": True}}
    )
    # 帧2：卖一仍未打穿
    f1_fut = SnapshotFrame(
        ts=1788260001.0, token_id="t1",
        best_bid=0.45, best_ask=0.46,
        bids=[(0.45, 50.0)], asks=[(0.46, 20.0)],
        spread=0.01, mid_price=0.455, obi=0.43,
        kline={"BTC": {"amplitude": 0.05, "is_choppy": True}}
    )
    f2_fut = SnapshotFrame(
        ts=1788260001.0, token_id="t2",
        best_bid=0.45, best_ask=0.46,
        bids=[(0.45, 50.0)], asks=[(0.46, 20.0)],
        spread=0.01, mid_price=0.455, obi=0.43,
        kline={"BTC": {"amplitude": 0.05, "is_choppy": True}}
    )

    params_base = {
        "mm_min_bid": 0.38, "max_spread": 0.05, "obi_floor": -0.35, "initial_margin": 0.018,
        "amount": 10.0, "entry_max_price": 0.50, "entry_min_price": 0.28,
        "strict_fill": False
    }
    # 基准模式：假定挂单即能成交
    res_base = MultiMarketSimulator.simulate([f1, f2, f1_fut, f2_fut], params_base)
    assert res_base.total_trades == 1

    # 严格模式：卖一未曾打穿 <= yes_p，严禁虚空成交
    params_strict = dict(params_base, strict_fill=True)
    res_strict = MultiMarketSimulator.simulate([f1, f2, f1_fut, f2_fut], params_strict)
    assert res_strict.total_trades == 0, "卖一未跌破买单挂价，严格模式下不得虚构成交"

    # 若未来帧卖一跌破买价 (0.45 <= 0.451)，严格模式应成功确认成交
    f1_fut_fill = SnapshotFrame(
        ts=1788260002.0, token_id="t1",
        best_bid=0.45, best_ask=0.45,  # 卖一砸到 0.45，成交确认！
        bids=[(0.45, 50.0)], asks=[(0.45, 20.0)],
        spread=0.00, mid_price=0.45, obi=0.43,
        kline={"BTC": {"amplitude": 0.05, "is_choppy": True}}
    )
    f2_fut_fill = SnapshotFrame(
        ts=1788260002.0, token_id="t2",
        best_bid=0.45, best_ask=0.45,
        bids=[(0.45, 50.0)], asks=[(0.45, 20.0)],
        spread=0.00, mid_price=0.45, obi=0.43,
        kline={"BTC": {"amplitude": 0.05, "is_choppy": True}}
    )
    res_strict_filled = MultiMarketSimulator.simulate([f1, f2, f1_fut_fill, f2_fut_fill], params_strict)
    assert res_strict_filled.total_trades == 1


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
            "ETH": {"gk_volatility": 0.04, "net_change": 0.02},
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

    params_strict = {
        "mm_min_bid": 0.38, "max_spread": 0.05, "obi_floor": -0.35, "initial_margin": 0.018,
        "amount": 10.0, "entry_max_price": 0.50, "entry_min_price": 0.28,
        "max_amplitude": 0.03, "max_net_change": 0.05
    }
    res_strict = MultiMarketSimulator.simulate(frames, params_strict)
    assert res_strict.total_trades == 0, "ETH GK波动率=0.04% > 阈值0.03%，必须被多资产动态拦截"

    params_relaxed = dict(params_strict, max_amplitude=0.05)
    res_relaxed = MultiMarketSimulator.simulate(frames, params_relaxed)
    assert res_relaxed.total_trades == 1
    assert res_relaxed.hedged_locked_count == 1


def test_dual_mode_legacy_snapshot_normalization():
    """测试历史旧快照 (仅含收盘价 3σ amplitude) 自动按 0.35x 理论系数折算为 GK 尺度"""
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

    params_strict = {
        "mm_min_bid": 0.38, "max_spread": 0.05, "obi_floor": -0.35, "initial_margin": 0.018,
        "amount": 10.0, "entry_max_price": 0.50, "entry_min_price": 0.28,
        "max_amplitude": 0.06, "max_net_change": 0.05
    }
    res_strict = MultiMarketSimulator.simulate([f1, f2], params_strict)
    assert res_strict.total_trades == 0

    params_relaxed = dict(params_strict, max_amplitude=0.08, max_net_change=0.05)
    res_relaxed = MultiMarketSimulator.simulate([f1, f2], params_relaxed)
    assert res_relaxed.total_trades == 1
