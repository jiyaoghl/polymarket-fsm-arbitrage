from __future__ import annotations

import json
import math
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from .types import Quote


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _iso_to_ts(s: str) -> Optional[float]:
    try:
        # e.g. "2026-03-17T10:13:00.000Z"
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
    initial_yes_mid: float


class PublicApproxFeed:
    """
    用 Gamma 公开信息构造“近似”报价序列。

    约束与取舍（MVP）：
    - Gamma 通常不提供历史 L2，因此这里用“随机游走 + 价差”近似 bid/ask。
    - 蒙特卡洛结算使用到期前的 YES mid 作为隐含概率。
    """

    def __init__(
        self,
        gamma_host: str,
        http_proxy: str = "",
        https_proxy: str = "",
        user_agent: str = "curl/8.13.0",
        timeout_sec: float = 3.0,
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

    def iter_btc_5m_markets(self, days: int) -> List[PublicMarket]:
        """
        近似发现过去 N 天的 btc-updown-5m-<ts> 市场（按 5min 窗口枚举 slug）。
        """
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=days)
        start_ts = int(start.timestamp())
        end_ts = int(now.timestamp())

        # 对齐到 5min
        start_ts = (start_ts // 300) * 300
        end_ts = (end_ts // 300) * 300

        markets: List[PublicMarket] = []

        slugs: List[Tuple[int, str]] = []
        for ts in range(start_ts, end_ts + 1, 300):
            slugs.append((ts, f"btc-updown-5m-{ts}"))

        # 并发拉取，显著加速（Gamma 单个请求成本较高）
        max_workers = 24
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = {
                ex.submit(self._fetch_market_by_slug, slug): (ts, slug) for ts, slug in slugs
            }
            for fut in as_completed(futs):
                ts, slug = futs[fut]
                try:
                    m = fut.result()
                except Exception:
                    m = None
                if not m:
                    continue
                pm = self._parse_public_market(slug, m, fallback_start_ts=float(ts))
                if pm:
                    markets.append(pm)

        # 保证按时间顺序
        markets.sort(key=lambda x: x.start_ts)

        return markets

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

        # token ids
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

        # times
        end_ts = None
        if isinstance(m.get("endDate"), str):
            end_ts = _iso_to_ts(m["endDate"])
        if end_ts is None:
            # 兜底：5min 市场
            end_ts = fallback_start_ts + 300

        start_ts = end_ts - 300

        # initial price: outcomePrices often like ["0.49","0.51"]
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
            start_ts=float(start_ts),
            end_ts=float(end_ts),
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
            initial_yes_mid=initial_yes_mid,
        )

    def iter_quotes(
        self,
        markets: List[PublicMarket],
        *,
        seed: int,
        quote_step_sec: int = 1,
        spread_bps: float = 20.0,
    ) -> Iterable[Quote]:
        """
        为每个市场生成近似报价序列（YES/NO 双边 bid/ask）。

        模型：
        - YES mid: 随机游走 + 向 0.5 轻微均值回归
        - NO mid: 1 - YES mid
        - bid/ask: mid ± spread/2
        """
        rng = random.Random(seed)
        half_spread = (spread_bps / 10_000.0) / 2.0

        for pm in markets:
            # 初始 mid
            y = pm.initial_yes_mid

            t = pm.start_ts
            while t <= pm.end_ts:
                # random walk with mean reversion
                # step volatility tuned for 5min horizon (heuristic)
                shock = rng.gauss(0.0, 0.003)
                drift = (0.5 - y) * 0.02
                y = _clamp(y + drift + shock, 0.01, 0.99)
                n = 1.0 - y

                yes_bid = _clamp(y - half_spread, 0.0, 1.0)
                yes_ask = _clamp(y + half_spread, 0.0, 1.0)
                no_bid = _clamp(n - half_spread, 0.0, 1.0)
                no_ask = _clamp(n + half_spread, 0.0, 1.0)

                yield Quote(
                    ts=float(t),
                    market_id=pm.market_id,
                    yes_bid=yes_bid,
                    yes_ask=yes_ask,
                    no_bid=no_bid,
                    no_ask=no_ask,
                )
                t += quote_step_sec

    @staticmethod
    def implied_yes_prob_from_quote(q: Quote) -> float:
        # Use mid as implied probability proxy
        yes_mid = (q.yes_bid + q.yes_ask) / 2.0
        return _clamp(yes_mid, 0.01, 0.99)

