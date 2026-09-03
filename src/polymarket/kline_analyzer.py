import math
import time
import threading
import statistics
from typing import Dict, Any, List, Optional
import requests

from polymarket.config import (
    HTTP_PROXY,
    HTTPS_PROXY,
    CRYPTO_CHOP_MAX_AMPLITUDE,
    CRYPTO_CHOP_MAX_NET_CHANGE,
    ASSET_CHOP_THRESHOLDS,
    SUPPORTED_ASSETS,
    GK_EWMA_ENABLED,
    GK_EWMA_HALF_LIFE,
)
from polymarket.logger import logger

# 全局内存缓存与线程锁
_asset_status: Dict[str, Dict[str, Any]] = {}
_status_lock = threading.RLock()
_session = requests.Session()


# ─────────────────────────────────────────────
# 纯函数：Garman-Klass 逐 K 线方差
# ─────────────────────────────────────────────
def _gk_variance(o: float, h: float, l: float, c: float) -> float:
    """
    计算单根 K 线的 Garman-Klass 方差（无量纲，百分比²的分数形式）。

    公式（Garman & Klass, 1980）:
        σ²_GK = 0.5 * ln(H/L)² - (2*ln2 - 1) * ln(C/O)²

    说明：
    - 方差估计效率比纯 close-to-close 法提升约 7.4×
    - 单边趋势 K 线（O≈L, C≈H）可能产生微小负值 → 必须在调用方 clamp 到 0
    """
    if o <= 0 or h <= 0 or l <= 0 or c <= 0:
        return 0.0
    ln_hl = math.log(h / l)
    ln_co = math.log(c / o)
    # 2*ln(2) - 1 ≈ 0.38629
    return 0.5 * ln_hl * ln_hl - (2.0 * math.log(2.0) - 1.0) * ln_co * ln_co


def _ewma_weights(n: int, half_life: int) -> List[float]:
    """
    生成长度为 n 的 EWMA 指数衰减权重向量（最新根权重最大）。

    衰减因子 λ = 2^(-1/half_life)，使得间隔 half_life 根的权重恰好减半。
    索引 0 为最旧根（权重最低），索引 n-1 为最新根（权重 = 1.0）。
    """
    if n <= 0:
        return []
    lam = 2.0 ** (-1.0 / max(half_life, 1))
    # 最新根权重 = λ^0 = 1.0；往旧每步乘以 λ
    weights = [lam ** (n - 1 - i) for i in range(n)]
    return weights


# ─────────────────────────────────────────────
# 核心 K 线抓取与 GK+EWMA 分析
# ─────────────────────────────────────────────
def _fetch_kline_and_analyze(asset: str, limit: int = 10) -> Dict[str, Any]:
    """底层单次 K 线抓取与波动率分析核心逻辑（升级为 Garman-Klass + EWMA）。"""
    asset_upper = asset.upper()
    # Binance OHLC 格式：[open_time, open, high, low, close, ...]
    url = (
        f"https://api.binance.com/api/v3/klines"
        f"?symbol={asset_upper}USDT&interval=1m&limit={limit}"
    )

    proxies = {}
    if HTTP_PROXY:
        proxies["http"] = HTTP_PROXY
    if HTTPS_PROXY:
        proxies["https"] = HTTPS_PROXY

    try:
        r = _session.get(url, proxies=proxies, timeout=3.5)
        r.raise_for_status()
        data = r.json()

        if not data or len(data) < limit:
            return {
                "is_choppy": True, "error": "数据不足",
                "timestamp": time.time(), "amplitude": 0.0, "net_change": 0.0,
                "gk_volatility": 0.0, "close_volatility_3sigma": 0.0,
            }

        # ── 提取 OHLC 序列 ──────────────────────────────
        opens  = [float(k[1]) for k in data]
        highs  = [float(k[2]) for k in data]
        lows   = [float(k[3]) for k in data]
        closes = [float(k[4]) for k in data]

        # ── 旧版 close 3σ 振幅（备份字段，仅供对比参考）──
        mean_close = statistics.mean(closes)
        stdev_close = statistics.stdev(closes) if len(closes) > 1 else 0.0
        stdev_pct = (stdev_close / mean_close) * 100 if mean_close > 0 else 0.0
        close_volatility_3sigma = stdev_pct * 3

        # ── 极短期 1m 动量飞刀冲击（沿用原逻辑）──────────
        last_k = data[-1]
        last_open  = float(last_k[1])
        last_close = float(last_k[4])
        last_1m_net_change = (
            abs(last_close - last_open) / last_open * 100
            if last_open > 0 else 0.0
        )

        # ── GK+EWMA 波动率计算 ──────────────────────────
        if GK_EWMA_ENABLED:
            # 逐根计算 GK 方差，并 clamp 负值（评审 Issue-1）
            gk_vars = [
                max(_gk_variance(opens[i], highs[i], lows[i], closes[i]), 0.0)
                for i in range(len(data))
            ]
            # EWMA 加权均方差（评审建议：半衰期 4，权重尾部 ≈ 17%）
            weights = _ewma_weights(len(gk_vars), GK_EWMA_HALF_LIFE)
            w_sum = sum(weights)
            if w_sum > 0:
                gk_var_ewma = sum(w * v for w, v in zip(weights, gk_vars)) / w_sum
            else:
                gk_var_ewma = 0.0
            # 转换为百分比波动率
            gk_volatility = math.sqrt(gk_var_ewma) * 100.0
            # 主 amplitude 字段替换为 GK+EWMA（下游零感知）
            amplitude = gk_volatility

            # net_change：改用最近 3 根 GK 方差均值与阈值比较（评审追加建议）
            # 消除均值漂移对净变动的影响，反映"近期真实短期波动强度"
            recent_gk_vars = gk_vars[-3:] if len(gk_vars) >= 3 else gk_vars
            recent_gk_mean_vol = math.sqrt(
                sum(recent_gk_vars) / len(recent_gk_vars)
            ) * 100.0 if recent_gk_vars else 0.0
            net_change = recent_gk_mean_vol  # 单位 %，与旧 net_change 含义对齐
        else:
            # GK 未启用时降级回旧逻辑（完全向后兼容）
            gk_volatility = 0.0
            amplitude = close_volatility_3sigma
            net_change = (
                abs(closes[-1] - mean_close) / mean_close * 100
                if mean_close > 0 else 0.0
            )

        # ── 开仓决策 ──────────────────────────────────
        asset_cfg = ASSET_CHOP_THRESHOLDS.get(asset_upper, {})
        max_amp_thresh  = asset_cfg.get("max_amplitude", CRYPTO_CHOP_MAX_AMPLITUDE)
        max_net_thresh  = asset_cfg.get("max_net_change", CRYPTO_CHOP_MAX_NET_CHANGE)
        max_1m_shock_thresh = max_net_thresh * 0.65

        # 宏观波动率守门：amplitude AND net_change 双重门槛
        is_macro_choppy = (amplitude < max_amp_thresh) and (net_change < max_net_thresh)
        # 极短期 1m 动量飞刀冲击（保持不变）
        is_1m_shock = (last_1m_net_change >= max_1m_shock_thresh)
        is_choppy = is_macro_choppy and (not is_1m_shock)

        return {
            "is_choppy": is_choppy,
            "amplitude": amplitude,                         # GK+EWMA 波动率（主字段）
            "gk_volatility": gk_volatility,                # GK+EWMA 原值（调试用）
            "close_volatility_3sigma": close_volatility_3sigma,  # 旧 close 3σ（对比用）
            "net_change": net_change,                       # 短期波动强度（GK 路径）
            "last_1m_net_change": last_1m_net_change,
            "latest_price": closes[-1] if closes else 0.0,
            "error": "",
            "timestamp": time.time(),
        }
    except Exception as e:
        return {
            "is_choppy": True, "error": str(e),
            "timestamp": time.time(), "amplitude": 0.0, "net_change": 0.0,
            "gk_volatility": 0.0, "close_volatility_3sigma": 0.0,
        }


