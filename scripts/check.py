#!/usr/bin/env python3
"""
Polymarket 5Min 套利机器人 - 快捷查询与全链路诊断 CLI 工具

使用方法:
  python scripts/check.py             # 一键全面诊断与看板输出 (网络测速 + 钱包资产 + 实盘记录 + 本地持仓 + 服务状态)
  python scripts/check.py live        # 快捷查询当前钱包在 Polymarket 的真实链上/CLOB成交历史
  python scripts/check.py latency     # 快捷测试网络与各节点全链路往返延迟 (RTT)
  python scripts/check.py balance     # 快捷查询钱包与链上/CLOB余额
  python scripts/check.py trades      # 快捷查看本地数据库历史持仓与对冲记录
  python scripts/check.py status      # 快捷检查系统后台进程与端口状态
"""

import sys
import os
import time
import json
import sqlite3
import argparse
import requests
from typing import Dict, Any, List, Tuple

# 跨平台正确将项目根目录下的 src 添加到 Python 路径
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
sys.path.insert(0, SRC_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, ".env"), override=True)
load_dotenv(os.path.join(PROJECT_ROOT, "configs", ".env"), override=True)

from polymarket.config import (
    PK, CLOB_HOST, GAMMA_HOST, RPC_URL, HTTP_PROXY, HTTPS_PROXY, DB_PATH
)
from polymarket.client import PolyClient

# ANSI 颜色定义
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def get_proxies_dict() -> Dict[str, str]:
    proxy_url = HTTPS_PROXY or HTTP_PROXY or ""
    if proxy_url:
        return {"http": proxy_url, "https": proxy_url}
    return None


# ==============================================================================
# 1. 网络与节点延迟深度测试 (Latency Benchmark)
# ==============================================================================

def measure_http_latency(url: str, name: str, samples: int = 3, proxies: Dict[str, str] = None) -> Tuple[float, float, str]:
    """测量 HTTP/REST 端点的往返时延 (RTT)"""
    import requests
    latencies = []
    status_msg = "OK"

    for _ in range(samples):
        t0 = time.perf_counter()
        try:
            resp = requests.get(url, proxies=proxies, timeout=5)
            t1 = time.perf_counter()
            if resp.status_code < 400:
                latencies.append((t1 - t0) * 1000.0)
            else:
                status_msg = f"HTTP {resp.status_code}"
        except Exception:
            status_msg = "超时/连接失败"
            break
        time.sleep(0.05)

    if not latencies:
        return -1.0, 0.0, status_msg

    avg_lat = sum(latencies) / len(latencies)
    min_lat = min(latencies)
    return avg_lat, min_lat, "OK"


def measure_ws_latency(ws_url: str, samples: int = 2) -> Tuple[float, float, str]:
    """测量 WebSocket 握手与连接建立延迟"""
    import asyncio
    import websockets

    latencies = []
    status_msg = "OK"

    async def _test_once():
        t0 = time.perf_counter()
        try:
            async with websockets.connect(ws_url, open_timeout=5, close_timeout=2) as ws:
                t1 = time.perf_counter()
                return (t1 - t0) * 1000.0
        except Exception:
            return None

    for _ in range(samples):
        res = asyncio.run(_test_once())
        if res is not None:
            latencies.append(res)
        else:
            status_msg = "WS 握手失败"
        time.sleep(0.05)

    if not latencies:
        return -1.0, 0.0, status_msg

    avg_lat = sum(latencies) / len(latencies)
    min_lat = min(latencies)
    return avg_lat, min_lat, "OK"


