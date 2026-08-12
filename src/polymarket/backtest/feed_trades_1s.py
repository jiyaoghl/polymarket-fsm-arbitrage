from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

from .types import Quote


DATA_API_BASE = "https://data-api.polymarket.com"


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _iso_to_ts(s: str) -> Optional[float]:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


@dataclass(frozen=True)
class PublicMarket:
    market_id: str  # conditionId
    slug: str
    start_ts: int
    end_ts: int
    yes_asset_id: str
    no_asset_id: str
    initial_yes_mid: float


class Trades1sFeed:
    """
    档2：用 Data API 的逐笔 trades 聚合成 1 秒行情（驱动回测引擎的 1s 步进）。

    数据来源：
    - Gamma: `GET /markets/slug/{slug}` 提供 conditionId + clobTokenIds + endDate + outcomePrices
    - Data API: `GET https://data-api.polymarket.com/trades?market=<conditionId>`

    说明：
    - Data API 的 `timestamp` 实测为 Unix 秒。
    - Trade 的 `asset` 字段是 outcome token 的 asset id（大整数/字符串），可用于区分 YES/NO。
    """

    def __init__(
        self,
        gamma_host: str,
        http_proxy: str = "",
        https_proxy: str = "",
        user_agent: str = "curl/8.13.0",
        timeout_sec: float = 10.0,
        max_workers: int = 12,
        cache_dir: str = "backtest_cache/trades",
    ):
        self.gamma_host = gamma_host.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": user_agent, "Accept": "application/json"}
        )
        proxies: Dict[str, str] = {}
        if http_proxy:
            proxies["http"] = http_proxy
        if https_proxy:
            proxies["https"] = https_proxy
        if proxies:
            self.session.proxies.update(proxies)
            self.session.trust_env = False
        self.timeout_sec = timeout_sec
        self.max_workers = max_workers
        self.cache_dir = cache_dir

    def iter_btc_5m_markets(self, days: int) -> List[PublicMarket]:
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=days)
        start_ts = int(start.timestamp())
        end_ts = int(now.timestamp())
        start_ts = (start_ts // 300) * 300
        end_ts = (end_ts // 300) * 300

        slugs: List[Tuple[int, str]] = [(ts, f"btc-updown-5m-{ts}") for ts in range(start_ts, end_ts + 1, 300)]

        out: List[PublicMarket] = []
        with ThreadPoolExecutor(max_workers=self.max_workers) as ex:
            futs = {ex.submit(self._fetch_market_by_slug, slug): (ts, slug) for ts, slug in slugs}
            for fut in as_completed(futs):
                ts, slug = futs[fut]
                try:
                    m = fut.result()
                except Exception:
                    m = None
                if not m:
                    continue
                pm = self._parse_public_market(slug, m, fallback_start_ts=ts)
                if pm:
                    out.append(pm)

        out.sort(key=lambda x: x.start_ts)
        return out

    def _fetch_market_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        url = f"{self.gamma_host}/markets/slug/{slug}"
        try:
            resp = self.session.get(url, timeout=self.timeout_sec)
            if resp.status_code != 200:
                return None
            return resp.json()
        except Exception:
            return None

    @staticmethod
    def _parse_public_market(slug: str, m: Dict[str, Any], fallback_start_ts: int) -> Optional[PublicMarket]:
        if not isinstance(m, dict):
            return None
        market_id = str(m.get("conditionId") or m.get("id") or "")
        if not market_id:
            return None

        # asset ids (YES/NO)
        clob_token_ids = m.get("clobTokenIds")
        yes_asset_id = ""
        no_asset_id = ""
        if isinstance(clob_token_ids, str):
            try:
                arr = json.loads(clob_token_ids)
                if isinstance(arr, list) and len(arr) >= 2:
                    yes_asset_id = str(arr[0])
                    no_asset_id = str(arr[1])
            except Exception:
                pass
        if not yes_asset_id or not no_asset_id:
            return None

        end_ts = None
        if isinstance(m.get("endDate"), str):
            end_ts = _iso_to_ts(m["endDate"])
        if end_ts is None:
            end_ts = float(fallback_start_ts + 300)
        end_ts_i = int(end_ts)
        start_ts_i = end_ts_i - 300

        # initial mid from outcomePrices[0]
        initial_yes_mid = 0.5
        op = m.get("outcomePrices")
        if isinstance(op, list) and len(op) >= 1:
            try:
                initial_yes_mid = float(op[0])
            except Exception:
                pass
        initial_yes_mid = _clamp(initial_yes_mid, 0.01, 0.99)

        return PublicMarket(
            market_id=market_id,
            slug=slug,
            start_ts=start_ts_i,
            end_ts=end_ts_i,
            yes_asset_id=yes_asset_id,
            no_asset_id=no_asset_id,
            initial_yes_mid=initial_yes_mid,
        )

    def fetch_trades_for_market(
        self,
        market_id: str,
        *,
        limit: int = 10000,
        max_pages: int = 5,
        taker_only: bool = True,
        offset_step: int = 10000,
        window_start_ts: int | None = None,
    ) -> List[Dict[str, Any]]:
        """
        拉取单个 market 的 trades（Data API）。
        Data API 仅支持 offset 分页，这里做一个上限保护。
        """
        cache_path = ""
        if self.cache_dir:
            try:
                os.makedirs(self.cache_dir, exist_ok=True)
                cache_path = os.path.join(self.cache_dir, f"{market_id}.json")
                if os.path.exists(cache_path):
                    with open(cache_path, "r", encoding="utf-8") as f:
                        cached = json.load(f)
                        if isinstance(cached, list):
                            return [x for x in cached if isinstance(x, dict)]
            except Exception:
                cache_path = ""

        out: List[Dict[str, Any]] = []
        offset = 0
        for _ in range(max_pages):
            params = {
                "market": market_id,
                "limit": limit,
                "offset": offset,
                "takerOnly": taker_only,
            }
            resp = self.session.get(f"{DATA_API_BASE}/trades", params=params, timeout=self.timeout_sec)
            resp.raise_for_status()
            arr = resp.json() or []
            if not isinstance(arr, list) or not arr:
                break
            out.extend([x for x in arr if isinstance(x, dict)])

            # Early stop: Data API 通常按 timestamp desc 返回；如果本页最早一条已经早于窗口开始，就没必要继续翻页
            if window_start_ts is not None:
                try:
                    min_ts = min(int(x.get("timestamp")) for x in arr if isinstance(x, dict) and x.get("timestamp") is not None)
                    if min_ts < window_start_ts:
                        break
                except Exception:
                    pass

            if len(arr) < limit:
                break
            offset += offset_step
        if cache_path:
            try:
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(out, f)
            except Exception:
                pass
        return out

    @staticmethod
    def _split_trades_by_asset(
        trades: List[Dict[str, Any]],
        yes_asset_id: str,
        no_asset_id: str,
        start_ts: int,
        end_ts: int,
    ) -> Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]:
        yes: List[Tuple[int, float]] = []
        no: List[Tuple[int, float]] = []
        for tr in trades:
            try:
                t = int(tr.get("timestamp"))
                if t < start_ts or t > end_ts:
                    continue
                asset = str(tr.get("asset"))
                p = float(tr.get("price"))
            except Exception:
                continue
            p = _clamp(p, 0.0, 1.0)
            if asset == yes_asset_id:
                yes.append((t, p))
            elif asset == no_asset_id:
                no.append((t, p))
        yes.sort(key=lambda x: x[0])
        no.sort(key=lambda x: x[0])
        return yes, no

    def iter_quotes(
        self,
        markets: List[PublicMarket],
        *,
        spread_bps: float = 20.0,
        taker_only: bool = True,
    ) -> Iterable[Quote]:
        """
        对每个 market：
        - 拉 trades（并发）
        - 聚合成 1 秒 last/mid（无交易则前向填充）
        - 合成 bid/ask
        """
        half_spread = (spread_bps / 10_000.0) / 2.0

        def _fetch(pm: PublicMarket):
            try:
                return pm.market_id, self.fetch_trades_for_market(
                    pm.market_id,
                    taker_only=taker_only,
                    window_start_ts=pm.start_ts,
                )
            except Exception:
                return pm.market_id, []

        # 先并发把 trades 拉齐，再按市场时间顺序产出 1s Quote（更符合时间轴）
        trades_by_market: Dict[str, List[Dict[str, Any]]] = {}
        with ThreadPoolExecutor(max_workers=self.max_workers) as ex:
            futs = [ex.submit(_fetch, pm) for pm in markets]
            for fut in as_completed(futs):
                mid, trades = fut.result()
                trades_by_market[mid] = trades

        for pm in sorted(markets, key=lambda x: x.start_ts):
            trades = trades_by_market.get(pm.market_id) or []
            if not trades:
                continue

            yes_tr, no_tr = self._split_trades_by_asset(
                trades, pm.yes_asset_id, pm.no_asset_id, pm.start_ts, pm.end_ts
            )

            yi = 0
            ni = 0
            yes_last = pm.initial_yes_mid
            no_last = 1.0 - pm.initial_yes_mid

            for t in range(pm.start_ts, pm.end_ts + 1):
                while yi < len(yes_tr) and yes_tr[yi][0] <= t:
                    yes_last = yes_tr[yi][1]
                    yi += 1
                while ni < len(no_tr) and no_tr[ni][0] <= t:
                    no_last = no_tr[ni][1]
                    ni += 1

                yes_mid = _clamp(yes_last, 0.01, 0.99)
                no_mid = _clamp(no_last, 0.01, 0.99)

                s = yes_mid + no_mid
                if s > 1e-9:
                    yes_mid = _clamp(yes_mid / s, 0.01, 0.99)
                    no_mid = _clamp(no_mid / s, 0.01, 0.99)

                yes_bid = _clamp(yes_mid - half_spread, 0.0, 1.0)
                yes_ask = _clamp(yes_mid + half_spread, 0.0, 1.0)
                no_bid = _clamp(no_mid - half_spread, 0.0, 1.0)
                no_ask = _clamp(no_mid + half_spread, 0.0, 1.0)

                yield Quote(
                    ts=float(t),
                    market_id=pm.market_id,
                    yes_bid=yes_bid,
                    yes_ask=yes_ask,
                    no_bid=no_bid,
                    no_ask=no_ask,
                )

