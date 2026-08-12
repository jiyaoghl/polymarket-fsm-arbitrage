import sys
import os
import requests
import json
from web3 import Web3
from dotenv import load_dotenv

load_dotenv(r"d:\生活\Trading\polymarket\.env", override=True)
sys.path.insert(0, r"d:\生活\Trading\polymarket\src")

from polymarket.config import HTTP_PROXY
from polymarket.client import PolyClient

print("\n--- 深入排查余额差异 ---")
wallet_addr = "0x6F7FFC854218e1874a11B9dd5e660d547Dc69250"
print(f"排查地址: {wallet_addr}")

# 1. 检查 RPC 节点状态
print("\n[1] 检查 Polygon RPC 节点状态")
proxy_url = HTTP_PROXY if HTTP_PROXY else None
proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
rpc_url = "https://polygon-rpc.com"
w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"proxies": proxies} if proxies else {}))
if w3.is_connected():
    block = w3.eth.get_block('latest')
    print(f"RPC 已连接! 最新区块高度: {block['number']}")
else:
    print("RPC 连接失败!")

# 2. 检查链上代币原始值 (Raw balance)
print("\n[2] 链上代币原始值 (balanceOf)")
ERC20_ABI = json.loads('[{"constant":true,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"}]')
tokens = {
    "USDC.e": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
    "USDC": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
    "pUSD": "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
}
for name, address in tokens.items():
    try:
        contract = w3.eth.contract(address=w3.to_checksum_address(address), abi=ERC20_ABI)
        raw_balance = contract.functions.balanceOf(wallet_addr).call()
        print(f"{name} 原始余额值 (wei): {raw_balance}")
    except Exception as e:
        print(f"{name} 查询失败: {e}")

# 3. 检查 Polymarket CLOB API 余额
print("\n[3] 检查 Polymarket CLOB 接口余额")
try:
    client = PolyClient(is_live=True)
    res_usdc = client._get_signed("/balance-allowance?asset_type=USDC&signature_type=0")
    print(f"CLOB USDC (balance-allowance): {res_usdc}")
    res_collat = client._get_signed("/balance-allowance?asset_type=COLLATERAL&signature_type=0")
    print(f"CLOB COLLATERAL: {res_collat}")
except Exception as e:
    print(f"CLOB API 查询失败: {e}")
