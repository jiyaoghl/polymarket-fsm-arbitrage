import sys
import os
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from polymarket.client import PolyClient, get_poly_signature

def benchmark():
    client = PolyClient(is_live=True)
    if not client.wallet:
        from eth_account import Account
        client.wallet = Account.create()

    token_id = "72845409470103349987888317924255911537618556478819840544959071464291077251665"
    price = 0.45
    amount = 10.0
    side = "BUY"

    # 1. 预热
    for _ in range(5):
        client._create_v2_signed_order(token_id, price, amount, side)
        get_poly_signature(int(time.time()), "POST", "/order", "{}")

    # 2. 测试 EIP-712 原生签名耗时
    iterations = 100
    t0 = time.perf_counter()
    for _ in range(iterations):
        client._create_v2_signed_order(token_id, price, amount, side)
    t1 = time.perf_counter()
    eip712_avg_ms = ((t1 - t0) / iterations) * 1000

    # 3. 测试 L2 HMAC-SHA256 签名耗时
    t2 = time.perf_counter()
    for _ in range(iterations):
        get_poly_signature(int(time.time()), "POST", "/order", '{"test":"payload"}')
    t3 = time.perf_counter()
    hmac_avg_ms = ((t3 - t2) / iterations) * 1000

    print(f"=== Polymarket 签名性能基准测试 (单次迭代 x {iterations}) ===")
    print(f"1. EIP-712 原生椭圆曲线签名耗时 : {eip712_avg_ms:.3f} ms ({eip712_avg_ms*1000:.1f} μs)")
    print(f"2. L2 HMAC-SHA256 鉴权签名耗时  : {hmac_avg_ms:.3f} ms ({hmac_avg_ms*1000:.1f} μs)")
    print(f"3. 下单前总计算耗时 (纯 CPU)   : {eip712_avg_ms + hmac_avg_ms:.3f} ms")

if __name__ == "__main__":
    benchmark()
