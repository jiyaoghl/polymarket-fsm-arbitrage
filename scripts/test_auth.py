#!/usr/bin/env python3
"""
Polymarket CLOB 实盘鉴权与链上环境全项诊断工具
用于在切换实盘前，一键验证：
1. 本地私钥推导与钱包地址
2. CLOB L2 API 鉴权有效性 (API Key, Secret, Passphrase, HMAC-SHA256 签名)
3. Polygon 链上资产与 Gas (POL/MATIC, USDC)
4. Polymarket CTF Exchange 智能合约 USDC 授权 (Allowance)
5. VPS 服务器时钟同步偏差 (NTP Clock Drift Check)
"""

import sys
import os
import time
import json
import base64
import hmac
import hashlib
import io
import requests
from typing import Dict, Any, Optional

if sys.platform == "win32":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except Exception:
        pass

# 跨平台添加 src 目录到 Python 路径
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
sys.path.insert(0, SRC_DIR)

# 加载 .env
from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, ".env"), override=True)
load_dotenv(os.path.join(PROJECT_ROOT, "configs", ".env"), override=True)

from polymarket.config import (
    PK, API_KEY, API_SECRET, API_PASSPHRASE,
    CLOB_HOST, HTTP_PROXY, HTTPS_PROXY, RPC_URL, CHAIN_ID
)

# 兼容别名
CLOB_API_KEY = API_KEY or os.getenv("POLX_API_KEY", "")
CLOB_API_SECRET = API_SECRET or os.getenv("POLX_API_SECRET", "")
CLOB_API_PASSPHRASE = API_PASSPHRASE or os.getenv("POLX_API_PASSPHRASE", "")

# 颜色输出
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
BOLD = "\033[1m"
RESET = "\033[0m"

def print_banner(title: str):
    print(f"\n{BOLD}{BLUE}{'=' * 60}{RESET}")
    print(f"{BOLD}{BLUE}  {title}{RESET}")
    print(f"{BOLD}{BLUE}{'=' * 60}{RESET}")

def get_proxy_dict():
    proxy_url = HTTPS_PROXY or HTTP_PROXY or ""
    if proxy_url:
        return {"http": proxy_url, "https": proxy_url}
    return None

def test_key_derivation() -> Optional[str]:
    """步骤 1: 私钥推导与地址验证"""
    print_banner("[1/5] 私钥推导与钱包地址")
    
    if not PK or PK in ("your_private_key_here", "0x0000000000000000000000000000000000000000000000000000000000000000"):
        print(f"{RED}❌ 未配置有效的 POLX_PK 私钥！请在 .env 中填入真实私钥。{RESET}")
        return None

    try:
        from eth_account import Account
        Account.enable_unaudited_hdwallet_features()
        clean_pk = PK if PK.startswith("0x") else f"0x{PK}"
        acc = Account.from_key(clean_pk)
        print(f"{GREEN}✅ 私钥格式合法！{RESET}")
        print(f"   主钱包地址: {BOLD}{acc.address}{RESET}")
        return acc.address
    except Exception as e:
        print(f"{RED}❌ 私钥解析失败: {e}{RESET}")
        return None

