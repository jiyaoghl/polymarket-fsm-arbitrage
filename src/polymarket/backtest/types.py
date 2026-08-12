from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional


Side = Literal["BUY", "SELL"]
OutcomeSide = Literal["YES", "NO"]
OrderType = Literal["FOK", "GTC"]


@dataclass(frozen=True)
class Quote:
    ts: float
    market_id: str
    yes_bid: float
    yes_ask: float
    no_bid: float
    no_ask: float


@dataclass
class Fill:
    ts: float
    market_id: str
    outcome: OutcomeSide
    side: Side
    price: float
    size: float  # shares
    fee: float  # USDC
    order_type: OrderType


@dataclass
class Trade:
    market_id: str
    entry_ts: float
    exit_ts: float
    leg1_outcome: OutcomeSide
    leg2_outcome: Optional[OutcomeSide]
    leg1_cost: float  # avg fill price (USDC per share)
    leg2_cost: Optional[float]
    size: float  # shares
    pnl: float  # USDC
    status: str  # locked / stopped / expired
    reason: str


@dataclass
class BacktestConfig:
    days: int
    seed: int
    initial_capital: float
    order_amount_usdc: float
    entry_max_price: float
    reentry_trigger_price: float
    stop_loss_time_remaining_sec: int
    entry_window_sec: int
    max_open_markets: int
    quote_step_sec: int
    spread_bps: float
    taker_fee_bps: float
    maker_fee_bps: float
    monte_carlo_paths: int
    allow_leg2_gtc: bool

