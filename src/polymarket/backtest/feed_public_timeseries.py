from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

from .types import Quote


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _iso_to_ts(s: str) -> Optional[float]:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


@dataclass(frozen=True)
class PublicMarket:
    market_id: str
    slug: str
    start_ts: float
    end_ts: float
    yes_token_id: str
    no_token_id: str


class PublicTimeSeriesFeed:
    """
    次优方案（更真实的公开数据）：
    - 用 Gamma 枚举 btc-updown-5m-<ts> 市场拿 token ids
    - 用 CLOB `GET /prices-history?market=<assetId>` 拉 YES/NO 的历史价格序列
    - 用真实 mid + 合成 spread 生成 bid/ask Quote

    说明：
    - /prices-history 的 `market` 参数文档写的是 market(asset id)，实测对 CLOB token/asset id 可用；
      若未来接口变化，本 feed 会抛错，runner 会回退到 approx feed。
    """

    def __init__(
        self,
        gamma_host: str,
        clob_host: str,
        http_proxy: str = "",
        https_proxy: str = "",
        user_agent: str = "curl/8.13.0",
        timeout_sec: float = 6.0,
    ):
        self.gamma_host = gamma_host.rstrip("/")
        self.clob_host = clob_host.rstrip("/")
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

    def iter_btc_5m_markets(self, days: int) -> List[PublicMarket]:
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=days)
        start_ts = int(start.timestamp())
        end_ts = int(now.timestamp())
        start_ts = (start_ts // 300) * 300
        end_ts = (end_ts // 300) * 300

        out: List[PublicMarket] = []
        for ts in range(start_ts, end_ts + 1, 300):
            slug = f"btc-updown-5m-{ts}"
            m = self._fetch_market_by_slug(slug)
            if not m:
                continue
            pm = self._parse_public_market(slug, m, fallback_start_ts=float(ts))
            if pm:
                out.append(pm)
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
    def _parse_public_market(
        slug: str, m: Dict[str, Any], fallback_start_ts: float
    ) -> Optional[PublicMarket]:
        if not isinstance(m, dict):
            return None

        market_id = str(m.get("conditionId") or m.get("id") or "")
        if not market_id:
            return None

        clob_token_ids = m.get("clobTokenIds")
        yes_token_id = ""
        no_token_id = ""
        if isinstance(clob_token_ids, str):
            try:
                arr = json.loads(clob_token_ids)
                if isinstance(arr, list) and len(arr) >= 2:
                    yes_token_id = str(arr[0])
                    no_token_id = str(arr[1])
            except Exception:
                pass
        if not yes_token_id or not no_token_id:
            return None

        end_ts = None
        if isinstance(m.get("endDate"), str):
            end_ts = _iso_to_ts(m["endDate"])
        if end_ts is None:
            end_ts = fallback_start_ts + 300

        start_ts = end_ts - 300
        return PublicMarket(
            market_id=market_id,
            slug=slug,
            start_ts=float(start_ts),
            end_ts=float(end_ts),
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
        )

    def fetch_prices_history(
        self,
        asset_id: str,
        *,
        start_ts: float,
        end_ts: float,
        interval: str = "1m",
        fidelity: int = 1,
    ) -> List[Tuple[float, float]]:
        url = f"{self.clob_host}/prices-history"
        params = {
            "market": asset_id,
            "startTs": start_ts,
            "endTs": end_ts,
            "interval": interval,
            "fidelity": fidelity,
        }
        resp = self.session.get(url, params=params, timeout=self.timeout_sec)
        resp.raise_for_status()
        data = resp.json() or {}
        hist = data.get("history", []) or []
        out: List[Tuple[float, float]] = []
        for item in hist:
            try:
                t = float(item["t"])
                p = float(item["p"])
                out.append((t, _clamp(p, 0.0, 1.0)))
            except Exception:
                continue
        out.sort(key=lambda x: x[0])
        return out

    def iter_quotes(
        self,
        markets: List[PublicMarket],
        *,
        spread_bps: float = 20.0,
        interval: str = "1m",
        fidelity: int = 1,
    ) -> Iterable[Quote]:
        half_spread = (spread_bps / 10_000.0) / 2.0

        for pm in markets:
            yes_hist = self.fetch_prices_history(
                pm.yes_token_id,
                start_ts=pm.start_ts,
                end_ts=pm.end_ts,
                interval=interval,
                fidelity=fidelity,
            )
            no_hist: List[Tuple[float, float]] = []
            try:
                no_hist = self.fetch_prices_history(
                    pm.no_token_id,
                    start_ts=pm.start_ts,
                    end_ts=pm.end_ts,
                    interval=interval,
                    fidelity=fidelity,
                )
            except Exception:
                # 允许 NO 拉不到时用 1-YES 近似
                no_hist = []

            if not yes_hist:
                # 没有时间序列就跳过（runner 会回退到 approx）
                continue

            # 合并时间轴：用 YES 的时间戳为主，NO forward-fill（或 1-YES）
            no_idx = 0
            last_no = None
            for t, yes_mid in yes_hist:
                while no_idx < len(no_hist) and no_hist[no_idx][0] <= t:
                    last_no = no_hist[no_idx][1]
                    no_idx += 1
                if last_no is None:
                    no_mid = 1.0 - yes_mid
                else:
                    no_mid = last_no

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

