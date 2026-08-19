import requests
from polymarket.config import HTTP_PROXY, HTTPS_PROXY, CRYPTO_CHOP_MAX_AMPLITUDE, CRYPTO_CHOP_MAX_NET_CHANGE
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

def is_asset_choppy(asset: str, limit: int = 10) -> bool:
    """
    判断指定的 asset (如 BTC, ETH) 当前是否处于震荡横盘期。
    
    规则：
    1. 获取近 limit 分钟的 1m K 线。
    2. 计算最高价和最低价的振幅，如果超过 CRYPTO_CHOP_MAX_AMPLITUDE，认定为单边行情。
    3. 计算收盘价和开盘价的净位移，如果超过 CRYPTO_CHOP_MAX_NET_CHANGE，认定为单边行情。
    
    返回：
    True: 处于震荡横盘期，可以安全执行双开双平套利。
    False: 处于单边波动期，建议空仓观望避免打损。
    """
    asset_upper = asset.upper()
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
            
        highs = [float(k[2]) for k in data]
        lows = [float(k[3]) for k in data]
        opens = [float(k[1]) for k in data]
        closes = [float(k[4]) for k in data]
        
        max_high = max(highs)
        min_low = min(lows)
        
        # 计算百分比振幅和净变化
        amplitude = (max_high - min_low) / min_low * 100
        net_change = abs(closes[-1] - opens[0]) / opens[0] * 100
        
        is_choppy = (amplitude < CRYPTO_CHOP_MAX_AMPLITUDE) and (net_change < CRYPTO_CHOP_MAX_NET_CHANGE)
        
        if not is_choppy:
            logger.warning(
                f"[风控] {asset_upper} 当前存在单边波动风险！\n"
                f"  振幅: {amplitude:.3f}% (阈值 {CRYPTO_CHOP_MAX_AMPLITUDE}%)\n"
                f"  净变动: {net_change:.3f}% (阈值 {CRYPTO_CHOP_MAX_NET_CHANGE}%)"
            )
        else:
            logger.info(f"[风控] {asset_upper} 行情稳定 (振幅 {amplitude:.3f}%)，允许入场。")
            
        _asset_status[asset_upper] = {
            "is_choppy": is_choppy,
            "amplitude": amplitude,
            "net_change": net_change,
            "error": "",
            "timestamp": time.time()
        }
        return is_choppy
        
    except Exception as e:
        logger.error(f"[风控] {asset_upper} Binance K 线获取异常，报错: {e}。保守放行。")
        _asset_status[asset_upper] = {"is_choppy": True, "error": str(e), "timestamp": time.time()}
        return True
