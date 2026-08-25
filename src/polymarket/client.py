import os
import requests
import time
import json
import hmac
import hashlib
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Tuple, Callable
from eth_account import Account
from eth_account.messages import encode_typed_data

from polymarket.config import (
    PK, API_KEY, API_SECRET, API_PASSPHRASE,
    CLOB_HOST, GAMMA_HOST, HTTP_PROXY, HTTPS_PROXY,
    EIP712_DOMAIN_VERSION, EXCHANGE_CONTRACT_V2, NEG_RISK_EXCHANGE_CONTRACT_V2, COLLATERAL_TOKEN_NAME,
    SIGNATURE_TYPE, FUNDER_ADDRESS
)
from polymarket.logger import logger


# ========= API 请求限流器 =========
class RateLimiter:
    """
    简单的令牌桶限流器。
    
    用于控制 API 请求频率，避免触发 429 错误。
    """
    
    def __init__(self, rate: float = 10.0, period: float = 1.0):
        """
        Args:
            rate: 时间窗口内允许的最大请求数
            period: 时间窗口（秒）
        """
        self.rate = rate
        self.period = period
        self.tokens = rate
        self.last_update = time.time()
        self._lock = threading.Lock()
    
    def acquire(self) -> None:
        """获取一个令牌，如果无可用令牌则等待。"""
        with self._lock:
            now = time.time()
            elapsed = now - self.last_update
            
            # 补充令牌
            self.tokens = min(self.rate, self.tokens + elapsed * (self.rate / self.period))
            self.last_update = now
            
            if self.tokens < 1:
                # 需要等待
                wait_time = (1 - self.tokens) * (self.period / self.rate)
                time.sleep(wait_time)
                self.tokens = 0
            else:
                self.tokens -= 1


def retry_on_failure(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 10.0,
    exceptions: Tuple = (Exception,),
):
    """
    重试装饰器。
    
    Args:
        max_retries: 最大重试次数
        base_delay: 基础延迟（秒）
        max_delay: 最大延迟（秒）
        exceptions: 需要重试的异常类型
    """
    def decorator(func: Callable) -> Callable:
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        delay = min(base_delay * (2 ** attempt), max_delay)
                        logger.warning(f"请求失败 (attempt {attempt + 1}/{max_retries}): {e}, {delay:.1f}s 后重试")
                        time.sleep(delay)
            raise last_exception
        return wrapper
    return decorator


def get_poly_signature(timestamp: int, method: str, request_path: str, body: str = "", secret: str = None) -> str:
    """
    生成 Polymarket CLOB API 标准 L2 HMAC-SHA256 签名。
    
    签名格式：timestamp + method + requestPath + body
    使用 base64 解码后的 API_SECRET 进行 HMAC-SHA256 计算，最后进行 base64 编码
    """
    import base64
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