def check_latency():
    """执行全链路核心服务网络延迟测速"""
    print("\n" + "=" * 65)
    print(f" {BOLD}[网络测速] 全链路核心服务往返时延 (RTT Benchmark){RESET}")
    print("=" * 65)

    proxies = get_proxies_dict()
    proxy_info = f"启用 ({HTTPS_PROXY or HTTP_PROXY})" if proxies else "未配置 (直连)"
    print(f"当前代理模式: {proxy_info}\n")

    targets = [
        {
            "name": "Polymarket CLOB 撮合 API",
            "type": "HTTP",
            "url": f"{CLOB_HOST}/time",
            "target": "clob_rest"
        },
        {
            "name": "Polymarket Gamma 市场数据",
            "type": "HTTP",
            "url": f"{GAMMA_HOST}/events?limit=1",
            "target": "gamma_rest"
        },
        {
            "name": "Polymarket CLOB WebSocket",
            "type": "WS",
            "url": "wss://ws-subscriptions-clob.polymarket.com/ws/market",
            "target": "clob_ws"
        },
        {
            "name": "Binance K线行情源 (防爆盾)",
            "type": "HTTP",
            "url": "https://api.binance.com/api/v3/ping",
            "target": "binance_rest"
        },
        {
            "name": "Polygon 链上 RPC 节点",
            "type": "RPC",
            "url": RPC_URL or "https://polygon-rpc.com",
            "target": "polygon_rpc"
        },
    ]

    print(f"{'目标服务 / 端点':<28} | {'类型':<5} | {'平均时延 (Avg)':<14} | {'最佳时延 (Min)':<14} | {'评级'}")
    print("-" * 75)

    for item in targets:
        name = item["name"]
        t_type = item["type"]
        url = item["url"]

        if t_type == "WS":
            avg_l, min_l, msg = measure_ws_latency(url)
        else:
            avg_l, min_l, msg = measure_http_latency(url, name, proxies=proxies)

        if avg_l < 0:
            print(f"{name:<28} | {t_type:<5} | {RED}{msg:<14}{RESET} | {RED}{'-':<14}{RESET} | {RED}不可用 ❌{RESET}")
        else:
            if avg_l < 150:
                grade = f"{GREEN}极佳 🚀{RESET}"
            elif avg_l < 350:
                grade = f"{CYAN}良好 ⚡{RESET}"
            elif avg_l < 600:
                grade = f"{YELLOW}一般 ⚠️{RESET}"
            else:
                grade = f"{RED}高延迟 🐢{RESET}"

            print(f"{name:<28} | {t_type:<5} | {avg_l:>8.1f} ms     | {min_l:>8.1f} ms     | {grade}")

    print("-" * 75)
    print("💡 评级参考: <150ms 极适合高频抢单 | 150~350ms 适合 Taker-Maker 做市 | >500ms 建议切换 VPS 机房节点\n")


# ==============================================================================
# 2. 钱包与账户余额查询
# ==============================================================================

def check_balance():
    """查询钱包与账户余额"""
    print("\n" + "=" * 65)
    print(f" {BOLD}[账户资金] 钱包地址与链上/CLOB余额查询{RESET}")
    print("=" * 65)

    client = PolyClient(is_live=True)
    wallet_addr = client.wallet.address if client.wallet else "未绑定私钥"
    print(f"主钱包地址: {BOLD}{wallet_addr}{RESET}")

    if wallet_addr == "未绑定私钥":
        print(f"{YELLOW}未检测到有效私钥 POLX_PK，跳过链上资产查询。{RESET}\n")
        return

    # 候选 RPC 节点列表（优先使用配置的 RPC，自动降级至公共高可用节点）
    rpc_candidates = [
        r for r in [
            RPC_URL,
            "https://polygon-rpc.com",
            "https://polygon-bor-rpc.publicnode.com",
            "https://1rpc.io/matic"
        ] if r and "your_alchemy_key" not in r
    ]
    proxies = get_proxies_dict()

    tokens = {
        "POL/MATIC (Gas 费)": ("NATIVE", 18),
        "pUSD (Polymarket USD 抵押品)": ("0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB", 6),
        "USDC.e (Bridged USDC)": ("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174", 6),
        "USDC (Native USDC)": ("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", 6),
    }

    print("\n--- Polygon 链上主网资产 (Polygon Mainnet) ---")
    addr_clean = wallet_addr.lower().replace("0x", "").zfill(64)
    data_hex = "0x70a08231" + addr_clean

    for name, (contract, decimals) in tokens.items():
        success = False
        val = 0.0
        last_err = ""

        for rpc_url in rpc_candidates:
            try:
                if contract == "NATIVE":
                    payload = {"jsonrpc": "2.0", "method": "eth_getBalance", "params": [wallet_addr, "latest"], "id": 1}
                else:
                    payload = {"jsonrpc": "2.0", "method": "eth_call", "params": [{"to": contract, "data": data_hex}, "latest"], "id": 1}

                resp = requests.post(rpc_url, json=payload, proxies=proxies, timeout=6)
                if resp.status_code != 200:
                    continue
                resp_json = resp.json()
                if not isinstance(resp_json, dict) or "error" in resp_json:
                    continue

                hex_val = resp_json.get("result", "0x0")
                if hex_val == "0x" or not hex_val:
                    hex_val = "0x0"
                val = int(hex_val, 16) / (10**decimals)
                success = True
                break
            except Exception as e:
                last_err = str(e)
                continue

        unit = name.split(" ")[0]
        if success:
            color = GREEN if val > 0 else YELLOW
            print(f"  * {name:<28}: {color}{val:>10.4f} {unit}{RESET}")
        else:
            print(f"  * {name:<28}: {YELLOW}查询受限 (已尝试多个公共节点){RESET}")

    # CLOB 实盘可用抵押金查询
    try:
        live_bal = client.get_balance()
        if live_bal and float(live_bal.get("usdc", 0.0)) > 0:
            print(f"  * {'CLOB 撮合可用抵押品余额':<28}: {GREEN}{float(live_bal.get('usdc', 0.0)):>10.4f} pUSD/USDC{RESET}")
    except Exception:
        pass

    # 模拟盘余额
    sim_client = PolyClient(is_live=False)
    sim_bal = sim_client.get_balance()
    print(f"\n--- 模拟盘账户状态 (Mock Mode) ---")
    print(f"  * 模拟账户虚拟本金              : {GREEN}{sim_bal.get('usdc', 100.0):>10.2f} USDC{RESET}")


