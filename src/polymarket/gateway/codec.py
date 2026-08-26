import os
import time
import json
import hmac
import hashlib
import base64
from decimal import Decimal, ROUND_DOWN
from typing import Dict, Any, Tuple, Optional
from eth_account import Account
from eth_account.messages import encode_typed_data

from polymarket.config import (
    EXCHANGE_CONTRACT_V2, SIGNATURE_TYPE, FUNDER_ADDRESS
)
from polymarket.logger import logger

class CLOBProtocolCodec:
    """
    Polymarket CLOB V2 协议纯无状态编解码器 (Stateless Protocol Codec)。
    
    职责：
    1. 价格与数量强制安全钳制 (Price & Size Guardrails)；
    2. 纯原生 EIP-712 Typed Data 签名体构建；
    3. 标准 CLOB V2 Wire REST Payload 结构序列化；
    4. L2 HMAC-SHA256 请求头签名生成。
    """

    @staticmethod
    def sanitize_order_params(price: float, amount: float) -> Tuple[float, float]:
        """
        价格与数量安全钳制：
        - 价格钳制在 [0.001, 0.999] 区间，保留 4 位小数；
        - 底层硬性要求 Shares >= 5.0 份，保留 2 位小数；若传入 USDC 金额则自动折算为份数。
        """
        safe_price = round(min(max(float(price), 0.001), 0.999), 4)
        raw_amount = float(amount)
        if raw_amount < 5.0 and safe_price > 0:
            calc_shares = raw_amount / safe_price
            safe_size = round(max(calc_shares, 5.0), 2)
        else:
            safe_size = round(max(raw_amount, 5.0), 2)
        return safe_price, safe_size

    @staticmethod
    def get_poly_signature(timestamp: int, method: str, request_path: str, body: str = "", secret: str = "") -> str:
        """
        生成 Polymarket CLOB API 标准 L2 HMAC-SHA256 签名。
        签名格式：timestamp + method + requestPath + body
        """
        from polymarket import config
        api_sec = secret or config.API_SECRET or os.getenv("POLX_API_SECRET", "")
        if not api_sec:
            return ""
        
        message = f"{timestamp}{method.upper()}{request_path}"
        if body:
            message += body
            
        try:
            secret_bytes = base64.b64decode(api_sec)
        except Exception:
            secret_bytes = api_sec.encode('utf-8')

        signature = hmac.new(
            secret_bytes,
            message.encode('utf-8'),
            hashlib.sha256
        ).digest()
        return base64.b64encode(signature).decode('utf-8')

    @classmethod
    def create_v2_signed_order(
        cls,
        wallet: Account,
        token_id: str,
        price: float,
        amount: float,
        side: str = "BUY",
        salt: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        纯原生构建 Polymarket CLOB V2 EIP-712 签名订单字典。
        """
        now_ms = int(time.time() * 1000)
        salt = salt if salt is not None else (now_ms % 2147483647)
        maker = FUNDER_ADDRESS if (FUNDER_ADDRESS and not FUNDER_ADDRESS.startswith("your_")) else wallet.address
        signer = wallet.address
        zero_bytes32 = "0x0000000000000000000000000000000000000000000000000000000000000000"

        d_price = Decimal(str(price))
        d_size = Decimal(str(amount))

        if side.upper() == "BUY":
            maker_usdc = (d_size * d_price).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            taker_shares = d_size.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
            raw_maker = int(maker_usdc * Decimal("1000000"))
            raw_taker = int(taker_shares * Decimal("1000000"))
            side_int = 0
        else:
            maker_shares = d_size.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            taker_usdc = (d_size * d_price).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            raw_maker = int(maker_shares * Decimal("1000000"))
            raw_taker = int(taker_usdc * Decimal("1000000"))
            side_int = 1

        eip712_data = {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "Order": [
                    {"name": "salt", "type": "uint256"},
                    {"name": "maker", "type": "address"},
                    {"name": "signer", "type": "address"},
                    {"name": "tokenId", "type": "uint256"},
                    {"name": "makerAmount", "type": "uint256"},
                    {"name": "takerAmount", "type": "uint256"},
                    {"name": "side", "type": "uint8"},
                    {"name": "signatureType", "type": "uint8"},
                    {"name": "timestamp", "type": "uint256"},
                    {"name": "metadata", "type": "bytes32"},
                    {"name": "builder", "type": "bytes32"},
                ],
            },
            "domain": {
                "name": "Polymarket CTF Exchange",
                "version": "2",
                "chainId": 137,
                "verifyingContract": EXCHANGE_CONTRACT_V2,
            },
            "primaryType": "Order",
            "message": {
                "salt": salt,
                "maker": maker,
                "signer": signer,
                "tokenId": int(token_id),
                "makerAmount": raw_maker,
                "takerAmount": raw_taker,
                "side": side_int,
                "signatureType": SIGNATURE_TYPE,
                "timestamp": now_ms,
                "metadata": bytes.fromhex(zero_bytes32[2:]),
                "builder": bytes.fromhex(zero_bytes32[2:]),
            },
        }

        signable_message = encode_typed_data(full_message=eip712_data)
        signed = wallet.sign_message(signable_message)
        signature_hex = signed.signature.hex()
        if not signature_hex.startswith("0x"):
            signature_hex = "0x" + signature_hex

        order_dict = {
            "salt": int(salt),
            "maker": maker,
            "signer": signer,
            "tokenId": str(token_id),
            "makerAmount": str(raw_maker),
            "takerAmount": str(raw_taker),
            "side": side.upper(),
            "expiration": "0",
            "signatureType": int(SIGNATURE_TYPE),
            "timestamp": str(now_ms),
            "metadata": zero_bytes32,
            "builder": zero_bytes32,
            "signature": signature_hex,
        }
        return order_dict
