from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass
from typing import Dict, Iterable, List, Tuple

from .types import Trade


@dataclass
class Summary:
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    total_pnl: float
    avg_win: float
    avg_loss: float
    max_drawdown: float
    sharpe_ratio: float
    profit_factor: float
    avg_holding_time_sec: float

    def to_dict(self) -> Dict:
        return asdict(self)


def compute_max_drawdown(equity_curve: List[Tuple[float, float]]) -> float:
    peak = -float("inf")
    max_dd = 0.0
    for _, eq in equity_curve:
        if eq > peak:
            peak = eq
        if peak > 0:
            dd = (peak - eq) / peak
            if dd > max_dd:
                max_dd = dd
    return max_dd


def summarize(trades: List[Trade], equity_curve: List[Tuple[float, float]]) -> Summary:
    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]

    total_trades = len(trades)
    winning_trades = len(wins)
    losing_trades = len(losses)
    win_rate = (winning_trades / total_trades) if total_trades else 0.0
    total_pnl = sum(pnls) if pnls else 0.0
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0
    max_dd = compute_max_drawdown(equity_curve) if equity_curve else 0.0

    sharpe = 0.0
    if len(pnls) >= 2:
        std = statistics.stdev(pnls)
        if std > 1e-12:
            sharpe = statistics.mean(pnls) / std

    profit_factor = 999.99
    if losses and abs(sum(losses)) > 1e-12:
        profit_factor = abs(sum(wins) / sum(losses)) if wins else 0.0

    holding = []
    for t in trades:
        holding.append(max(0.0, t.exit_ts - t.entry_ts))
    avg_holding = (sum(holding) / len(holding)) if holding else 0.0

    return Summary(
        total_trades=total_trades,
        winning_trades=winning_trades,
        losing_trades=losing_trades,
        win_rate=win_rate,
        total_pnl=total_pnl,
        avg_win=avg_win,
        avg_loss=avg_loss,
        max_drawdown=max_dd,
        sharpe_ratio=sharpe,
        profit_factor=profit_factor,
        avg_holding_time_sec=avg_holding,
    )