# ==============================================================================
# 3. 当前钱包真实链上/CLOB成交历史 (Live Real Trades)
# ==============================================================================

def check_live_trades(limit: int = 20):
    """查询当前钱包在 Polymarket 官方服务器上的真实历史成交记录，并计算单笔 EV 与累计总 EV"""
    print("\n" + "=" * 65)
    print(f" {BOLD}[实盘记录] 当前钱包链上/CLOB真实成交历史 (最近 {limit} 笔){RESET}")
    print("=" * 65)

    client = PolyClient(is_live=True)
    wallet_addr = client.wallet.address if client.wallet else None

    if not wallet_addr or wallet_addr == "未绑定私钥":
        print(f"{YELLOW}未检测到有效私钥 POLX_PK，无法查询钱包实盘历史。{RESET}\n")
        return

    print(f"查询钱包: {BOLD}{wallet_addr}{RESET}\n")

    import requests
    proxies = get_proxies_dict()
    
    # 途径 1: Polymarket Data API
    url = f"https://data-api.polymarket.com/trades?user={wallet_addr.lower()}&limit={limit}"
    
    try:
        resp = requests.get(url, proxies=proxies, timeout=10)
        if resp.status_code == 200:
            trades = resp.json()
            if not trades or not isinstance(trades, list):
                print(f"{CYAN}当前钱包在 Polymarket 暂无真实成交记录（尚未产生实盘交易）。{RESET}\n")
                return
                
            total_trades = len(trades)
            total_usdc_vol = 0.0
            total_expected_ev = 0.0
            total_fee_est = 0.0

            print(f"{'成交时间 (UTC)':<19} | {'市场/标的':<22} | {'方向':<4} | {'结果':<4} | {'成交价':<7} | {'金额(USDC)':<10} | {'单笔净 EV (到期/锁利)':<18} | {'Tx Hash / ID'}")
            print("-" * 105)
            
            for t in trades:
                ts = t.get("timestamp") or t.get("match_time") or t.get("created_at")
                if isinstance(ts, (int, float)):
                    if ts > 1e11:  # ms
                        ts = ts / 1000.0
                    time_str = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(ts))
                else:
                    time_str = str(ts)[:19] if ts else "N/A"
                    
                title = t.get("title") or t.get("question") or t.get("market_slug") or t.get("condition_id") or "N/A"
                title_short = (title[:20] + "..") if len(title) > 22 else title
                
                side = str(t.get("side", "BUY")).upper()
                outcome = str(t.get("outcome") or t.get("asset") or ("YES" if t.get("outcome_index") == 0 else "NO")).upper()
                price = float(t.get("price", 0.0))
                size = float(t.get("size", 0.0))
                usdc_val = float(t.get("usdc_size") or t.get("cash_amount") or (price * size))
                
                total_usdc_vol += usdc_val
                
                # 估算手续费与单笔 EV
                fee_rate = 0.01 if side == "BUY" else 0.0
                fee_val = usdc_val * fee_rate
                total_fee_est += fee_val
                
                # 单笔 EV: 若为 BUY，理论到期期望毛利为 (1 - price) * size，扣除手续费即为净 EV
                if side == "BUY":
                    if price > 0:
                        single_ev = ((1.0 - price) * size) - fee_val
                    else:
                        single_ev = 0.0
                else:
                    single_ev = usdc_val - fee_val
                    
                total_expected_ev += single_ev
                
                tx_hash = t.get("transaction_hash") or t.get("id") or "N/A"
                tx_short = (str(tx_hash)[:10] + "...") if len(str(tx_hash)) > 12 else str(tx_hash)
                
                side_color = GREEN if side == "BUY" else RED
                outcome_color = GREEN if outcome in ("YES", "UP") else YELLOW
                ev_color = GREEN if single_ev > 0 else (RED if single_ev < 0 else YELLOW)
                
                print(f"{time_str:<19} | {title_short:<22} | {side_color}{side:<4}{RESET} | {outcome_color}{outcome:<4}{RESET} | ${price:<6.3f} | ${usdc_val:>8.2f} | {ev_color}${single_ev:>10.4f} USDC{RESET} | {tx_short}")
                
            print("-" * 105)
            
            ev_summary_color = GREEN if total_expected_ev > 0 else (RED if total_expected_ev < 0 else YELLOW)
            avg_single_ev = (total_expected_ev / total_trades) if total_trades > 0 else 0.0

            print(f"{BOLD}📊 实盘交易统计看板 (共检索到 {total_trades} 笔实盘记录):{RESET}")
            print(f"  • 累计交易总额 (Total Volume) : ${total_usdc_vol:>10.2f} USDC")
            print(f"  • 累计理论总 EV (Total Net EV): {ev_summary_color}${total_expected_ev:>10.4f} USDC{RESET}")
            print(f"  • 预估手续费磨损 (Est. Fees) : {YELLOW}${total_fee_est:>10.4f} USDC{RESET}")
            print(f"  • 平均单笔 EV (Avg / Trade)   : {ev_summary_color}${avg_single_ev:>10.4f} USDC{RESET}\n")
            return
        else:
            print(f"{YELLOW}Polymarket Data API 响应: HTTP {resp.status_code} ({resp.text[:60]}){RESET}")
    except Exception as e:
        print(f"{YELLOW}查询实盘成交记录受限: {e}{RESET}")