class KlineRefresherDaemon:
    """
    后台常驻非阻塞 K 线刷新守护线程 (Background Kline Refresher Daemon)。

    设计优势：
    1. 纯无阻塞：每 3 秒后台静默拉取并更新内存缓存，主交易事件循环 <0.01ms；
    2. 网络故障隔离：外部抖动或超时完全由后台线程消化，绝不影响状态机执行。
    """
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(KlineRefresherDaemon, cls).__new__(cls)
                cls._instance._started = False
        return cls._instance

    def start(self, interval_sec: float = 3.0):
        with self._lock:
            if self._started:
                return
            self._started = True
            self.interval_sec = interval_sec
            t = threading.Thread(
                target=self._run_loop, daemon=True, name="KlineRefresherDaemon"
            )
            t.start()
            logger.info("[KlineRefresherDaemon] 后台非阻塞 K 线刷新守护线程已启动。")

    def _run_loop(self):
        # 启动时立刻快速预热一次
        for a in SUPPORTED_ASSETS:
            res = _fetch_kline_and_analyze(a)
            with _status_lock:
                _asset_status[a.upper()] = res

        while True:
            try:
                for a in SUPPORTED_ASSETS:
                    res = _fetch_kline_and_analyze(a)
                    with _status_lock:
                        _asset_status[a.upper()] = res
                    time.sleep(0.5)
            except Exception as e:
                logger.warning(f"[KlineRefresherDaemon] 刷新 K 线异常: {e}")
            time.sleep(self.interval_sec)


# 启动后台常驻守护线程
_refresher = KlineRefresherDaemon()
_refresher.start()


def get_asset_status(asset: str, limit: int = 10, force_refresh: bool = False) -> dict:
    """读取指定资产的波动率与防爆盾状态 (默认纯内存零阻塞读取)。"""
    asset_upper = asset.upper()
    with _status_lock:
        st = _asset_status.get(asset_upper)
    if not st or force_refresh:
        st = _fetch_kline_and_analyze(asset_upper, limit=limit)
        with _status_lock:
            _asset_status[asset_upper] = st
    return st


def is_asset_choppy(asset: str, limit: int = 10, cache_ttl: float = 15.0) -> bool:
    """
    判断指定资产当前是否处于震荡横盘期（默认纯内存零阻塞；cache_ttl<=0 强制刷新）。
    使用 GK+EWMA 波动率估计器（若 GK_EWMA_ENABLED=true）或旧 close 3σ 法（降级）。
    """
    force_refresh = (cache_ttl <= 0.0)
    st = get_asset_status(asset, limit=limit, force_refresh=force_refresh)
    return st.get("is_choppy", True)


