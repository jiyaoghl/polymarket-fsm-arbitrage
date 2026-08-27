import os
import time
import json
from typing import Dict, List, Optional, Any
from eth_account import Account

from polymarket.config import PK, RPC_URL, HTTP_PROXY, HTTPS_PROXY
from polymarket.logger import logger

# Polygon 主网官方合约与抵押代币常量
CTF_EXCHANGE_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_BRIDGED_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
USDC_NATIVE_ADDRESS = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"

# CTF 合约 redeemPositions 核心 ABI
CTF_REDEEM_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"}
        ],
        "name": "redeemPositions",
        "outputs": [],
        "payable": False,
        "stateMutability": "nonpayable",
        "type": "function"
    }
]

# 多候选公共 RPC 节点列表 (高可用轮询)
DEFAULT_RPC_CANDIDATES = [
    RPC_URL,
    "https://polygon-rpc.com",
    "https://1rpc.io/matic",
    "https://polygon.gateway.tenderly.co",
    "https://rpc.ankr.com/polygon"
]


class OnChainRedeemer:
    """
    Polymarket CTF 合约链上自动赎回引擎 (On-Chain CTF Auto-Redeemer)。
    
    核心功能：
    1. 原生调用 Polygon ConditionalTokens.redeemPositions 结算已结束市场并回流抵押品；
    2. 多 RPC 节点自动降级轮询 (Multi-RPC Fallback)；
    3. 支持离线纯 ABI 编码构建与零外部依赖测试；
    4. 自动处理二元市场 indexSets=[1, 2] 的标准交割。
    """

    def __init__(
        self,
        private_key: Optional[str] = None,
        rpc_candidates: Optional[List[str]] = None,
        ctf_address: str = CTF_EXCHANGE_ADDRESS,
        collateral_token: str = USDC_BRIDGED_ADDRESS
    ):
        self.private_key = private_key or PK
        self.wallet: Optional[Account] = None
        if self.private_key and not self.private_key.startswith("your_"):
            try:
                self.wallet = Account.from_key(self.private_key)
            except Exception as e:
                logger.warning(f"[OnChainRedeemer] 钱包初始化异常: {e}")

        raw_candidates = rpc_candidates or DEFAULT_RPC_CANDIDATES
        self.rpc_list = [r for r in raw_candidates if r and isinstance(r, str) and r.startswith("http")]
        if not self.rpc_list:
            self.rpc_list = ["https://polygon-rpc.com"]

        self.ctf_address = ctf_address
        self.collateral_token = collateral_token
        self._current_rpc_idx = 0

    def get_active_rpc(self) -> str:
        """获取当前活跃的 RPC 节点 URL"""
        return self.rpc_list[self._current_rpc_idx % len(self.rpc_list)]

    def rotate_rpc(self) -> str:
        """切换至下一个备选 RPC 节点"""
        self._current_rpc_idx = (self._current_rpc_idx + 1) % len(self.rpc_list)
        new_rpc = self.get_active_rpc()
        logger.info(f"[OnChainRedeemer] 切换至备选 RPC: {new_rpc}")
        return new_rpc

    @staticmethod
    def format_bytes32(hex_str: str) -> bytes:
        """格式化 0x 开头的 32 字节哈希值"""
        clean = hex_str.strip().lower()
        if clean.startswith("0x"):
            clean = clean[2:]
        clean = clean.zfill(64)
        return bytes.fromhex(clean)

    def encode_redeem_data(
        self,
        condition_id: str,
        collateral_token: Optional[str] = None,
        index_sets: Optional[List[int]] = None
    ) -> bytes:
        """
        纯内存构建 redeemPositions 的交易 Calldata。
        支持在无网络/离线环境下进行确定性单元测试验证。
        """
        try:
            from web3 import Web3
            w3 = Web3()
            contract = w3.eth.contract(
                address=Web3.to_checksum_address(self.ctf_address),
                abi=CTF_REDEEM_ABI
            )
            collat = Web3.to_checksum_address(collateral_token or self.collateral_token)
            cond_bytes = self.format_bytes32(condition_id)
            parent_col = b"\x00" * 32
            idx_sets = index_sets or [1, 2]

            tx_data = contract.encode_abi(
                "redeemPositions",
                args=[collat, parent_col, cond_bytes, idx_sets]
            )
            return bytes.fromhex(tx_data[2:] if tx_data.startswith("0x") else tx_data)
        except Exception as e:
            logger.error(f"[OnChainRedeemer] 编码 Calldata 失败: {e}")
            raise

    def redeem_positions(
        self,
        condition_id: str,
        collateral_token: Optional[str] = None,
        index_sets: Optional[List[int]] = None,
        max_retries: int = 3
    ) -> Dict[str, Any]:
        """
        向 Polygon 发起真实链上赎回交易 (具备多 RPC 故障转移机制)。
        """
        if not self.wallet:
            logger.warning(f"[OnChainRedeemer] 未配置有效钱包，跳过链上赎回: {condition_id}")
            return {"status": "SKIPPED", "reason": "No wallet configured"}

        collat = collateral_token or self.collateral_token
        idx_sets = index_sets or [1, 2]
        cond_bytes = self.format_bytes32(condition_id)
        parent_col = b"\x00" * 32

        last_error = None
        for attempt in range(max_retries):
            rpc_url = self.get_active_rpc()
            try:
                from web3 import Web3
                from web3.middleware import ExtraDataToPOAMiddleware

                w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
                try:
                    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
                except Exception:
                    pass

                if not w3.is_connected():
                    logger.warning(f"[OnChainRedeemer] RPC 无法连通 ({rpc_url})，轮换节点...")
                    self.rotate_rpc()
                    continue

                contract = w3.eth.contract(
                    address=Web3.to_checksum_address(self.ctf_address),
                    abi=CTF_REDEEM_ABI
                )

                account_address = self.wallet.address
                nonce = w3.eth.get_transaction_count(account_address)
                gas_price = w3.eth.gas_price

                safe_gas_price = max(int(gas_price * 1.25), 35_000_000_000)
                tx = contract.functions.redeemPositions(
                    Web3.to_checksum_address(collat),
                    parent_col,
                    cond_bytes,
                    idx_sets
                ).build_transaction({
                    "from": account_address,
                    "nonce": nonce,
                    "gas": 250000,
                    "gasPrice": safe_gas_price,
                    "chainId": 137,
                })

                signed_tx = self.wallet.sign_transaction(tx)
                raw_hash = getattr(signed_tx, "raw_transaction", None) or getattr(signed_tx, "rawTransaction", None)
                tx_hash = w3.eth.send_raw_transaction(raw_hash)
                tx_hex = tx_hash.hex()
                logger.info(f"[OnChainRedeemer] 链上赎回交易已广播: tx={tx_hex} (Market: {condition_id})")

                return {
                    "status": "SUCCESS",
                    "tx_hash": tx_hex,
                    "market_id": condition_id,
                    "rpc": rpc_url
                }

            except Exception as e:
                last_error = str(e)
                logger.warning(f"[OnChainRedeemer] 赎回失败 (Attempt {attempt+1}/{max_retries}, RPC: {rpc_url}): {e}")
                self.rotate_rpc()
                time.sleep(1.0)

        return {
            "status": "ERROR",
            "error": last_error or "All RPCs failed",
            "market_id": condition_id
        }