# ==============================================================================
# 4. 本地数据库历史交易与对冲持仓明细
# ==============================================================================

def check_trades(limit: int = 10):
    """查询数据库中的历史持仓与对冲记录，并计算单笔 EV 与总 EV 统计"""
    print("\n" + "=" * 65)
    print(f" {BOLD}[本地记录] 历史套利与对冲持仓明细 (最近 {limit} 笔){RESET}")
    print("=" * 65)

    db_path = DB_PATH
    if not os.path.exists(db_path):
        db_path = os.path.join(PROJECT_ROOT, "tmp", "trading.db")

    if not os.path.exists(db_path):
        print(f"尚未检索到数据库文件：{db_path}\n")
        return

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # 优先读取 historical_trades 表
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='historical_trades';")
        if cursor.fetchone():
            # 1. 查询全部记录用于计算全局统计
            cursor.execute("SELECT * FROM historical_trades ORDER BY archived_at DESC")
            all_rows = cursor.fetchall()
            
            if all_rows:
                total_trades = len(all_rows)
                total_net_ev = 0.0
                total_gross = 0.0
                total_fee = 0.0
                win_count = 0

                for r in all_rows:
                    row_dict = dict(r)
                    ev_val = float(row_dict.get("ev", 0.0))
                    total_net_ev += ev_val
                    if ev_val > 0:
                        win_count += 1

                    t_json = row_dict.get("trade_json", "{}")
                    try:
                        t_data = json.loads(t_json)
                        total_gross += float(t_data.get("gross_profit_usdc") or (ev_val if ev_val > 0 else 0.0))
                        total_fee += float(t_data.get("fee_usdc", 0.0))
                    except Exception:
                        pass

                # 2. 截取最近 limit 笔进行明细展示
                recent_rows = all_rows[:limit]
                print(f"{'归档时间 (UTC)':<19} | {'市场 ID (前缀)':<14} | {'策略 ID':<22} | {'毛利':<8} | {'手续费':<7} | {'单笔净 EV':<12} | {'状态'}")
                print("-" * 96)
                
                for r in recent_rows:
                    row_dict = dict(r)
                    archived = str(row_dict.get("archived_at", "N/A"))[:19]
                    mkt_id = (str(row_dict.get("market_id"))[:10] + "...") if row_dict.get("market_id") else "N/A"
                    strat = str(row_dict.get("strategy_id", "default"))[:22]
                    net_ev = float(row_dict.get("ev", 0.0))
                    
                    t_json = row_dict.get("trade_json", "{}")
                    try:
                        t_data = json.loads(t_json)
                        status = t_data.get("status", "SETTLED")
                        gross = float(t_data.get("gross_profit_usdc") or (net_ev if net_ev > 0 else 0.0))
                        fee = float(t_data.get("fee_usdc", 0.0))
                    except Exception:
                        status = "SETTLED"
                        gross = net_ev if net_ev > 0 else 0.0
                        fee = 0.0

                    ev_color = GREEN if net_ev > 0 else (RED if net_ev < 0 else YELLOW)
                    print(f"{archived:<19} | {mkt_id:<14} | {strat:<22} | ${gross:>6.3f} | ${fee:>5.3f} | {ev_color}${net_ev:>9.4f}{RESET} | {status}")

                print("-" * 96)

                # 3. 输出总 EV 与统计看板
                win_rate = (win_count / total_trades * 100.0) if total_trades > 0 else 0.0
                avg_ev = (total_net_ev / total_trades) if total_trades > 0 else 0.0
                total_color = GREEN if total_net_ev > 0 else (RED if total_net_ev < 0 else YELLOW)

                print(f"{BOLD}📊 累计套利统计看板 (全量 {total_trades} 笔交易):{RESET}")
                print(f"  • 累计总净 EV (Total Net EV) : {total_color}${total_net_ev:>10.4f} USDC{RESET}")
                print(f"  • 累计总毛利 (Total Gross)   : {GREEN}${total_gross:>10.4f} USDC{RESET}")
                print(f"  • 累计手续费 (Total Fees)    : {YELLOW}${total_fee:>10.4f} USDC{RESET}")
                print(f"  • 历史胜率   (Win Rate)      : {BOLD}{win_rate:>9.1f} %{RESET} ({win_count}/{total_trades} 胜)")
                print(f"  • 平均单笔 EV (Avg / Trade)  : {total_color}${avg_ev:>10.4f} USDC{RESET}\n")

                conn.close()
                return

        # 兜底查询 positions 表
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='positions';")
        if cursor.fetchone():
            cursor.execute("SELECT * FROM positions ORDER BY rowid DESC LIMIT ?", (limit,))
            rows = cursor.fetchall()
            if rows:
                print(f"{'开仓时间':<19} | {'市场 ID (前缀)':<14} | {'方向':<4} | {'开仓价':<7} | {'金额 (USDC)':<10} | {'状态'}")
                print("-" * 75)
                for r in rows:
                    row_dict = dict(r)
                    created_str = str(row_dict.get("created_at", "N/A"))[:19]
                    mkt_short = (str(row_dict.get("market_id"))[:10] + "...") if row_dict.get("market_id") else "N/A"
                    side = row_dict.get("side") or "NO"
                    price = float(row_dict.get("cost_basis") or row_dict.get("price") or 0.0)
                    amount = float(row_dict.get("amount") or 0.0)
                    status = row_dict.get("status", "N/A")
                    print(f"{created_str:<19} | {mkt_short:<14} | {side:<4} | ${price:<6.2f} | {amount:>8.2f} USDC | {status}")
                conn.close()
                return

        print("当前数据库中暂无交易记录。\n")
        conn.close()

    except Exception as e:
        print(f"读取数据库失败: {e}")


