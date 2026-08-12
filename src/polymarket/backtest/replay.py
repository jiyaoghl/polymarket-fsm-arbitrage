from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

from .feed_trades_1s import Trades1sFeed, PublicMarket
from .types import Quote


@dataclass(frozen=True)
class ReplayBundle:
    markets: List[PublicMarket]
    quotes_by_market: Dict[str, List[Quote]]


def build_replay_bundle(
    *,
    feed: Trades1sFeed,
    days: int,
    max_markets: int | None = None,
    spread_bps: float = 20.0,
) -> ReplayBundle:
    markets = feed.iter_btc_5m_markets(days=days)
    if max_markets is not None and max_markets > 0:
        markets = markets[-max_markets:]

    quotes_by_market: Dict[str, List[Quote]] = {}
    for q in feed.iter_quotes(markets, spread_bps=spread_bps):
        quotes_by_market.setdefault(q.market_id, []).append(q)

    return ReplayBundle(markets=markets, quotes_by_market=quotes_by_market)

