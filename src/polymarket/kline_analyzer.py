import requests
from polymarket.config import HTTP_PROXY, HTTPS_PROXY, BTC_CHOP_MAX_AMPLITUDE, BTC_CHOP_MAX_NET_CHANGE
from polymarket.logger import logger

def is_btc_choppy(limit: int = 10) -> bool:
    """
    判断 BTC 当前是否处于震荡横盘期。
    
    规则：
    1. 获取近 limit 分钟的 1m K 线。
    2. 计算最高价和最低价的振幅，如果超过 BTC_CHOP_MAX_AMPLITUDE，认定为单边行情。
    3. 计算收盘价和开盘价的净位移，如果超过 BTC_CHOP_MAX_NET_CHANGE，认定为单边行情。
    
    返回：
    True: 处于震荡横盘期，可以安全执行双开双平套利。
    False: 处于单边波动期，建议空仓观望避免打损。
    """
    url = f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&limit={limit}"
    
    proxies = {}
    if HTTP_PROXY:
        proxies["http"] = HTTP_PROXY
    if HTTPS_PROXY:
        proxies["https"] = HTTPS_PROXY
        
    try:
        r = requests.get(url, proxies=proxies, timeout=5)
        r.raise_for_status()
        data = r.json()
        
        if not data or len(data) < limit:
            logger.warning("[风控] 无法获取足够的 Binance K 线数据，保守放行。")
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
        
        is_choppy = (amplitude < BTC_CHOP_MAX_AMPLITUDE) and (net_change < BTC_CHOP_MAX_NET_CHANGE)
        
        if not is_choppy:
            logger.warning(
                f"[风控] BTC 当前存在单边波动风险！\n"
                f"  振幅: {amplitude:.3f}% (阈值 {BTC_CHOP_MAX_AMPLITUDE}%)\n"
                f"  净变动: {net_change:.3f}% (阈值 {BTC_CHOP_MAX_NET_CHANGE}%)"
            )
        else:
            logger.info(f"[风控] BTC 行情稳定 (振幅 {amplitude:.3f}%)，允许入场。")
            
        return is_choppy
        
    except Exception as e:
        logger.error(f"[风控] Binance K 线获取异常，报错: {e}。保守放行。")
        # 考虑到 API 限制或网络波动，保守起见在接口挂掉时，不强行阻止策略运行，避免永远不交易。
        # 这里可以选择 True (信任其他风控) 或 False (绝对安全)。
        return True