# ==============================================================================
# ==============================================
# 5. 一键仓位结算与资金赎回 (Redeem & Claim)
# ==============================================================================

def check_and_redeem():
    """扫描并一键执行已结算市场的资金赎回 (Redeem & Claim)"""
    print("\n" + "=" * 65)
    print(f" {BOLD}[仓位结算] 一键扫描并赎回已到期盘口 (Redeem Payout){RESET}")
    print("=" * 65)

    client = PolyClient(is_live=True)
    if not client.wallet:
        print(f"{RED}❌ 未检测到有效私钥 POLX_PK，无法执行链上/CLOB结算赎回。{RESET}\n")
        return

    wallet_addr = client.wallet.address
    print(f"操作钱包: {BOLD}{wallet_addr}{RESET}\n")

    db_path = DB_PATH
    if not os.path.exists(db_path):
        db_path = os.path.join(PROJECT_ROOT, "tmp", "trading.db")

    market_ids = set()
    if os.path.exists(db_path):
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            for tbl in ("historical_trades", "positions", "active_trades_cache", "processed_markets"):
                cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{tbl}';")
                if cursor.fetchone():
                    cursor.execute(f"SELECT DISTINCT market_id FROM {tbl}")
                    for r in cursor.fetchall():
                        m_id = r["market_id"] if "market_id" in r.keys() else None
                        if m_id:
                            market_ids.add(m_id)
            conn.close()
        except Exception as e:
            print(f"读取本地数据库异常: {e}")

    if not market_ids:
        print(f"{YELLOW}本地数据库中暂无记录。尝试通过 CLOB 接口查询已结束市场...{RESET}")
        try:
            closed = client.get_closed_markets()
            for m in closed:
                m_id = m.get("condition_id") or m.get("id")
                if m_id:
                    market_ids.add(m_id)
        except Exception as e:
            print(f"查询已结束市场异常: {e}")

    if not market_ids:
        print(f"{CYAN}未检测到需要结算的市场。{RESET}\n")
        return

    print(f"共扫描到 {len(market_ids)} 个历史市场，正在逐一校验并触发结算...")
    print(f"{'市场 ID (前缀)':<24} | {'结算状态':<14} | {'赎回回款 (USDC)'}")
    print("-" * 65)

    success_count = 0
    total_payout = 0.0

    for m_id in market_ids:
        mkt_short = (str(m_id)[:20] + "...") if len(str(m_id)) > 22 else str(m_id)
        try:
            res = client.redeem(m_id)
            status = res.get("status", "UNKNOWN")
            payout = float(res.get("payout", 0.0))
            if status == "SUCCESS":
                success_count += 1
                total_payout += payout
                status_str = f"{GREEN}成功兑现{RESET}"
                payout_str = f"{GREEN}+${payout:.2f} USDC{RESET}"
            elif status == "SIMULATED":
                status_str = f"{CYAN}模拟成功{RESET}"
                payout_str = "$0.00"
            else:
                status_str = f"{YELLOW}无需结算/已领{RESET}"
                payout_str = "$0.00"

            print(f"{mkt_short:<24} | {status_str:<23} | {payout_str}")
        except Exception as e:
            print(f"{mkt_short:<24} | {RED}结算失败 ({e}){RESET}")

    print("-" * 65)
    print(f"{BOLD}🎉 结算完成！共成功赎回 {success_count} 个市场，累计回款: ${total_payout:.2f} USDC{RESET}\n")