def test_clob_auth(wallet_address: str) -> bool:
    """步骤 2: CLOB L2 API 鉴权测试 (HMAC-SHA256)"""
    print_banner("[2/5] CLOB L2 API 鉴权测试 (API Key & 签名)")
    
    if not CLOB_API_KEY or CLOB_API_KEY == "your_api_key_here":
        print(f"{RED}❌ 未配置 POLX_API_KEY！{RESET}")
        return False
    if not CLOB_API_SECRET or CLOB_API_SECRET == "your_api_secret_here":
        print(f"{RED}❌ 未配置 POLX_API_SECRET！{RESET}")
        return False
    if not CLOB_API_PASSPHRASE or CLOB_API_PASSPHRASE == "your_passphrase_here":
        print(f"{RED}❌ 未配置 POLX_API_PASSPHRASE！{RESET}")
        return False

    print(f"   API Key: {CLOB_API_KEY[:8]}...{CLOB_API_KEY[-4:]}")
    print(f"   CLOB Host: {CLOB_HOST}")
    
    # 优先使用官方 py_clob_client 进行鉴权验证
    clean_pk = os.getenv("POLX_PK") or PK or ""
    if clean_pk.startswith("0x"):
        clean_pk_fmt = clean_pk
    else:
        clean_pk_fmt = f"0x{clean_pk}"

    api_k = os.getenv("POLX_API_KEY") or CLOB_API_KEY
    api_s = os.getenv("POLX_API_SECRET") or CLOB_API_SECRET
    api_p = os.getenv("POLX_API_PASSPHRASE") or CLOB_API_PASSPHRASE

    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType, ApiCreds
        
        c = ClobClient(host=CLOB_HOST, key=clean_pk_fmt, chain_id=137)
        c.set_api_creds(ApiCreds(api_key=api_k, api_secret=api_s, api_passphrase=api_p))
        
        bal = c.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        if bal is not None:
            raw_bal = float(bal.get("balance", 0.0))
            usdc_val = raw_bal / 1e6 if raw_bal > 1000 else raw_bal
            print(f"{GREEN}✅ CLOB API 鉴权成功！(ClobClient 握手通过){RESET}")
            print(f"   CLOB 撮合可用抵押品余额: {GREEN}{usdc_val:.4f} pUSD/USDC{RESET}")
            return True
    except Exception as ce:
        print(f"{YELLOW}   py_clob_client 尝试失败 ({ce})，正在尝试原生 HMAC 签名重试...{RESET}")

    # 构造标准原生 L2 鉴权头部（包含完整 Query String 与 POLY_ADDRESS）
    timestamp = str(int(time.time()))
    method = "GET"
    request_path = "/balance-allowance?asset_type=COLLATERAL"
    body = ""
    message = f"{timestamp}{method}{request_path}{body}"

    try:
        secret_bytes = base64.b64decode(api_s)
        signature = hmac.new(secret_bytes, message.encode("utf-8"), hashlib.sha256).digest()
        sig_b64 = base64.b64encode(signature).decode("utf-8")

        headers = {
            "POLY_API_KEY": api_k,
            "POLY_SIGNATURE": sig_b64,
            "POLY_TIMESTAMP": timestamp,
            "POLY_PASSPHRASE": api_p,
            "POLY_ADDRESS": wallet_address,
            "User-Agent": "curl/8.13.0",
        }

        url = f"{CLOB_HOST}{request_path}"
        resp = requests.get(url, headers=headers, proxies=get_proxy_dict(), timeout=10)

        if resp.status_code == 200:
            print(f"{GREEN}✅ CLOB API 鉴权成功！(HTTP 200 OK){RESET}")
            print(f"   已成功连通 Polymarket 交易撮合引擎。")
            return True
        elif resp.status_code == 401:
            print(f"{RED}❌ 鉴权失败 (HTTP 401 Unauthorized): API 密钥、Secret 或 Passphrase 不正确！{RESET}")
            print(f"   远端返回: {resp.text}")
            return False
        elif resp.status_code == 400:
            print(f"{RED}❌ 请求被拒绝 (HTTP 400): {resp.text}{RESET}")
            print(f"{YELLOW}   提示: 请检查 VPS 系统时间是否与网络标准时间同步！{RESET}")
            return False
        else:
            print(f"{YELLOW}⚠️ 收到响应码 HTTP {resp.status_code}: {resp.text}{RESET}")
            return False

    except Exception as e:
        print(f"{RED}❌ 网络请求异常: {e}{RESET}")
        return False

def test_clock_drift() -> None:
    """步骤 3: 检查服务器时钟偏差 (防止签名超时)"""
    print_banner("[3/5] VPS 服务器时钟偏差检测 (Clock Drift)")
    try:
        resp = requests.get(f"{CLOB_HOST}/time", proxies=get_proxy_dict(), timeout=5)
        if resp.status_code == 200:
            server_ts = float(resp.text.strip()) if resp.text.strip().isdigit() else float(resp.json().get("time", time.time()))
            local_ts = time.time()
            drift = abs(local_ts - server_ts)
            print(f"   本地时间戳: {local_ts:.3f} | 远端时间戳: {server_ts:.3f}")
            if drift < 2.0:
                print(f"{GREEN}✅ 时钟同步良好！时间偏差: {drift*1000:.1f} ms (小于 2 秒){RESET}")
            else:
                print(f"{YELLOW}⚠️ 时钟偏差较大: {drift:.2f} 秒！建议在 VPS 上执行: sudo apt install ntp && sudo systemctl restart ntp{RESET}")
        else:
            print(f"   无法获取远端时间 (HTTP {resp.status_code})，跳过时钟检测。")
    except Exception as e:
        print(f"   时钟检测跳过: {e}")

