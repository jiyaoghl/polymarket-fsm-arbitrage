#!/usr/bin/env python3
"""
Polymarket CLOB API 凭证一键生成/派生工具
原理：使用钱包私钥 (POLX_PK) 进行 EIP-712 签名，向 Polymarket CLOB 服务器申请/派生 L2 API Key, Secret 和 Passphrase。
"""

import os
import sys
import json
import time
import io
import requests

if sys.platform == "win32":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except Exception:
        pass

from eth_account import Account
from eth_account.messages import encode_typed_data

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, ".env"), override=True)
load_dotenv(os.path.join(PROJECT_ROOT, "configs", ".env"), override=True)

from polymarket.config import PK, CLOB_HOST, HTTP_PROXY, HTTPS_PROXY

# 颜色
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BOLD = "\033[1m"
RESET = "\033[0m"

def get_proxy_dict():
    proxy_url = HTTPS_PROXY or HTTP_PROXY or ""
    if proxy_url:
        return {"http": proxy_url, "https": proxy_url}
    return None

def derive_or_create_api_credentials():
    print(f"\n{BOLD}🔑 Polymarket CLOB API 凭证一键生成/派生{RESET}")
    print("=" * 60)

    if not PK or PK in ("your_private_key_here", "0x0000000000000000000000000000000000000000000000000000000000000000"):
        print(f"{RED}❌ 未检测到有效私钥！请先在 .env 中填入 POLX_PK=你的私钥{RESET}\n")
        return

    Account.enable_unaudited_hdwallet_features()
    clean_pk = PK if PK.startswith("0x") else f"0x{PK}"
    account = Account.from_key(clean_pk)
    wallet_address = account.address
    print(f"当前钱包地址: {BOLD}{wallet_address}{RESET}\n")

    # 方式 1: 尝试直接使用官方 py-clob-client 库 (如果已安装)
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.constants import POLYGON
        
        print("正在通过 py-clob-client 派生/创建 API 凭证...")
        client = ClobClient(
            host=CLOB_HOST,
            key=clean_pk,
            chain_id=137
        )
        creds = client.create_or_derive_api_creds()
        if creds:
            api_key = creds.api_key
            api_secret = creds.api_secret
            api_passphrase = creds.api_passphrase

            print(f"\n{GREEN}{BOLD}🎉 成功获取/派生 API 凭证！{RESET}")
            print("=" * 60)
            print(f"POLX_API_KEY={api_key}")
            print(f"POLX_API_SECRET={api_secret}")
            print(f"POLX_API_PASSPHRASE={api_passphrase}")
            print("=" * 60)

            # 自动更新本地 .env 与 configs/.env 文件
            save_to_env_files(api_key, api_secret, api_passphrase)
            return
    except ImportError:
        pass
    except Exception as e:
        print(f"{YELLOW}py-clob-client 派生异常 ({e})，切换为纯原生 EIP-712 请求...{RESET}")

    # 方式 2: 原生 EIP-712 签名请求 Polymarket /auth/derive-api-key 或 /auth/api-key
    timestamp = int(time.time())
    nonce = 0

    domain = {
        "name": "ClobAuthDomain",
        "version": "1",
        "chainId": 137,
    }
    
    types = {
        "ClobAuth": [
            {"name": "address", "type": "address"},
            {"name": "timestamp", "type": "string"},
            {"name": "nonce", "type": "uint256"},
            {"name": "message", "type": "string"},
        ]
    }

    message_data = {
        "address": wallet_address,
        "timestamp": str(timestamp),
        "nonce": nonce,
        "message": "This message attests that I control the given address and enables Polymarket API authentication."
    }

    structured_data = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
            ],
            **types
        },
        "domain": domain,
        "primaryType": "ClobAuth",
        "message": message_data
    }

    signable_message = encode_typed_data(full_message=structured_data)
    signed_message = Account.sign_message(signable_message, clean_pk)
    signature_hex = signed_message.signature.hex()
    if not signature_hex.startswith("0x"):
        signature_hex = "0x" + signature_hex

    headers = {
        "POLY_ADDRESS": wallet_address,
        "POLY_SIGNATURE": signature_hex,
        "POLY_TIMESTAMP": str(timestamp),
        "POLY_NONCE": str(nonce),
        "Content-Type": "application/json",
        "User-Agent": "Polymarket-Arbitrage-Bot/1.0"
    }

    # 尝试派生已有 API Key
    url_derive = f"{CLOB_HOST}/auth/derive-api-key"
    try:
        resp = requests.get(url_derive, headers=headers, proxies=get_proxy_dict(), timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            print(f"\n{GREEN}{BOLD}🎉 成功派生已有 API 凭证！{RESET}")
            print("=" * 60)
            print(f"{BOLD}请将以下内容复制并填入你的 .env 文件中：{RESET}\n")
            print(f"POLX_API_KEY={data.get('apiKey')}")
            print(f"POLX_API_SECRET={data.get('secret')}")
            print(f"POLX_API_PASSPHRASE={data.get('passphrase')}")
            print("=" * 60)
            return
    except Exception as e:
        print(f"派生已有 Key 请求异常: {e}")

    # 若未生成过，创建新 API Key
    url_create = f"{CLOB_HOST}/auth/api-key"
    try:
        resp = requests.post(url_create, headers=headers, json={}, proxies=get_proxy_dict(), timeout=10)
        if resp.status_code == 200 or resp.status_code == 201:
            data = resp.json()
            print(f"\n{GREEN}{BOLD}🎉 成功创建全新 API 凭证！{RESET}")
            print("=" * 60)
            print(f"{BOLD}请将以下内容复制并填入你的 .env 文件中：{RESET}\n")
            print(f"POLX_API_KEY={data.get('apiKey')}")
            print(f"POLX_API_SECRET={data.get('secret')}")
            save_to_env_files(data.get('apiKey'), data.get('secret'), data.get('passphrase'))
            return
        else:
            print(f"{RED}❌ 创建/派生 API Key 失败 (HTTP {resp.status_code}): {resp.text}{RESET}")
    except Exception as e:
        print(f"{RED}❌ 网络请求失败: {e}{RESET}")

def save_to_env_files(api_key: str, api_secret: str, passphrase: str):
    """自动将派生的 API 凭据写回本地 .env 文件"""
    env_paths = [
        os.path.join(PROJECT_ROOT, ".env"),
        os.path.join(PROJECT_ROOT, "configs", ".env")
    ]
    
    for p in env_paths:
        if not os.path.exists(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                lines = f.readlines()
            
            new_lines = []
            keys_written = {"POLX_API_KEY": False, "POLX_API_SECRET": False, "POLX_API_PASSPHRASE": False}
            
            for line in lines:
                stripped = line.strip()
                if stripped.startswith("POLX_API_KEY="):
                    new_lines.append(f"POLX_API_KEY={api_key}\n")
                    keys_written["POLX_API_KEY"] = True
                elif stripped.startswith("POLX_API_SECRET="):
                    new_lines.append(f"POLX_API_SECRET={api_secret}\n")
                    keys_written["POLX_API_SECRET"] = True
                elif stripped.startswith("POLX_API_PASSPHRASE="):
                    new_lines.append(f"POLX_API_PASSPHRASE={passphrase}\n")
                    keys_written["POLX_API_PASSPHRASE"] = True
                else:
                    new_lines.append(line)
            
            for k, val in [("POLX_API_KEY", api_key), ("POLX_API_SECRET", api_secret), ("POLX_API_PASSPHRASE", passphrase)]:
                if not keys_written[k]:
                    new_lines.append(f"{k}={val}\n")
            
            with open(p, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
            print(f"{GREEN}✓ 已自动同步更新至配置文件: {os.path.relpath(p, PROJECT_ROOT)}{RESET}")
        except Exception as e:
            print(f"{YELLOW}写入 {p} 失败: {e}{RESET}")

if __name__ == "__main__":
    derive_or_create_api_credentials()
