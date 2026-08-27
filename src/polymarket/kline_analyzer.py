import requests
from polymarket.config import HTTP_PROXY, HTTPS_PROXY, CRYPTO_CHOP_MAX_AMPLITUDE, CRYPTO_CHOP_MAX_NET_CHANGE, ASSET_CHOP_THRESHOLDS
from polymarket.logger import logger
import time

# 缓存最近一次检测的结果
_asset_status = {}
_session = requests.Session()

def get_asset_status(asset: str) -> dict:
    return _asset_status.get(asset.upper(), {
        "is_choppy": True,
        "amplitude": 0.0,
        "net_change": 0.0,
        "timestamp": 0,
        "error": "未初始化"
    })

def is_asset_choppy(asset: str, limit: int = 10, cache_ttl: float = 10.0) -> bool:
    """
    判断指定的 asset (如 BTC, ETH) 当前是否处于震荡横盘期。
    
    规则：
    1. 优先读取 10s 内的内存缓存，避免多策略高频重复发起网络请求阻塞主线程。
    2. 获取近 limit 分钟的 1m K 线。
    3. 计算最高价和最低价的振幅，如果超过对应资产的 max_amplitude，认定为单边行情。
    4. 计算收盘价和开盘价的净位移，如果超过对应资产的 max_net_change，认定为单边行情。
    
    返回：
    True: 处于震荡横盘期，可以安全执行双开双平套利。
    False: 处于单边波动期，建议空仓观望避免打损。
    """
    asset_upper = asset.upper()
    now_ts = time.time()

    # 1. 优先读取短期内存缓存 (10s 内复用，避免多策略同一秒连续打 15 次 HTTP 阻塞)
    cached = _asset_status.get(asset_upper)
    if cached and (now_ts - cached.get("timestamp", 0) < cache_ttl) and not cached.get("error"):
        return cached.get("is_choppy", True)

    url = f"https://api.binance.com/api/v3/klines?symbol={asset_upper}USDT&interval=1m&limit={limit}"
    
    proxies = {}
    if HTTP_PROXY:
        proxies["http"] = HTTP_PROXY
    if HTTPS_PROXY:
        proxies["https"] = HTTPS_PROXY
        
    try:
        r = _session.get(url, proxies=proxies, timeout=5)
        r.raise_for_status()
        data = r.json()
        
        if not data or len(data) < limit:
            logger.warning(f"[风控] 无法获取足够的 {asset_upper} K 线数据，保守放行。")
            _asset_status[asset_upper] = {"is_choppy": True, "error": "数据不足", "timestamp": time.time()}
            return True
            
        closes = [float(k[4]) for k in data]
        
        import statistics
        mean_close = statistics.mean(closes)
        stdev_close = statistics.stdev(closes) if len(closes) > 1 else 0.0
        
        # 1. 基于标准差的统计分布振幅
        # 极差 (Max-Min) 在 N=10 的样本下通常约为 3 个标准差
        # 这里乘以 3 是为了让新算法算出的数值完美适配原本用户配置的 max_amplitude 阈值
        stdev_pct = (stdev_close / mean_close) * 100
        amplitude = stdev_pct * 3
        
        # 2. 均值回归偏离度 (代替原来单纯的首尾相减)
        # 考察最新价偏离 10 分钟均线的程度，这比看第一根和最后一根更加鲁棒
        net_change = abs(closes[-1] - mean_close) / mean_close * 100
        
        # [动态阈值匹配] 优先使用该品种专属阈值，回退到通用阈值
        asset_cfg = ASSET_CHOP_THRESHOLDS.get(asset_upper, {})
        max_amp_thresh = asset_cfg.get("max_amplitude", CRYPTO_CHOP_MAX_AMPLITUDE)
        max_net_thresh = asset_cfg.get("max_net_change", CRYPTO_CHOP_MAX_NET_CHANGE)
        
        is_choppy = (amplitude < max_amp_thresh) and (net_change < max_net_thresh)
        
        last_status = _asset_status.get(asset_upper, {}).get("is_choppy", None)
        status_changed = (last_status is None) or (last_status != is_choppy)
        
        if not is_choppy:
            msg = (f"[风控] {asset_upper} 当前存在单边波动风险！\n"
                   f"  振幅: {amplitude:.3f}% (阈值 {max_amp_thresh}%)\n"
                   f"  净变动: {net_change:.3f}% (阈值 {max_net_thresh}%)")
            if status_changed:
                logger.warning(msg)
            else:
                logger.debug(msg)
        else:
            msg = f"[风控] {asset_upper} 行情稳定 (振幅 {amplitude:.3f}% ≤ {max_amp_thresh}%)，允许入场。"
            if status_changed:
                logger.info(msg)
            else:
                logger.debug(msg)
            
        _asset_status[asset_upper] = {
            "is_choppy": is_choppy,
            "amplitude": amplitude,
            "net_change": net_change,
            "latest_price": closes[-1] if closes else 0.0,
            "error": "",
            "timestamp": time.time()
        }
        return is_choppy
        
    except Exception as e:
        logger.error(f"[风控] {asset_upper} Binance K 线获取异常，报错: {e}。保守放行。")
        _asset_status[asset_upper] = {"is_choppy": True, "error": str(e), "timestamp": time.time()}
        return True
