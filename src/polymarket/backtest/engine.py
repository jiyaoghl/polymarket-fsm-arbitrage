from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .types import BacktestConfig, Fill, OutcomeSide, Quote, Trade


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


@dataclass
class Position:
    yes_shares: float = 0.0
    no_shares: float = 0.0


class ExecutionModel:
    def __init__(self, taker_fee_bps: float, maker_fee_bps: float):
        self.taker_fee_bps = taker_fee_bps
        self.maker_fee_bps = maker_fee_bps

    def fee(self, notional_usdc: float, is_taker: bool) -> float:
        bps = self.taker_fee_bps if is_taker else self.maker_fee_bps
        return notional_usdc * (bps / 10_000.0)

    def fill_buy(self, q: Quote, outcome: OutcomeSide, size: float, order_type: str) -> Tuple[float, float, bool]:
        """
        返回 (fill_price, fee_usdc, is_taker).
        近似撮合：FOK 直接按 ask 成交；GTC 视为 maker，按 bid 成交（保守一点）或触价成交（这里取 bid 近似）。
        """
        if outcome == "YES":
            bid, ask = q.yes_bid, q.yes_ask
        else:
            bid, ask = q.no_bid, q.no_ask

        if order_type == "FOK":
            price = ask
            is_taker = True
        else:  # GTC
            price = bid
            is_taker = False

        notional = price * size
        fee = self.fee(notional, is_taker=is_taker)
        return price, fee, is_taker

    def fill_sell(self, q: Quote, outcome: OutcomeSide, size: float, order_type: str) -> Tuple[float, float, bool]:
        if outcome == "YES":
            bid, ask = q.yes_bid, q.yes_ask
        else:
            bid, ask = q.no_bid, q.no_ask

        if order_type == "FOK":
            price = bid
            is_taker = True
        else:
            price = ask
            is_taker = False

        notional = price * size
        fee = self.fee(notional, is_taker=is_taker)
        return price, fee, is_taker


