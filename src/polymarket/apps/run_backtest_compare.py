from __future__ import annotations

import argparse
import csv
import json
import os
from datetime import datetime
from dataclasses import asdict
from typing import Any, Dict, List

from polymarket.config import (
    GAMMA_HOST,
    HTTP_PROXY,
    HTTPS_PROXY,
    STOP_LOSS_TIME_REMAINING,
)

from polymarket.backtest.engine import BacktestEngineV2
from polymarket.backtest.feed_trades_1s import Trades1sFeed
from polymarket.backtest.metrics import summarize
from polymarket.backtest.replay import build_replay_bundle
from polymarket.backtest.types import BacktestConfig


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _load_strategies(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        arr = json.load(f)
    return [x for x in arr if isinstance(x, dict)]


def main() -> int:
    p = argparse.ArgumentParser(description="Multi-strategy backtest comparison (shared replay)")
    p.add_argument("--strategies", type=str, default="configs/strategies.json", help="strategies json path")
    p.add_argument("--days", type=int, default=1, help="lookback days")
    p.add_argument("--max-markets", type=int, default=60, help="most recent N markets (0=all)")
    p.add_argument("--seed", type=int, default=42, help="RNG seed (for Monte Carlo settlement)")
    p.add_argument("--spread-bps", type=float, default=20.0, help="synthetic bid/ask spread")
    p.add_argument("--taker-fee-bps", type=float, default=0.0)
    p.add_argument("--maker-fee-bps", type=float, default=0.0)
    p.add_argument("--mc-paths", type=int, default=200)
    p.add_argument("--out", type=str, default="data/backtest_out/compare_summary.csv")
    args = p.parse_args()

    strategies = _load_strategies(args.strategies)

    feed = Trades1sFeed(
        gamma_host=GAMMA_HOST,
        http_proxy=HTTP_PROXY,
        https_proxy=HTTPS_PROXY,
        cache_dir="tmp/backtest_cache/trades",
    )

    bundle = build_replay_bundle(
        feed=feed,
        days=args.days,
        max_markets=(args.max_markets if args.max_markets and args.max_markets > 0 else None),
        spread_bps=args.spread_bps,
    )

    rows: List[Dict[str, Any]] = []
    for s in strategies:
        strategy_id = s.get("strategy_id", "unknown")
        name = s.get("name", strategy_id)

        cfg = BacktestConfig(
            days=args.days,
            seed=args.seed,
            initial_capital=50.0,
            order_amount_usdc=float(s.get("amount", 1.0)),
            entry_max_price=float(s.get("entry_max_price", 0.5)),
            reentry_trigger_price=float(s.get("reentry_trigger", 0.4)),
            stop_loss_time_remaining_sec=STOP_LOSS_TIME_REMAINING,
            entry_window_sec=30,
            max_open_markets=10,
            quote_step_sec=1,
            spread_bps=args.spread_bps,
            taker_fee_bps=args.taker_fee_bps,
            maker_fee_bps=args.maker_fee_bps,
            monte_carlo_paths=args.mc_paths,
            allow_leg2_gtc=(str(s.get("leg2_order_type", "FOK")).upper() == "GTC"),
        )

        eng = BacktestEngineV2(cfg)
        for pm in bundle.markets:
            eng.on_market_start(pm.market_id, float(pm.start_ts), float(pm.end_ts))

        # replay
        for pm in bundle.markets:
            quotes = bundle.quotes_by_market.get(pm.market_id) or []
            for q in quotes:
                eng.on_quote(q)
                if q.ts >= float(pm.end_ts):
                    eng.on_market_end(pm.market_id)

        summary = summarize(eng.trades, eng.equity_curve)
        final_capital = eng.cash
        locked = sum(1 for t in eng.trades if t.status == "locked")
        stopped = sum(1 for t in eng.trades if t.status == "stopped")

        rows.append(
            {
                "strategy_id": strategy_id,
                "name": name,
                "markets": len(bundle.markets),
                "initial_capital": cfg.initial_capital,
                "final_capital": final_capital,
                "return_pct": ((final_capital - cfg.initial_capital) / cfg.initial_capital) if cfg.initial_capital else 0.0,
                "trades": summary.total_trades,
                "locked": locked,
                "stopped": stopped,
                "locked_rate": (locked / summary.total_trades) if summary.total_trades else 0.0,
                "total_pnl": summary.total_pnl,
                "max_drawdown": summary.max_drawdown,
                "sharpe": summary.sharpe_ratio,
                "profit_factor": summary.profit_factor,
                "win_rate": summary.win_rate,
                "avg_holding_sec": summary.avg_holding_time_sec,
                "entry_max_price": cfg.entry_max_price,
                "reentry_trigger": cfg.reentry_trigger_price,
                "amount_usdc": cfg.order_amount_usdc,
                "max_open_markets": cfg.max_open_markets,
                "leg2_order_type": "GTC" if cfg.allow_leg2_gtc else "FOK",
            }
        )

    rows.sort(key=lambda r: (r["total_pnl"], -r["max_drawdown"]), reverse=True)

    out_path = os.path.abspath(args.out)
    _ensure_dir(os.path.dirname(out_path) or ".")
    actual_out_path = out_path
    try:
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else [])
            if rows:
                w.writeheader()
                for r in rows:
                    w.writerow(r)
    except PermissionError:
        # If the target file is open/locked (e.g. in editor), write to a timestamped file.
        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        base, ext = os.path.splitext(out_path)
        actual_out_path = f"{base}.{ts}{ext or '.csv'}"
        with open(actual_out_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else [])
            if rows:
                w.writeheader()
                for r in rows:
                    w.writerow(r)

    # console table (top 8)
    print("")
    print("=" * 100)
    print(f"多策略回测对比（共享行情回放） days={args.days} max_markets={args.max_markets or 'all'} markets={len(bundle.markets)}")
    print("=" * 100)
    print(f"{'strategy_id':<28} {'tr':>4} {'lock%':>6} {'pnl':>10} {'dd':>8} {'sharpe':>7} {'PF':>6}")
    for r in rows[:8]:
        print(
            f"{r['strategy_id']:<28} {r['trades']:>4} {r['locked_rate']*100:>5.1f}% {r['total_pnl']:>10.2f} {r['max_drawdown']*100:>7.2f}% {r['sharpe']:>7.2f} {r['profit_factor']:>6.2f}"
        )
    print("-" * 100)
    print(f"已写入：{actual_out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

