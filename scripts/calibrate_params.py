#!/usr/bin/env python3
"""
Polymarket 离线高保真参数标定与贝叶斯寻优 CLI 入口 (V3.2 完美复刻版)。

核心特性：
1. 纯离线读取真实 1s 盘口深度与多资产波动率快照；
2. 【分市场独立并发锁】：BTC / ETH / SOL 多盘口独立维持 120s 生命周期，真实还原多资产并发捕获；
3. 【全出场路径微观模拟】：完整重放 HEDGED_LOCKED (双买锁仓)、SMART_FLIP (阶梯做T变现) 与 FORCE_CLOSED (穿透强平)；
4. 【Optuna 贝叶斯寻优】：集成 TPE 采样器在连续浮点空间精细标定全局最优参数矩阵；
5. 【微观物理深度穿透】：强平接入买盘前 5 档 VWAP 深度与 2026 抛物线手续费真实惩罚；
6. 输出全生命周期收益分解与量化标定报告。
"""

import argparse
import os
import sys
from pathlib import Path
from typing import Dict, List, Any

# 确保项目根路径在 sys.path 中
repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from src.polymarket.services.backtest import (
    SnapshotFrame,
    EvalResult,
    SnapshotLoader,
    MultiMarketSimulator,
    OptunaOptimizer,
    ReportGenerator,
)

# 向后兼容别名导出 (供旧版测试用例与外部模块引用)
MathSandbox = MultiMarketSimulator


def main():
    parser = argparse.ArgumentParser(description="Polymarket 离线高保真参数标定引擎 (完美复刻版)")
    parser.add_argument("--snapshots-dir", type=str, default="vps-logs/snapshots", help="L2 快照目录路径")
    parser.add_argument("--max-frames", type=int, default=500000, help="最多读取的快照帧数 (默认 500000)")
    parser.add_argument("--mode", type=str, default="optuna", choices=["optuna", "grid"], help="寻优模式")
    parser.add_argument("--trials", type=int, default=300, help="Optuna 试验次数 (默认 300)")
    parser.add_argument("--output", type=str, default="doc/CALIBRATION_REPORT.md", help="输出报告路径")
    parser.add_argument("--strict-fill", action="store_true", help="开启严格被动成交模式 (要求卖方打穿才算首腿成交)")
    args = parser.parse_args()

    # 解析为基于项目根目录的绝对路径
    out_path = Path(args.output)
    if not out_path.is_absolute():
        out_path = repo_root / out_path

    print("===========================================================================")
    print("  [*] Polymarket 离线高保真参数标定与贝叶斯寻优 (V3.2 完美复刻版)")
    print(f"  [*] 严格被动成交模式: {'已启用 (Strict Fill)' if args.strict_fill else '标准基准模式'}")
    print("===========================================================================")

    frames = SnapshotLoader.load_all_frames(args.snapshots_dir, max_frames=args.max_frames)
    print(f"[+] 成功加载 {len(frames)} 帧真实快照数据")

    if not frames:
        print("[-] 快照数据为空，请先运行 python scripts/vps_ops.py sync-snapshots 同步数据。")
        return

    results: List[EvalResult] = []
    if args.mode == "optuna":
        optimizer = OptunaOptimizer(frames, strict_fill=args.strict_fill)
        results = optimizer.optimize(n_trials=args.trials)

    if not results:
        # 网格备用
        param_grid = {
            "entry_max_price": [0.40, 0.42, 0.45],
            "entry_min_price": [0.28, 0.30],
            "mm_min_bid": [0.35, 0.38, 0.40],
            "max_spread": [0.04, 0.05, 0.06],
            "obi_floor": [-0.40, -0.35, -0.25],
            "max_amplitude": [0.03, 0.06, 0.12],
            "max_net_change": [0.02, 0.04, 0.08],
            "initial_margin": [0.015, 0.018],
            "amount": [10.0],
            "strict_fill": args.strict_fill
        }
        import itertools
        keys = list(param_grid.keys())
        combos = list(itertools.product(*[param_grid[k] for k in keys]))[:200]
        print(f"[*] 执行备用网格搜索 ({len(combos)} 组)...")
        for c in combos:
            p = dict(zip(keys, c))
            results.append(MultiMarketSimulator.simulate(frames, p))
        results.sort(key=lambda r: r.score, reverse=True)

    ReportGenerator.generate(frames, results, str(out_path))


if __name__ == "__main__":
    main()
