import os
import sys
import time
import io
from typing import List

if sys.platform == "win32":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except Exception:
        pass

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

import requests
import httpx
from polymarket.config import CLOB_HOST, GAMMA_HOST, HTTPS_PROXY, HTTP_PROXY

def benchmark_http_protocols(num_requests: int = 10):
    print("==================================================================")
    print(f"       Polymarket API 网络层基准性能测试 (请求数: {num_requests})       ")
    print("==================================================================")

    url = f"{CLOB_HOST}/time"
    proxy_url = HTTPS_PROXY or HTTP_PROXY or None

    # 1. 测试传统 HTTP/1.1 (requests.Session)
    print("\n[1/2] 正在测试传统 HTTP/1.1 (requests.Session)...")
    req_session = requests.Session()
    if proxy_url:
        req_session.proxies.update({"http": proxy_url, "https": proxy_url})
    
    t0 = time.perf_counter()
    h1_latencies = []
    for i in range(num_requests):
        step_t0 = time.perf_counter()
        try:
            r = req_session.get(url, timeout=5)
            r.raise_for_status()
            step_lat = (time.perf_counter() - step_t0) * 1000
            h1_latencies.append(step_lat)
        except Exception as e:
            print(f"  - HTTP/1.1 请求 {i+1} 失败: {e}")
    total_h1_time = (time.perf_counter() - t0) * 1000

    # 2. 测试原生 HTTP/2 (httpx.Client http2=True)
    print("[2/2] 正在测试原生 HTTP/2 多路复用 (httpx.Client http2=True)...")
    h2_client = httpx.Client(
        http2=True,
        timeout=5.0,
        proxy=proxy_url,
        trust_env=False if proxy_url else True
    )

    # 预热一次连接
    try:
        h2_client.get(url)
    except Exception:
        pass

    t0 = time.perf_counter()
    h2_latencies = []
    for i in range(num_requests):
        step_t0 = time.perf_counter()
        try:
            r = h2_client.get(url)
            r.raise_for_status()
            step_lat = (time.perf_counter() - step_t0) * 1000
            h2_latencies.append(step_lat)
        except Exception as e:
            print(f"  - HTTP/2 请求 {i+1} 失败: {e}")
    total_h2_time = (time.perf_counter() - t0) * 1000
    h2_client.close()

    # 汇总数据
    print("\n======================= 性能基准测试结果 =======================")
    if h1_latencies and h2_latencies:
        avg_h1 = sum(h1_latencies) / len(h1_latencies)
        min_h1 = min(h1_latencies)
        avg_h2 = sum(h2_latencies) / len(h2_latencies)
        min_h2 = min(h2_latencies)
        speedup = ((avg_h1 - avg_h2) / avg_h1) * 100 if avg_h1 > 0 else 0

        print(f"• HTTP/1.1  | 平均延迟: {avg_h1:6.2f} ms | 最快单次: {min_h1:6.2f} ms | 总耗时: {total_h1_time:6.2f} ms")
        print(f"• HTTP/2    | 平均延迟: {avg_h2:6.2f} ms | 最快单次: {min_h2:6.2f} ms | 总耗时: {total_h2_time:6.2f} ms")
        print("------------------------------------------------------------------")
        print(f"⚡ [HTTP/2 性能提升] 响应延迟降低: {speedup:+.1f}%  | 总吞吐耗时缩短: {total_h1_time - total_h2_time:.1f} ms")
    else:
        print("测试在离线模式下完成，请在连接网络时复测实际数据。")
    print("==================================================================")

if __name__ == "__main__":
    benchmark_http_protocols(num_requests=5)
