from __future__ import annotations

import csv
import os
from dataclasses import asdict
from typing import Dict, List, Optional, Tuple

from polymarket.config import (
    GAMMA_HOST,
    CLOB_HOST,
    HTTP_PROXY,
    HTTPS_PROXY,
    INITIAL_ENTRY_MAX_PRICE,
    REENTRY_TRIGGER_PRICE,
    STOP_LOSS_TIME_REMAINING,
)

from .engine import BacktestEngineV2
from .feed_public_approx import PublicApproxFeed
from .feed_public_timeseries import PublicTimeSeriesFeed
from .feed_trades_1s import Trades1sFeed
from .metrics import Summary, summarize
from .types import BacktestConfig, Fill, Quote, Trade


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def export_fills_csv(fills: List[Fill], out_path: str) -> None:
    if not fills:
        return
    _ensure_dir(os.path.dirname(out_path) or ".")
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "ts",
                "market_id",
                "outcome",
                "side",
                "price",
                "size",
                "fee",
                "order_type",
            ],
        )
        w.writeheader()
        for x in fills:
            w.writerow(asdict(x))


def export_trades_csv(trades: List[Trade], out_path: str) -> None:
    if not trades:
        return
    _ensure_dir(os.path.dirname(out_path) or ".")
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "market_id",
                "entry_ts",
                "exit_ts",
                "leg1_outcome",
                "leg2_outcome",
                "leg1_cost",
                "leg2_cost",
                "size",
                "pnl",
                "status",
                "reason",
            ],
        )
        w.writeheader()
        for t in trades:
            w.writerow(asdict(t))


def export_equity_csv(equity_curve: List[Tuple[float, float]], out_path: str) -> None:
    if not equity_curve:
        return
    _ensure_dir(os.path.dirname(out_path) or ".")
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ts", "equity"])
        for ts, eq in equity_curve:
            w.writerow([ts, eq])


def run_backtest(
    *,
    days: int = 3,
    seed: int = 42,
    quote_step_sec: int = 1,
    spread_bps: float = 20.0,
    taker_fee_bps: float = 0.0,
    maker_fee_bps: float = 0.0,
    monte_carlo_paths: int = 200,
    allow_leg2_gtc: bool = True,
    out_dir: str = "data/backtest_out",
    max_markets: int | None = None,
) -> Dict[str, object]:
    """
    公开接口近似回测入口。

    输出：
    - backtest_out/trades.csv
    - backtest_out/fills.csv
    - backtest_out/equity.csv
    """
    cfg = BacktestConfig(
        days=days,
        seed=seed,
        initial_capital=50.0,
        order_amount_usdc=1.0,
        entry_max_price=INITIAL_ENTRY_MAX_PRICE,
        reentry_trigger_price=REENTRY_TRIGGER_PRICE,
        stop_loss_time_remaining_sec=STOP_LOSS_TIME_REMAINING,
        entry_window_sec=30,
        max_open_markets=10,
        quote_step_sec=quote_step_sec,
        spread_bps=spread_bps,
        taker_fee_bps=taker_fee_bps,
        maker_fee_bps=maker_fee_bps,
        monte_carlo_paths=monte_carlo_paths,
        allow_leg2_gtc=allow_leg2_gtc,
    )

    # Prefer trades->1s feed (best public proxy for volatility), then prices-history, then approx.
    trades_feed = Trades1sFeed(
        gamma_host=GAMMA_HOST,
        http_proxy=HTTP_PROXY,
        https_proxy=HTTPS_PROXY,
        cache_dir="tmp/backtest_cache/trades",
    )
    markets = trades_feed.iter_btc_5m_markets(days=cfg.days)
    if max_markets is not None and max_markets > 0:
        markets = markets[-max_markets:]

    engine = BacktestEngineV2(cfg)

    # build market registry
    market_end_ts: Dict[str, float] = {}
    for pm in markets:
        engine.on_market_start(pm.market_id, pm.start_ts, pm.end_ts)
        market_end_ts[pm.market_id] = pm.end_ts

    used_markets = set()

    # replay quotes (trades -> 1s)
    try:
        for q in trades_feed.iter_quotes(markets, spread_bps=cfg.spread_bps):
            used_markets.add(q.market_id)
            engine.on_quote(q)
            end_ts = market_end_ts.get(q.market_id)
            if end_ts is not None and q.ts >= end_ts:
                engine.on_market_end(q.market_id)
    except Exception:
        used_markets = set()

    missing_ids = {m.market_id for m in markets if m.market_id not in used_markets}

    # fallback: prices-history (minute) if trades missing
    if missing_ids:
        ts_feed = PublicTimeSeriesFeed(
            gamma_host=GAMMA_HOST,
            clob_host=CLOB_HOST,
            http_proxy=HTTP_PROXY,
            https_proxy=HTTPS_PROXY,
        )
        try:
            for q in ts_feed.iter_quotes([m for m in markets if m.market_id in missing_ids], spread_bps=cfg.spread_bps):
                used_markets.add(q.market_id)
                engine.on_quote(q)
                end_ts = market_end_ts.get(q.market_id)
                if end_ts is not None and q.ts >= end_ts:
                    engine.on_market_end(q.market_id)
        except Exception:
            pass

    missing_ids = {m.market_id for m in markets if m.market_id not in used_markets}

    # fallback: approx
    if missing_ids:
        approx = PublicApproxFeed(
            gamma_host=GAMMA_HOST,
            http_proxy=HTTP_PROXY,
            https_proxy=HTTPS_PROXY,
        )
        approx_markets_all = approx.iter_btc_5m_markets(days=cfg.days)
        approx_missing = [m for m in approx_markets_all if m.market_id in missing_ids]
        for q in approx.iter_quotes(
            approx_missing,
            seed=cfg.seed,
            quote_step_sec=cfg.quote_step_sec,
            spread_bps=cfg.spread_bps,
        ):
            engine.on_quote(q)
            end_ts = market_end_ts.get(q.market_id)
            if end_ts is not None and q.ts >= end_ts:
                engine.on_market_end(q.market_id)

    summary = summarize(engine.trades, engine.equity_curve)
    final_capital = engine.cash

    out_dir_abs = os.path.abspath(out_dir)
    export_trades_csv(engine.trades, os.path.join(out_dir_abs, "trades.csv"))
    export_fills_csv(engine.fills, os.path.join(out_dir_abs, "fills.csv"))
    export_equity_csv(engine.equity_curve, os.path.join(out_dir_abs, "equity.csv"))

    return {
        "config": cfg,
        "summary": summary,
        "out_dir": out_dir_abs,
        "market_count": len(markets),
        "initial_capital": cfg.initial_capital,
        "final_capital": final_capital,
    }