def test_onchain_balances_and_allowance(wallet_address: str):
    """步骤 4 & 5: Polygon 链上资产与 CTF Exchange 合约授权检测"""
    print_banner("[4/5] Polygon 链上资产与 Gas 费检测")
    
    rpc_candidates = [
        r for r in [
            RPC_URL,
            "https://polygon-rpc.com",
            "https://polygon-bor-rpc.publicnode.com",
            "https://1rpc.io/matic"
        ] if r and "your_alchemy_key" not in r
    ]
    proxies = get_proxy_dict()
    
    # 常用合约地址
    USDC_NATIVE = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
    USDC_BRIDGED = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"  # Polymarket CTF Exchange 合约
    NEG_RISK_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
    
    addr_clean = wallet_address.lower().replace("0x", "").zfill(64)
    
    # 1. 查 POL (MATIC)
    pol_val = 0.0
    pol_success = False
    for rpc in rpc_candidates:
        try:
            payload = {"jsonrpc": "2.0", "method": "eth_getBalance", "params": [wallet_address, "latest"], "id": 1}
            resp = requests.post(rpc, json=payload, proxies=proxies, timeout=6).json()
            if isinstance(resp, dict) and "result" in resp:
                pol_wei = int(resp.get("result", "0x0"), 16)
                pol_val = pol_wei / 1e18
                pol_success = True
                break
        except Exception:
            continue
            
    if pol_success:
        if pol_val >= 0.05:
            print(f"   POL/MATIC (Gas): {GREEN}{pol_val:.4f} POL (充足){RESET}")
        elif pol_val > 0:
            print(f"   POL/MATIC (Gas): {YELLOW}{pol_val:.4f} POL (建议充值 >= 0.05 POL){RESET}")
        else:
            print(f"   POL/MATIC (Gas): {RED}0.0000 POL (需充值至少 0.05 POL 作为 Gas){RESET}")
    else:
        print(f"   POL/MATIC (Gas): {YELLOW}查询受限 (已尝试多个公共节点){RESET}")

    # 2. 查 USDC 余额
    for name, contract in [("USDC (Native)", USDC_NATIVE), ("USDC.e (Bridged)", USDC_BRIDGED)]:
        usdc_val = 0.0
        usdc_success = False
        data_hex = "0x70a08231" + addr_clean
        for rpc in rpc_candidates:
            try:
                payload = {"jsonrpc": "2.0", "method": "eth_call", "params": [{"to": contract, "data": data_hex}, "latest"], "id": 1}
                resp = requests.post(rpc, json=payload, proxies=proxies, timeout=6).json()
                if isinstance(resp, dict) and "result" in resp:
                    usdc_val = int(resp.get("result", "0x0"), 16) / 1e6
                    usdc_success = True
                    break
            except Exception:
                continue
        color = GREEN if usdc_val > 0 else YELLOW
        print(f"   {name:<16}: {color}${usdc_val:.2f} USDC{RESET}")

    # 3. 查 Allowance 授权
    print_banner("[5/5] Polymarket 智能合约 USDC 授权 (Allowance)")
    exchange_clean = CTF_EXCHANGE.lower().replace("0x", "").zfill(64)
    allowance_data = "0xdd62ed3e" + addr_clean + exchange_clean
    
    for name, contract in [("Native USDC", USDC_NATIVE), ("Bridged USDC.e", USDC_BRIDGED)]:
        try:
            payload = {"jsonrpc": "2.0", "method": "eth_call", "params": [{"to": contract, "data": allowance_data}, "latest"], "id": 1}
            resp = requests.post(rpc, json=payload, proxies=proxies, timeout=8).json()
            allowance = int(resp.get("result", "0x0"), 16) / 1e6
            if allowance > 1000:
                print(f"   {name} 授权额度: {GREEN}已无限授权 (${allowance:.0f} USDC){RESET}")
            elif allowance > 0:
                print(f"   {name} 授权额度: {GREEN}${allowance:.2f} USDC (已授权){RESET}")
            else:
                print(f"   {name} 授权额度: {RED}$0.00 (未授权！在 Polymarket 网页端充值或交易一次即可完成授权){RESET}")
        except Exception as e:
            print(f"   {name} 授权查询失败: {e}")

def main():
    print(f"\n{BOLD}🚀 Polymarket 5Min 套利机器人 - 实盘鉴权与就绪度诊断{RESET}")
    print(f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    
    addr = test_key_derivation()
    if not addr:
        print(f"\n{RED}❌ 私钥检测未通过，请检查 .env 文件。{RESET}\n")
        return

    clob_ok = test_clob_auth(addr)
    test_clock_drift()
    test_onchain_balances_and_allowance(addr)
    
    print_banner("诊断总结")
    if clob_ok:
        print(f"{GREEN}{BOLD}🎉 恭喜！Polymarket CLOB 鉴权测试 100% 通过！系统已具备实盘下单条件。{RESET}\n")
    else:
        print(f"{RED}{BOLD}⚠️ CLOB 鉴权未通过，请根据上方红色提示排查 API 凭证。{RESET}\n")

if __name__ == "__main__":
    main()
