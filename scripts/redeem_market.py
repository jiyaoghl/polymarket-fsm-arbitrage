import os
import sys

# 跨平台路径安全规范，严格匹配 src
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from polymarket.client import PolyClient
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("redeem_tool")

def main():
    if len(sys.argv) < 2:
        logger.error("用法: python scripts/redeem_market.py <market_id>")
        logger.info("示例: python scripts/redeem_market.py 0x123abc...")
        sys.exit(1)

    market_id = sys.argv[1].strip()
    
    # 强制 is_live=True，否则只会执行模拟分支而不会真正发送上链签名
    client = PolyClient(is_live=True)
    
    logger.info(f"正在向 Polymarket 发起针对市场 {market_id} 的手动赎回请求...")
    try:
        result = client.redeem(market_id)
        logger.info(f"✅ 赎回结果: {result}")
    except Exception as e:
        logger.error(f"❌ 赎回失败: {e}")

if __name__ == "__main__":
    main()
