from __future__ import annotations

import argparse

from polymarket.backtest.runner import run_backtest


def main() -> int:
    p = argparse.ArgumentParser(description="Polymarket 5m bot backtest (public approx + Monte Carlo)")
    p.add_argument("--days", type=int, default=3, help="lookback days (approx via slug enumeration)")
    p.add_argument("--seed", type=int, default=42, help="RNG seed")
    p.add_argument("--quote-step-sec", type=int, default=1, help="quote step seconds within a 5m market")
    p.add_argument("--spread-bps", type=float, default=20.0, help="synthetic bid/ask spread in bps")
    p.add_argument("--taker-fee-bps", type=float, default=0.0, help="taker fee in bps")
    p.add_argument("--maker-fee-bps", type=float, default=0.0, help="maker fee in bps")
    p.add_argument("--mc-paths", type=int, default=200, help="Monte Carlo paths per settlement")
    p.add_argument("--no-leg2-gtc", action="store_true", help="force leg2 to FOK (taker)")
    p.add_argument("--out-dir", type=str, default="data/backtest_out", help="output directory")
    p.add_argument("--max-markets", type=int, default=0, help="only backtest the most recent N markets (0=all)")
    args = p.parse_args()

    res = run_backtest(
        days=args.days,
        seed=args.seed,
        quote_step_sec=args.quote_step_sec,
        spread_bps=args.spread_bps,
        taker_fee_bps=args.taker_fee_bps,
        maker_fee_bps=args.maker_fee_bps,
        monte_carlo_paths=args.mc_paths,
        allow_leg2_gtc=(not args.no_leg2_gtc),
        out_dir=args.out_dir,
        max_markets=(args.max_markets if args.max_markets and args.max_markets > 0 else None),
    )

    summary = res["summary"]
    print("")
    print("=" * 60)
    print("回测结果（公开接口近似 + 蒙特卡洛结算）")
    print("=" * 60)
    print(f"市场数：        {res['market_count']}")
    print(f"初始资金：      ${res['initial_capital']:.2f}")
    print(f"总交易数：      {summary.total_trades}")
    print(f"盈利交易：      {summary.winning_trades}")
    print(f"亏损交易：      {summary.losing_trades}")
    print(f"胜率：          {summary.win_rate:.2%}")
    print(f"总盈亏：        ${summary.total_pnl:.2f}")
    print(f"平均盈利：      ${summary.avg_win:.2f}")
    print(f"平均亏损：      ${summary.avg_loss:.2f}")
    print(f"最大回撤：      {summary.max_drawdown:.2%}")
    print(f"夏普比率：      {summary.sharpe_ratio:.2f}")
    print(f"盈亏比：        {summary.profit_factor:.2f}")
    print(f"平均持仓时间：  {summary.avg_holding_time_sec:.1f}s")
    print(f"最终资金：      ${res['final_capital']:.2f}")
    if res["initial_capital"] > 0:
        ret = (res["final_capital"] - res["initial_capital"]) / res["initial_capital"]
        print(f"收益率：        {ret:.2%}")
    print(f"输出目录：      {res['out_dir']}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

