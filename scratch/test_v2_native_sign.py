import time
import random
from eth_account import Account
from eth_account.messages import encode_typed_data
from decimal import Decimal, ROUND_DOWN

pk = "0x0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
wallet = Account.from_key(pk)
exchange_contract = "0xE111180000d2663C0091e4f400237545B87B996B"

def build_v2_order(
    wallet: Account,
    token_id: str,
    price: float,
    size: float,
    side: str,
    signature_type: int = 0,
    funder: str = None,
    exchange_contract: str = exchange_contract,
    chain_id: int = 137,
):
    maker = funder if funder else wallet.address
    signer = wallet.address
    now_ms = int(time.time() * 1000)
    salt = random.randint(100000000, 999999999)
    zero_bytes32 = "0x0000000000000000000000000000000000000000000000000000000000000000"

    d_price = Decimal(str(price))
    d_size = Decimal(str(size))

    if side.upper() == "BUY":
        raw_maker = int((d_size * d_price).quantize(Decimal("0.000001"), rounding=ROUND_DOWN) * Decimal("1000000"))
        raw_taker = int(d_size.quantize(Decimal("0.000001"), rounding=ROUND_DOWN) * Decimal("1000000"))
        side_int = 0
    else:
        raw_maker = int(d_size.quantize(Decimal("0.000001"), rounding=ROUND_DOWN) * Decimal("1000000"))
        raw_taker = int((d_size * d_price).quantize(Decimal("0.000001"), rounding=ROUND_DOWN) * Decimal("1000000"))
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
            "chainId": chain_id,
            "verifyingContract": exchange_contract,
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
            "signatureType": int(signature_type),
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
        "signatureType": int(signature_type),
        "timestamp": str(now_ms),
        "metadata": zero_bytes32,
        "builder": zero_bytes32,
        "signature": signature_hex,
    }
    
    payload = {
        "order": order_dict,
        "owner": "test-owner-id",
        "orderType": "GTC",
        "deferExec": False,
        "postOnly": False
    }
    return payload

p = build_v2_order(wallet, "123456789", 0.45, 10.0, "BUY")
print("Payload schema validated:")
print(p)
