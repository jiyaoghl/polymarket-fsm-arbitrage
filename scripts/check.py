#!/usr/bin/env python3
"""
Polymarket 5Min 套利机器人 - 快捷查询与诊断 CLI 工具

使用方法:
  python check.py           # 一键全面诊断与看板输出
  python check.py balance   # 快捷查询钱包与链上/CLOB余额
  python check.py trades    # 快捷查看历史交易持仓与对冲记录
  python check.py status    # 快捷检查系统后台进程与端口状态
"""

import sys
import os
import time
import sqlite3
import argparse
from typing import Dict, Any

# 将 src 目录添加到 Python 路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"), override=True)
load_dotenv(os.path.join(os.path.dirname(__file__), "configs", ".env"), override=True)

from polymarket.config import PK, CLOB_HOST, HTTP_PROXY, HTTPS_PROXY
from polymarket.client import PolyClient


def check_balance():
    """查询钱包与账户余额"""
    print("\n" + "=" * 55)
    print(" [1/3] 钱包地址与账户余额查询")
    print("=" * 55)

    client = PolyClient(is_live=True)
    wallet_addr = client.wallet.address if client.wallet else "未绑定私钥"
    print(f"主钱包地址: {wallet_addr}")

    if wallet_addr == "未绑定私钥":
        print("未检测到有效私钥 POLX_PK，跳过链上余额查询。\n")
        return

    # Polygon 链上查询
    import requests
    rpc_url = "https://polygon-bor-rpc.publicnode.com"
    proxy_url = HTTPS_PROXY or HTTP_PROXY or ""
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None

    tokens = {
        "POL/MATIC (Gas 费)": ("NATIVE", 18),
        "USDC.e (Bridged USDC)": ("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174", 6),
        "USDC (Native USDC)": ("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", 6),
        "pUSD (Polymarket USD 官方)": ("0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB", 6),
    }

    print("\n--- Polygon 链上资产 (Polygon Mainnet) ---")
    addr_clean = wallet_addr.lower().replace("0x", "").zfill(64)
    data_hex = "0x70a08231" + addr_clean

    for name, (contract, decimals) in tokens.items():
        try:
            if contract == "NATIVE":
                payload = {"jsonrpc": "2.0", "method": "eth_getBalance", "params": [wallet_addr, "latest"], "id": 1}
            else:
                payload = {"jsonrpc": "2.0", "method": "eth_call", "params": [{"to": contract, "data": data_hex}, "latest"], "id": 1}

            resp = requests.post(rpc_url, json=payload, proxies=proxies, timeout=8)
            resp_json = resp.json()
            if "error" in resp_json:
                raise Exception(resp_json["error"].get("message", "RPC Error"))
                
            hex_val = resp_json.get("result", "0x0")
            if hex_val == "0x" or not hex_val:
                hex_val = "0x0"
            val = int(hex_val, 16) / (10**decimals)
            unit = name.split(" ")[0]
            print(f"  * {name:<32}: {val:>10.4f} {unit}")
        except Exception as e:
            print(f"  * {name:<32}: 查询失败 ({e})")

    # 模拟盘余额
    sim_client = PolyClient(is_live=False)
    sim_bal = sim_client.get_balance()
    print(f"\n--- 模拟交易账户 (Mock Mode) ---")
    print(f"  * 模拟盘初始/当前本金            : {sim_bal.get('usdc', 10000.0):>10.2f} USDC")


def check_trades(limit: int = 10):
    """查询数据库中的历史持仓与对冲记录"""
    print("\n" + "=" * 55)
    print(f" [2/3] 历史交易与持仓明细 (最近 {limit} 笔)")
    print("=" * 55)

    db_path = os.path.join(os.path.dirname(__file__), "tmp", "trading.db")
    if not os.path.exists(db_path):
        print(f"尚未检索到数据库文件：{db_path}\n")
        return

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='positions';")
        if not cursor.fetchone():
            print("数据库中尚未生成 positions 表（可能尚未触发交易）。\n")
            conn.close()
            return

        cursor.execute("SELECT * FROM positions ORDER BY rowid DESC LIMIT ?", (limit,))
        rows = cursor.fetchall()
        conn.close()

        if not rows:
            print("当前数据库中暂无交易记录。\n")
            return

        print(f"{'开仓时间 (UTC+8)':<20} | {'市场 ID (前缀)':<14} | {'方向':<4} | {'开仓价':<7} | {'金额 (USDC)':<10} | {'最终状态'}")
        print("-" * 75)
        for r in rows:
            row_dict = dict(r)
            created_at = row_dict.get("created_at", "N/A")
            mkt_id = row_dict.get("market_id", "N/A")
            side = row_dict.get("leg1_side") or row_dict.get("side") or "NO"
            price = row_dict.get("leg1_price") or row_dict.get("entry_price") or row_dict.get("price") or 0.0
            amount = row_dict.get("amount_usdc") or row_dict.get("amount") or 0.0
            status = row_dict.get("status", "N/A")

            mkt_short = (str(mkt_id)[:10] + "...") if mkt_id else "N/A"
            created_str = str(created_at).replace("T", " ")[:19]
            print(f"{created_str:<20} | {mkt_short:<14} | {side:<4} | ${float(price):<6.2f} | {float(amount):>8.2f} USDC | {status}")

    except Exception as e:
        print(f"读取数据库失败: {e}")


def check_status():
    """检查系统与监控 Dashboard 服务健康状态"""
    print("\n" + "=" * 55)
    print(" [3/3] 服务健康度与端口状态诊断")
    print("=" * 55)

    import socket
    dashboard_port = 8888
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2.0)
    result = sock.connect_ex(("127.0.0.1", dashboard_port))
    sock.close()

    if result == 0:
        print(f"  * Dashboard 面板服务 (Port {dashboard_port}) : [运行中] (http://127.0.0.1:{dashboard_port}/)")
    else:
        print(f"  * Dashboard 面板服务 (Port {dashboard_port}) : [未启动]")

    # 网络代理检查
    proxy_env = HTTPS_PROXY or HTTP_PROXY
    if proxy_env:
        print(f"  * 网络代理配置 (HTTP/HTTPS Proxy)   : [已启用] ({proxy_env})")
    else:
        print(f"  * 网络代理配置 (HTTP/HTTPS Proxy)   : [未配置] (直连模式)")


def main():
    parser = argparse.ArgumentParser(description="Polymarket 5Min 套利机器人快捷查询工具")
    parser.add_argument(
        "command",
        nargs="?",
        default="all",
        choices=["all", "balance", "trades", "status"],
        help="指定查询命令: all (全景 diagnostic), balance (余额), trades (持仓历史), status (服务健康度)",
    )
    args = parser.parse_args()

    cmd = args.command
    if cmd == "all":
        check_balance()
        check_trades(10)
        check_status()
        print("\n" + "=" * 55)
        print(" 快速查询完成！需求进一步操作可执行 `python check.py [balance|trades|status]`")
        print("=" * 55 + "\n")
    elif cmd == "balance":
        check_balance()
    elif cmd == "trades":
        check_trades(15)
    elif cmd == "status":
        check_status()


if __name__ == "__main__":
    main()
