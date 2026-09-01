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
)
from polymarket.logger import logger

# 全局内存缓存与线程锁
_asset_status: Dict[str, Dict[str, Any]] = {}
_status_lock = threading.RLock()
_session = requests.Session()


def _fetch_kline_and_analyze(asset: str, limit: int = 10) -> Dict[str, Any]:
    """底层单次 K 线抓取与统计分析核心逻辑。"""
    asset_upper = asset.upper()
    url = f"https://api.binance.com/api/v3/klines?symbol={asset_upper}USDT&interval=1m&limit={limit}"
    
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
            return {"is_choppy": True, "error": "数据不足", "timestamp": time.time(), "amplitude": 0.0, "net_change": 0.0}
            
        closes = [float(k[4]) for k in data]
        mean_close = statistics.mean(closes)
        stdev_close = statistics.stdev(closes) if len(closes) > 1 else 0.0
        
        # 1. 统计分布振幅 (3倍标准差)
        stdev_pct = (stdev_close / mean_close) * 100
        amplitude = stdev_pct * 3
        
        # 2. 均值回归偏离度
        net_change = abs(closes[-1] - mean_close) / mean_close * 100
        
        # 3. 极短期 1m 动量飞刀冲击
        last_k = data[-1]
        last_open = float(last_k[1])
        last_close = float(last_k[4])
        last_1m_net_change = (abs(last_close - last_open) / last_open * 100) if last_open > 0 else 0.0

        asset_cfg = ASSET_CHOP_THRESHOLDS.get(asset_upper, {})
        max_amp_thresh = asset_cfg.get("max_amplitude", CRYPTO_CHOP_MAX_AMPLITUDE)
        max_net_thresh = asset_cfg.get("max_net_change", CRYPTO_CHOP_MAX_NET_CHANGE)
        max_1m_shock_thresh = max_net_thresh * 0.65
        
        is_macro_choppy = (amplitude < max_amp_thresh) and (net_change < max_net_thresh)
        is_1m_shock = (last_1m_net_change >= max_1m_shock_thresh)
        is_choppy = is_macro_choppy and (not is_1m_shock)
        
        return {
            "is_choppy": is_choppy,
            "amplitude": amplitude,
            "net_change": net_change,
            "last_1m_net_change": last_1m_net_change,
            "latest_price": closes[-1] if closes else 0.0,
            "error": "",
            "timestamp": time.time()
        }
    except Exception as e:
        return {"is_choppy": True, "error": str(e), "timestamp": time.time(), "amplitude": 0.0, "net_change": 0.0}


class KlineRefresherDaemon:
    """
    后台常驻非阻塞 K 线刷新守护线程 (Background Kline Refresher Daemon)。
    
    设计优势：
    1. 纯无阻塞：每 3 秒后台静默拉取并更新内存缓存，主交易事件循环与 Handler 耗时 <0.01ms；
    2. 网络故障隔离：外部网络抖动或超时完全由后台线程消化，绝对不影响状态机执行。
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
            t = threading.Thread(target=self._run_loop, daemon=True, name="KlineRefresherDaemon")
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
    判断指定的 asset 当前是否处于震荡横盘期 (默认纯内存零阻塞读取；若 cache_ttl <= 0 则强制刷新)。
    """
    force_refresh = (cache_ttl <= 0.0)
    st = get_asset_status(asset, limit=limit, force_refresh=force_refresh)
    return st.get("is_choppy", True)