class BacktestEngineV2:
    """
    事件驱动（Quote 序列）回测引擎：
    - 用 bid/ask 近似成交
    - 支持首腿、二腿、止损
    - 到期使用蒙特卡洛按隐含概率结算
    """

    def __init__(self, cfg: BacktestConfig):
        self.cfg = cfg
        self.exec_model = ExecutionModel(
            taker_fee_bps=cfg.taker_fee_bps,
            maker_fee_bps=cfg.maker_fee_bps,
        )
        self.rng = random.Random(cfg.seed)

        self.cash = cfg.initial_capital
        self.position_by_market: Dict[str, Position] = {}
        self.active_trade_by_market: Dict[str, Dict] = {}

        self.fills: List[Fill] = []
        self.trades: List[Trade] = []
        self.equity_curve: List[Tuple[float, float]] = []

        # For Monte Carlo: keep last quote per market
        self.last_quote_by_market: Dict[str, Quote] = {}
        self.market_end_ts: Dict[str, float] = {}
        self.market_start_ts: Dict[str, float] = {}

    def on_market_start(self, market_id: str, start_ts: float, end_ts: float) -> None:
        self.market_start_ts[market_id] = start_ts
        self.market_end_ts[market_id] = end_ts
        self.position_by_market.setdefault(market_id, Position())

    def on_quote(self, q: Quote) -> None:
        self.last_quote_by_market[q.market_id] = q

        # Update equity curve snapshot
        self.equity_curve.append((q.ts, self.mark_to_market_equity(q.ts)))

        # If already have an active trade, manage it
        st = self.active_trade_by_market.get(q.market_id)
        if st:
            self._maybe_reentry_or_stop(q, st)
            return

        # Otherwise check entry
        self._maybe_enter(q)

    def on_market_end(self, market_id: str) -> None:
        """
        结算该 market 的持仓（如果还有）。
        """
        q = self.last_quote_by_market.get(market_id)
        if not q:
            return

        pos = self.position_by_market.get(market_id)
        if not pos:
            return

        # 先取出交易状态（即使没有剩余头寸也要生成 trade 记录）
        st = self.active_trade_by_market.pop(market_id, None)

        settlement_pnl = 0.0
        if not (pos.yes_shares == 0.0 and pos.no_shares == 0.0):
            # implied probability from last mid
            yes_prob = _clamp((q.yes_bid + q.yes_ask) / 2.0, 0.01, 0.99)
            # Monte Carlo payoff
            settlement_pnl = self._monte_carlo_settlement_pnl(
                pos, yes_prob, paths=self.cfg.monte_carlo_paths
            )

            self.cash += settlement_pnl

            # Close out positions
            pos.yes_shares = 0.0
            pos.no_shares = 0.0

        if st:
            leg2_outcome = st.get("leg2_outcome")
            trade = Trade(
                market_id=market_id,
                entry_ts=st["entry_ts"],
                exit_ts=q.ts,
                leg1_outcome=st["leg1_outcome"],
                leg2_outcome=leg2_outcome,
                leg1_cost=st["leg1_cost"],
                leg2_cost=st.get("leg2_cost"),
                size=st["size"],
                pnl=st.get("realized_pnl", 0.0) + settlement_pnl,
                status=st.get("status", "expired"),
                reason=st.get("reason", "expiry_settlement"),
            )
            self.trades.append(trade)

    def mark_to_market_equity(self, ts: float) -> float:
        # cash + sum(position * mid)
        eq = self.cash
        for mid, pos in self._iter_mark_to_market():
            eq += mid * pos
        return eq

    def _iter_mark_to_market(self):
        # yields (mid_price, shares) pairs for all positions
        for market_id, pos in self.position_by_market.items():
            q = self.last_quote_by_market.get(market_id)
            if not q:
                continue
            yes_mid = (q.yes_bid + q.yes_ask) / 2.0
            no_mid = (q.no_bid + q.no_ask) / 2.0
            if pos.yes_shares:
                yield yes_mid, pos.yes_shares
            if pos.no_shares:
                yield no_mid, pos.no_shares

    def _maybe_enter(self, q: Quote) -> None:
        # Max concurrent open markets
        if self.cfg.max_open_markets > 0 and len(self.active_trade_by_market) >= int(self.cfg.max_open_markets):
            return

        # Only allow entry in first N seconds after market start (match live strategy)
        start_ts = self.market_start_ts.get(q.market_id)
        if start_ts is not None and (q.ts - start_ts) > float(self.cfg.entry_window_sec):
            return

        # Choose cheaper ask side
        if q.yes_ask <= q.no_ask:
            outcome: OutcomeSide = "YES"
            ask = q.yes_ask
        else:
            outcome = "NO"
            ask = q.no_ask

        if ask > self.cfg.entry_max_price:
            return

        # Position sizing: USDC amount / price -> shares
        if ask <= 0:
            return
        size = self.cfg.order_amount_usdc / ask

        price, fee, _ = self.exec_model.fill_buy(q, outcome, size=size, order_type="FOK")
        cost = price * size + fee

        if self.cash < cost:
            return

        self.cash -= cost
        pos = self.position_by_market[q.market_id]
        if outcome == "YES":
            pos.yes_shares += size
        else:
            pos.no_shares += size

        self.fills.append(
            Fill(
                ts=q.ts,
                market_id=q.market_id,
                outcome=outcome,
                side="BUY",
                price=price,
                size=size,
                fee=fee,
                order_type="FOK",
            )
        )

        self.active_trade_by_market[q.market_id] = {
            "entry_ts": q.ts,
            "leg1_outcome": outcome,
            "leg1_cost": price,
            "leg1_fee": fee,
            "leg1_total_cost": cost,
            "size": size,
            "status": "leg1_only",
            "reason": "",
            "realized_pnl": 0.0,
        }

    def _maybe_reentry_or_stop(self, q: Quote, st: Dict) -> None:
        end_ts = self.market_end_ts.get(q.market_id)
        if end_ts is None:
            return
        time_to_expiry = end_ts - q.ts

        if st.get("status") != "leg1_only":
            return

        leg1_outcome: OutcomeSide = st["leg1_outcome"]
        other_outcome: OutcomeSide = "NO" if leg1_outcome == "YES" else "YES"

        other_ask = q.no_ask if other_outcome == "NO" else q.yes_ask
        # Reentry
        if other_ask < self.cfg.reentry_trigger_price and time_to_expiry > 10:
            # allow GTC based on config, else use FOK
            order_type = "GTC" if self.cfg.allow_leg2_gtc else "FOK"
            if other_ask <= 0:
                return
            size = st["size"]
            price, fee, _ = self.exec_model.fill_buy(q, other_outcome, size=size, order_type=order_type)
            cost = price * size + fee
            if self.cash < cost:
                return

            self.cash -= cost
            pos = self.position_by_market[q.market_id]
            if other_outcome == "YES":
                pos.yes_shares += size
            else:
                pos.no_shares += size

            self.fills.append(
                Fill(
                    ts=q.ts,
                    market_id=q.market_id,
                    outcome=other_outcome,
                    side="BUY",
                    price=price,
                    size=size,
                    fee=fee,
                    order_type=order_type,
                )
            )

            st["leg2_outcome"] = other_outcome
            st["leg2_cost"] = price
            st["status"] = "locked"
            st["reason"] = "reentry"
            return

        # Stop loss: sell leg1 near end
        if 10 < time_to_expiry <= self.cfg.stop_loss_time_remaining_sec:
            size = st["size"]
            price, fee, _ = self.exec_model.fill_sell(q, leg1_outcome, size=size, order_type="FOK")
            proceeds = price * size - fee

            # Reduce position
            pos = self.position_by_market[q.market_id]
            if leg1_outcome == "YES":
                sellable = min(pos.yes_shares, size)
                pos.yes_shares -= sellable
            else:
                sellable = min(pos.no_shares, size)
                pos.no_shares -= sellable

            self.cash += proceeds
            realized = proceeds - float(st.get("leg1_total_cost", st["leg1_cost"] * size))
            st["realized_pnl"] = st.get("realized_pnl", 0.0) + realized
            st["status"] = "stopped"
            st["reason"] = "stop_loss"

            self.fills.append(
                Fill(
                    ts=q.ts,
                    market_id=q.market_id,
                    outcome=leg1_outcome,
                    side="SELL",
                    price=price,
                    size=size,
                    fee=fee,
                    order_type="FOK",
                )
            )

    def _monte_carlo_settlement_pnl(self, pos: Position, yes_prob: float, paths: int) -> float:
        """
        计算到期结算带来的 PnL（相对于当前 cash，不包含建仓成本；建仓成本已在下单时扣除现金）。
        - 每股到期 payout 为 1 或 0
        - 这里返回 payout 的期望（用 MC 近似）减去 0（成本已发生）
        """
        if paths <= 0:
            paths = 1

        total_payout = 0.0
        for _ in range(paths):
            yes_wins = self.rng.random() < yes_prob
            payout = 0.0
            if yes_wins:
                payout += pos.yes_shares * 1.0
            else:
                payout += pos.no_shares * 1.0
            total_payout += payout

        expected_payout = total_payout / paths
        return expected_payout