# ==============================================================================
# 6. 服务健康度与端口诊断
# ==============================================================================

def check_status():
    """检查系统与监控 Dashboard 服务健康状态"""
    print("\n" + "=" * 65)
    print(f" {BOLD}[服务健康] Dashboard 监控与代理状态{RESET}")
    print("=" * 65)

    import socket
    dashboard_port = 8888
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1.5)
    result = sock.connect_ex(("127.0.0.1", dashboard_port))
    sock.close()

    if result == 0:
        print(f"  * Dashboard 面板服务 (Port {dashboard_port}) : {GREEN}[运行中]{RESET} (http://127.0.0.1:{dashboard_port}/)")
    else:
        print(f"  * Dashboard 面板服务 (Port {dashboard_port}) : {YELLOW}[未启动]{RESET}")

    proxy_env = HTTPS_PROXY or HTTP_PROXY
    if proxy_env:
        print(f"  * 网络代理配置 (HTTP/HTTPS Proxy)   : {GREEN}[已启用]{RESET} ({proxy_env})")
    else:
        print(f"  * 网络代理配置 (HTTP/HTTPS Proxy)   : {YELLOW}[未配置]{RESET} (直连模式)")


# ==============================================================================
# 主入口
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(description="Polymarket 5Min 套利机器人快捷查询与诊断 CLI")
    parser.add_argument(
        "command",
        nargs="?",
        default="all",
        choices=["all", "live", "latency", "balance", "trades", "redeem", "status"],
        help="指定查询指令: all (全景诊断), live (实盘成交), latency (网络测速), balance (资金余额), trades (本地持仓), redeem (一键结算赎回), status (服务健康度)",
    )
    args = parser.parse_args()

    cmd = args.command
    if cmd == "all":
        check_latency()
        check_balance()
        check_live_trades(10)
        check_trades(10)
        check_status()
        print("\n" + "=" * 65)
        print(f" {GREEN}全景诊断完成！一键结算可执行: python scripts/check.py redeem{RESET}")
        print("=" * 65 + "\n")
    elif cmd == "live":
        check_live_trades(20)
    elif cmd == "latency":
        check_latency()
    elif cmd == "balance":
        check_balance()
    elif cmd == "trades":
        check_trades(15)
    elif cmd == "redeem":
        check_and_redeem()
    elif cmd == "status":
        check_status()


if __name__ == "__main__":
    main()