class PolyClient:
    """
    统一封装 Gamma + CLOB REST API 的客户端。
    
    支持：
    - 模拟模式：返回模拟订单，用于测试
    - 实盘模式：使用官方 API 进行真实下单和结算
    """

    def __init__(self, is_live: bool = False, rate_limit: float = 10.0):
        # 针对 Windows 代理环境下 requests HTTPS 握手超时的绝杀：使用系统证书库
        try:
            import truststore
            truststore.inject_into_urllib3()
            logger.info("已注入 truststore (使用系统证书库)")
        except Exception:
            pass

        self.host = CLOB_HOST
        self.gamma_host = GAMMA_HOST
        self.is_live = is_live
        self.session = requests.Session()
        # 模拟浏览器/curl UA，防止被某些代理或 WAF 过滤
        self.session.headers.update({
            "User-Agent": "curl/8.13.0",
            "Accept": "application/json",
        })
        self.wallet = None

        proxies = {}
        if HTTP_PROXY:
            proxies["http"] = HTTP_PROXY
        if HTTPS_PROXY:
            proxies["https"] = HTTPS_PROXY
        if proxies:
            # 显式覆盖环境变量，确保 session 优先级最高
            self.session.proxies.update(proxies)
            self.session.trust_env = False 
            logger.info(f"已配置代理 (trust_env=False): {proxies}")

        from urllib3.util.retry import Retry
        from requests.adapters import HTTPAdapter
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "DELETE"],
        )
        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=20,
            pool_maxsize=20,
        )
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)


        self._rate_limiter = RateLimiter(rate=rate_limit, period=1.0)
        
        # 如果有私钥，初始化钱包用于签名
        if PK and not PK.startswith("your_"):
            try:
                self.wallet = Account.from_key(PK)
                logger.info(f"钱包已加载：{self.wallet.address[:8]}...{self.wallet.address[-6:]} (signature_type={SIGNATURE_TYPE})")
            except Exception as e:
                logger.warning(f"配置的钱包私钥格式有误，加载失败 (如仅运行模拟模式可忽略): {e}")
        
    def _get_auth_headers(self, method: str = "GET", request_path: str = "/", body: str = "") -> Dict[str, str]:
        """生成认证请求头。"""
        from polymarket import config
        api_key = config.API_KEY or os.getenv("POLX_API_KEY", "")
        passphrase = config.API_PASSPHRASE or os.getenv("POLX_API_PASSPHRASE", "")
        
        timestamp = int(time.time())
        signature = get_poly_signature(timestamp, method, request_path, body)
        
        headers = {
            "Content-Type": "application/json",
            "POLY_API_KEY": api_key,
            "POLY_PASSPHRASE": passphrase,
            "POLY_TIMESTAMP": str(timestamp),
            "POLY_SIGNATURE": signature,
        }
        if hasattr(self, "wallet") and self.wallet:
            headers["POLY_ADDRESS"] = self.wallet.address
            
        return headers

    def _post_signed(self, endpoint: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """发送签名的 POST 请求，内置 401 跨秒动态重签防线。"""
        url = f"{self.host}{endpoint}"
        body = json.dumps(data, separators=(',', ':'))
        
        headers = self._get_auth_headers("POST", endpoint, body)
        response = self.session.post(url, data=body, headers=headers, timeout=10)
        
        if response.status_code == 401:
            logger.warning(f"请求 {endpoint} 遭遇 401 鉴权拦截，疑似处于秒级跨秒临界区。触发动态重签自愈机制...")
            time.sleep(0.15)
            headers = self._get_auth_headers("POST", endpoint, body)
            response = self.session.post(url, data=body, headers=headers, timeout=10)
            if response.status_code == 401:
                logger.error(f"动态重签后依然 401。请检查真实的 API_KEY、API_SECRET 拼写及钱包地址。返回: {response.text}")
                
        response.raise_for_status()
        return response.json()

    def _get_signed(self, endpoint: str) -> Dict[str, Any]:
        """发送签名的 GET 请求。"""
        url = f"{self.host}{endpoint}"
        headers = self._get_auth_headers("GET", endpoint)
        
        response = self.session.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        return response.json()

    # ========= 市场发现 =========
    @retry_on_failure(max_retries=3, base_delay=1.0, max_delay=10.0, exceptions=(requests.exceptions.RequestException,))
    def discover_btc_5m_markets(self) -> List[Dict[str, Any]]:
        """
        基于时间戳 + Gamma API 精准发现最新 BTC 5 分钟 Up/Down 市场。
        """
        try:
            now_ts = int(time.time())
            current_window = (now_ts // 300) * 300
            predict_ts_list = [current_window + 300, current_window]

            btc_5m_markets: List[Dict[str, Any]] = []

            for ts in predict_ts_list:
                slug = f"btc-updown-5m-{ts}"
                url = f"{self.gamma_host}/markets/slug/{slug}"
                resp = self.session.get(url, timeout=5)
                if resp.status_code != 200:
                    continue

                m = resp.json()
                if not m.get("active") or m.get("closed"):
                    continue

                token_ids_str = m.get("clobTokenIds")
                if not token_ids_str:
                    continue
                token_ids = json.loads(token_ids_str)
                if len(token_ids) < 2:
                    continue

                end_date_raw = m.get("endDate")
                if end_date_raw and isinstance(end_date_raw, str):
                    from datetime import datetime as _dt, timezone as _tz
                    try:
                        expiry = _dt.fromisoformat(end_date_raw.replace("Z", "+00:00")).timestamp()
                    except Exception:
                        expiry = float(ts)
                else:
                    expiry = float(ts)

                btc_5m_markets.append(
                    {
                        "id": m["conditionId"],
                        "description": m.get("question") or slug,
                        "tokens": {
                            "YES": token_ids[0],
                            "NO": token_ids[1],
                        },
                        "expiry": expiry,
                    }
                )
                break

            if btc_5m_markets:
                logger.info(f"精准定位成功：{btc_5m_markets[0]['description']}")
            return btc_5m_markets
        except Exception as e:
            logger.exception(f"精准发现市场异常：{e}")
            return []

    def get_orderbook(self, token_id: str) -> Optional[Dict[str, Any]]:
        """获取指定 Token 的完整订单簿深度 (bids, asks)。"""
        for attempt in range(3):
            try:
                url = f"{self.host}/book?token_id={token_id}"
                response = self.session.get(url, timeout=10)
                response.raise_for_status()
                return response.json() or {}
            except Exception as e:
                if attempt == 2:
                    logger.warning(f"获取订单簿深度最终失败 token={token_id}: {e}")
                time.sleep(0.2)
        return None

    # ========= 行情/盘口 =========
    def get_market_price(self, token_id: str) -> Optional[Dict[str, float]]:
        """获取指定市场的买卖盘价格（最佳买一/卖一）。"""
        for attempt in range(3):
            try:
                url = f"{self.host}/book?token_id={token_id}"
                response = self.session.get(url, timeout=10)
                response.raise_for_status()
                data = response.json()

                asks = data.get("asks", []) or []
                bids = data.get("bids", []) or []

                best_ask = min((float(a["price"]) for a in asks), default=1.0)
                best_bid = max((float(b["price"]) for b in bids), default=0.0)

                return {"ask": best_ask, "bid": best_bid}
            except requests.exceptions.RequestException as e:
                if getattr(e.response, "status_code", 0) == 404:
                    # 404 意味着盘口已经彻底不存在或市场被关闭，直接返回
                    return None
                logger.warning(f"获取价格失败 token={token_id} (重试 {attempt + 1}/3): {e}")
                if attempt < 2:
                    time.sleep(1.0 * (2 ** attempt))
            except Exception as e:
                logger.error(f"获取价格发生意外错误 token={token_id}: {e}")
                return None
        logger.error(f"获取价格最终失败 token={token_id}")
        return None

    def _create_v2_signed_order(
        self,
        token_id: str,
        price: float,
        amount: float,
        side: str,
    ) -> Dict[str, Any]:
        """
        纯原生离线构建 Polymarket CLOB V2 EIP-712 签名订单结构体。
        严格符合 CLOB V2 (Domain Version 2) 规范，彻底消除 V1 旧字段 (nonce/feeRateBps/taker/expiration)。
        """
        import random
        from decimal import Decimal, ROUND_DOWN
        from eth_account.messages import encode_typed_data
        from polymarket.config import SIGNATURE_TYPE, FUNDER_ADDRESS, EXCHANGE_CONTRACT_V2

        maker = FUNDER_ADDRESS if FUNDER_ADDRESS else self.wallet.address
        signer = self.wallet.address
        now_ms = int(time.time() * 1000)
        salt = random.randint(100000000, 999999999)
        zero_bytes32 = "0x0000000000000000000000000000000000000000000000000000000000000000"

        d_price = Decimal(str(price))
        d_size = Decimal(str(amount))

        if side.upper() == "BUY":
            # 买入：花 pUSD (makerAmount，最大 2 位精度/分) 买入 shares (takerAmount，最大 4 位精度)
            maker_usdc = (d_size * d_price).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            taker_shares = d_size.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
            raw_maker = int(maker_usdc * Decimal("1000000"))
            raw_taker = int(taker_shares * Decimal("1000000"))
            side_int = 0
        else:
            # 卖出：卖出 shares (makerAmount，最大 2 位精度) 换取 pUSD (takerAmount，最大 2 位精度)
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
        signed = self.wallet.sign_message(signable_message)
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

    # ========= 下单接口 =========
    def post_order(
        self,
        token_id: str,
        price: float,
        amount: float,
        side: str = "BUY",
        order_type: str = "GTC",
    ) -> Optional[Dict[str, Any]]:
        """
        单笔下单接口。

        - 模拟模式：返回模拟订单
        - 实盘模式：调用 CLOB API 下单
        """
        mode_str = "实盘" if self.is_live else "模拟"
        logger.info(f"[{mode_str}] (CLOB V2) 执行下单：{side} {token_id} @ {price} x {amount} ({order_type})")

        now_ms = int(time.time() * 1000)
        zero_bytes32 = "0x0000000000000000000000000000000000000000000000000000000000000000"

        if not self.is_live:
            # [改进] 模拟模式：引入真实的网络延迟和价格滑点
            import random
            from polymarket.config import SIM_LATENCY_MIN_MS, SIM_LATENCY_MAX_MS, SIM_SLIPPAGE_MAX
            latency_ms = random.randint(SIM_LATENCY_MIN_MS, SIM_LATENCY_MAX_MS)
            time.sleep(latency_ms / 1000.0)
            slippage = round(random.uniform(0, SIM_SLIPPAGE_MAX), 4)
            sim_price = round(price + slippage, 4) if side.upper() == "BUY" else round(price - slippage, 4)
            return {
                "order_id": f"sim_{now_ms}",
                "status": "LIVE",
                "token_id": token_id,
                "side": side,
                "price": sim_price,
                "amount": amount,
                "timestamp": now_ms,
                "metadata": zero_bytes32,
                "builder": zero_bytes32,
            }

        # 实盘模式：调用 CLOB V2 API
        try:
            if amount <= 0:
                logger.error(f"[实盘] 下单金额/数量非法: amount={amount}")
                return None

            if not self.wallet:
                logger.error("[实盘] 钱包未就绪，请检查是否配置了有效私钥 POLX_PK")
                return None

            safe_price = round(min(max(float(price), 0.001), 0.999), 4)
            raw_size = float(amount)
            # CLOB 要求最小份数 >= 5.0 Shares；若传入为 USDC 金额，则折算为份数
            if raw_size < 5.0 and safe_price > 0:
                calc_shares = raw_size / safe_price
                safe_size = round(max(calc_shares, 5.0), 2)
            else:
                safe_size = round(max(raw_size, 5.0), 2)

            # 纯原生构建 V2 EIP-712 签名订单（0 外部网络延迟）
            signed_order = self._create_v2_signed_order(
                token_id=str(token_id),
                price=safe_price,
                amount=safe_size,
                side=side.upper(),
            )

            # 转换为 CLOB V2 标准 Payload
            from polymarket import config
            api_key = config.API_KEY or os.getenv("POLX_API_KEY", "")
            payload = {
                "order": signed_order,
                "owner": api_key,
                "orderType": order_type.upper(),
                "deferExec": False,
                "postOnly": False,
            }

            result = self._post_signed("/order", payload)
            order_id = result.get("order_id") or result.get("orderID") or result.get("id") or "N/A"
            logger.info(f"实盘 V2 下单成功：order_id={order_id}")
            return result
            
        except requests.exceptions.HTTPError as he:
            err_body = he.response.text if hasattr(he, "response") and he.response is not None else ""
            logger.error(f"实盘 V2 下单 HTTP 异常 ({he}): {err_body}")
            
            # 【P0 防御】尝试从撮合层异常响应中提取 orderID，防止幻象失败造成单边敞口泄漏
            extracted_order_id = None
            if err_body:
                try:
                    err_json = json.loads(err_body)
                    if isinstance(err_json, dict):
                        extracted_order_id = err_json.get("orderID") or err_json.get("order_id") or err_json.get("id")
                except Exception:
                    pass
            if extracted_order_id:
                logger.warning(f"[实盘防御] 下单异常但撮合引擎已生成 orderID={extracted_order_id}，转入 UNCONFIRMED 链上核查链路！")
                return {
                    "order_id": extracted_order_id,
                    "status": "UNCONFIRMED",
                    "error": err_body or str(he),
                    "token_id": token_id,
                    "side": side,
                    "price": safe_price,
                    "amount": safe_size,
                }
            return None
        except Exception as e:
            logger.exception(f"实盘 V2 下单失败：{e}")
            return None

    def check_user_trade_filled(self, token_id: str, max_age_seconds: float = 30.0) -> bool:
        """
        [P0 终极防线] 通过 Polymarket Data API 直接查证当前钱包在近期是否确实成交了指定 Token。
        用于在 CLOB REST API 查单超时或报 401 时的终极对账确认。
        """
        if not self.wallet:
            return False
        try:
            url = f"https://data-api.polymarket.com/trades?user={self.wallet.address.lower()}&limit=10"
            resp = self.session.get(url, timeout=5)
            if resp.status_code == 200:
                trades = resp.json()
                if isinstance(trades, list):
                    now_ts = time.time()
                    for t in trades:
                        t_asset = str(t.get("asset") or "")
                        if t_asset == str(token_id):
                            t_ts = t.get("timestamp") or t.get("created_at") or 0
                            if isinstance(t_ts, (int, float)):
                                if t_ts > 1e11:
                                    t_ts = t_ts / 1000.0
                                if (now_ts - t_ts) <= max_age_seconds:
                                    logger.info(f"[终极防线] 成功通过 Data API 确认到 token={token_id} 链上真实成交！")
                                    return True
        except Exception as e:
            logger.warning(f"[终极防线] Data API 查证成交异常: {e}")
        return False

    def post_batch_orders(self, orders: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """
        CLOB V2 批量下单（原子操作）。
        """
        mode_str = "实盘" if self.is_live else "模拟"
        logger.info(f"[{mode_str}] (CLOB V2) 执行批量下单 {len(orders)} 笔")

        now_ms = int(time.time() * 1000)
        zero_bytes32 = "0x0000000000000000000000000000000000000000000000000000000000000000"

        if not self.is_live:
            return {
                "status": "SIMULATED",
                "orders": [
                    {
                        **o,
                        "order_id": f"sim_batch_{i}_{now_ms}",
                        "status": "LIVE",
                        "timestamp": now_ms + i,
                        "metadata": zero_bytes32,
                        "builder": zero_bytes32,
                    }
                    for i, o in enumerate(orders)
                ],
            }

        # 实盘模式：调用 CLOB V2 批量下单 API
        try:
            if not self.wallet:
                logger.error("[实盘] 钱包未就绪，请检查是否配置了有效私钥 POLX_PK")
                return {"status": "ERROR", "error": "Wallet not ready"}

            from polymarket import config
            api_key = config.API_KEY or os.getenv("POLX_API_KEY", "")
            
            batch_orders = []
            for o in orders:
                safe_price = round(min(max(float(o["price"]), 0.001), 0.999), 4)
                safe_size = round(float(o["amount"]), 2)
                order_type_val = str(o.get("order_type", "GTC")).upper()

                signed_order = self._create_v2_signed_order(
                    token_id=str(o["token_id"]),
                    price=safe_price,
                    amount=safe_size,
                    side=str(o["side"]).upper(),
                )
                batch_orders.append({
                    "order": signed_order,
                    "owner": api_key,
                    "orderType": order_type_val,
                    "deferExec": False,
                    "postOnly": False,
                })

            payload = {"orders": batch_orders}
            result = self._post_signed("/batch-orders", payload)
            logger.info(f"批量 V2 下单成功：{len(result.get('orders', []))} 笔")
            return result
            
        except requests.exceptions.HTTPError as he:
            err_body = he.response.text if hasattr(he, "response") and he.response is not None else ""
            logger.error(f"批量 V2 下单 HTTP 异常 ({he}): {err_body}")
            return {"status": "ERROR", "error": err_body or str(he)}
        except Exception as e:
            logger.exception(f"批量 V2 下单失败：{e}")
            return {"status": "ERROR", "error": str(e)}

    # ========= 订单/资金 =============
    def get_open_orders(self) -> List[Dict[str, Any]]:
        """获取当前挂单。"""
        if not self.is_live:
            return []
        
        try:
            result = self._get_signed("/orders")
            return result.get("orders", [])
        except Exception as e:
            logger.error(f"获取挂单失败：{e}")
            return []

    def cancel_order(self, order_id: str) -> bool:
        """撤销订单。"""
        if not self.is_live:
            logger.info(f"取消订单 (模拟): {order_id}")
            return True
            
        try:
            endpoint = f"/order/{order_id}"
            headers = self._get_auth_headers("DELETE", endpoint)
            url = f"{self.host}{endpoint}"
            response = self.session.delete(url, headers=headers, timeout=10)
            response.raise_for_status()
            logger.info(f"取消订单成功：{order_id}")
            return True
        except Exception as e:
            logger.exception(f"取消订单失败：{e}")
            return False

    def get_balance(self) -> Dict[str, float]:
        """获取账户 USDC / pUSD 抵押品可用余额。"""
        if not self.is_live:
            from polymarket import config
            paper_cap = getattr(config, "PAPER_INITIAL_CAPITAL", 100.0)
            return {"usdc": float(paper_cap), "pending": 0.0}
        
        # 优先使用官方 py_clob_client
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType, ApiCreds
            from polymarket import config
            api_key = config.API_KEY or os.getenv("POLX_API_KEY", "")
            api_sec = config.API_SECRET or os.getenv("POLX_API_SECRET", "")
            api_pass = config.API_PASSPHRASE or os.getenv("POLX_API_PASSPHRASE", "")
            if config.PK:
                clean_pk = config.PK if config.PK.startswith("0x") else f"0x{config.PK}"
                c = ClobClient(host=self.host, key=clean_pk, chain_id=137)
                if api_key and api_sec and api_pass:
                    c.set_api_creds(ApiCreds(api_key=api_key, api_secret=api_sec, api_passphrase=api_pass))
                else:
                    c.set_api_creds(c.derive_api_key())
                
                res = c.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
                raw_bal = float(res.get("balance", 0.0))
                balance = raw_bal / 1e6 if raw_bal > 1000 else raw_bal
                return {"usdc": balance, "pending": 0.0}
        except Exception:
            pass

        for attempt in range(3):
            try:
                result = self._get_signed("/balance-allowance?asset_type=COLLATERAL")
                raw_bal = float(result.get("balance", 0))
                balance = raw_bal / 1e6 if raw_bal > 1000 else raw_bal
                return {
                    "usdc": balance,
                    "pending": 0.0,
                }
            except requests.exceptions.RequestException as e:
                logger.warning(f"获取余额失败 (重试 {attempt + 1}/3): {e}")
                if attempt < 2:
                    time.sleep(1.0 * (2 ** attempt))
            except Exception as e:
                logger.error(f"获取余额最终失败：{e}")
                break
        return {"usdc": 0.0, "pending": 0.0}


    # ========= 结算 / 领奖 =========
    @retry_on_failure(max_retries=3, base_delay=1.0, max_delay=10.0, exceptions=(requests.exceptions.RequestException,))
    def list_closed_markets(self) -> List[Dict[str, Any]]:
        """
        从 Gamma API 获取已关闭 (可 redeem) 市场的基本信息。
        """
        try:
            url = f"{self.gamma_host}/markets?closed=true&limit=50"
            resp = self.session.get(url, timeout=5)
            resp.raise_for_status()
            markets = resp.json() or []
            return markets
        except Exception as e:
            logger.exception(f"获取已关闭市场失败：{e}")
            return []

    def redeem(self, market_id: str) -> Dict[str, Any]:
        """
        领奖接口：调用 CLOB API 执行 redeem/payout。
        """
        mode_str = "实盘" if self.is_live else "模拟"
        if self.is_live:
            logger.info(f"[{mode_str}] 执行 redeem 市场：{market_id}")

        if not self.is_live:
            return {
                "market_id": market_id,
                "status": "SIMULATED",
                "payout": 0.0,
            }

        try:
            # 尝试调用 CLOB redeem API
            result = self._post_signed("/redeem", {"condition_id": market_id})
            logger.info(f"Redeem 成功：market={market_id}, payout={result.get('payout', 0)}")
            return {
                "market_id": market_id,
                "status": "SUCCESS",
                "payout": float(result.get("payout", 0)),
            }
        except requests.exceptions.HTTPError as he:
            if hasattr(he, "response") and he.response is not None and he.response.status_code == 404:
                logger.info(f"市场 {market_id} 结算完成（CLOB REST 无需额外提取，可在网页端一键 Claim）")
                return {"market_id": market_id, "status": "SETTLED", "payout": 0.0}
            logger.warning(f"Redeem 请求异常：{he}")
            return {"market_id": market_id, "status": "ERROR", "error": str(he), "payout": 0.0}
        except Exception as e:
            logger.warning(f"Redeem 失败：{e}")
            return {
                "market_id": market_id,
                "status": "ERROR",
                "error": str(e),
                "payout": 0.0,
            }

    def get_position(self, token_id: str) -> Dict[str, Any]:
        """获取指定 token 的持仓。"""
        if not self.is_live:
            return {"position": 0.0, "pnl": 0.0}
        
        try:
            result = self._get_signed(f"/position/{token_id}")
            return {
                "position": float(result.get("position", 0)),
                "pnl": float(result.get("pnl", 0)),
            }
        except Exception as e:
            logger.error(f"获取持仓失败：{e}")
            return {"position": 0.0, "pnl": 0.0}

    # ========= 订单状态查询 =========
    def get_order_status(self, order_id: str) -> Optional[str]:
        """
        查询订单状态。
        
        Args:
            order_id: 订单 ID
            
        Returns:
            订单状态字符串：LIVE, FILLED, CANCELLED, EXPIRED, REJECTED 等
            查询失败返回 None
        """
        if not self.is_live:
            # 模拟模式假设订单已成交
            return "FILLED"
        
        try:
            self._rate_limiter.acquire()
            result = self._get_signed(f"/order/{order_id}")
            return result.get("status", "UNKNOWN")
        except Exception as e:
            logger.error(f"查询订单状态失败 order_id={order_id}: {e}")
            return None

    def get_order_details(self, order_id: str) -> Optional[Dict[str, Any]]:
        """
        查询订单详情。
        
        Args:
            order_id: 订单 ID
            
        Returns:
            订单详情字典，查询失败返回 None
        """
        if not self.is_live:
            return {
                "order_id": order_id,
                "status": "FILLED",
                "filled_size": 0.0,
                "price": 0.0,
            }
        
        try:
            self._rate_limiter.acquire()
            result = self._get_signed(f"/order/{order_id}")
            return result
        except Exception as e:
            logger.error(f"查询订单详情失败 order_id={order_id}: {e}")
            return None

    def wait_for_order_fill(
        self,
        order_id: str,
        timeout: float = 30.0,
        interval: float = 0.5,
    ) -> bool:
        """
        等待订单成交。
        
        Args:
            order_id: 订单 ID
            timeout: 超时时间（秒）
            interval: 轮询间隔（秒）
            
        Returns:
            True 如果订单在超时前成交，False 否则
        """
        start_time = time.time()
        while time.time() - start_time < timeout:
            status = self.get_order_status(order_id)
            if status == "FILLED":
                return True
            elif status in ("CANCELLED", "EXPIRED", "REJECTED"):
                logger.warning(f"订单 {order_id} 状态异常: {status}")
                return False
            time.sleep(interval)
        
        logger.warning(f"等待订单 {order_id} 成交超时")
        return False

    # ========= 异步方法扩展 =========
    async def get_market_price_async(self, token_id: str) -> Optional[Dict[str, float]]:
        """
        异步获取市场价格（通过线程池调度同步方法，规避跨事件循环 Session 崩溃并复用 HTTP 连接池）。
        """
        import asyncio
        return await asyncio.to_thread(self.get_market_price, token_id)

    async def post_order_async(self, token_id: str, price: float, amount: float, side: str = 'BUY', order_type: str = 'GTC') -> Optional[Dict[str, Any]]:
        """
        异步下单（通过线程池调度同步方法，享受连接池复用与绝对线程安全）。
        """
        import asyncio
        if not self.is_live:
            # 模拟模式：引入真实的网络延迟和价格滑点
            import random
            from polymarket.config import SIM_LATENCY_MIN_MS, SIM_LATENCY_MAX_MS, SIM_SLIPPAGE_MAX
            latency_ms = random.randint(SIM_LATENCY_MIN_MS, SIM_LATENCY_MAX_MS)
            await asyncio.sleep(latency_ms / 1000.0)
            now_ms = int(time.time() * 1000)
            zero_bytes32 = "0x0000000000000000000000000000000000000000000000000000000000000000"
            slippage = round(random.uniform(0, SIM_SLIPPAGE_MAX), 4)
            sim_price = round(price + slippage, 4) if side.upper() == "BUY" else round(price - slippage, 4)
            return {"order_id": f"sim_{now_ms}", "status": "LIVE", "token_id": token_id, "side": side, "price": sim_price, "amount": amount, "timestamp": now_ms, "metadata": zero_bytes32, "builder": zero_bytes32}

        return await asyncio.to_thread(self.post_order, token_id, price, amount, side, order_type)

    async def post_batch_orders_async(self, orders: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """
        异步批量下单（调度同步 post_batch_orders，支持模拟与实盘模式）。
        """
        import asyncio
        return await asyncio.to_thread(self.post_batch_orders, orders)

# ================= 全局单例池 =================
_CLIENT_POOL: Dict[bool, PolyClient] = {}
_client_lock = threading.Lock()

def get_client(is_live: bool = False, rate_limit: float = 10.0) -> PolyClient:
    """获取单例 PolyClient 实例。基于 is_live 作为缓存键，实现全局连接池化。"""
    with _client_lock:
        if is_live not in _CLIENT_POOL:
            _CLIENT_POOL[is_live] = PolyClient(is_live=is_live, rate_limit=rate_limit)
        return _CLIENT_POOL[is_live]
