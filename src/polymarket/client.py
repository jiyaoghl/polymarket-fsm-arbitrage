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
    EIP712_DOMAIN_VERSION, EXCHANGE_CONTRACT_V2, NEG_RISK_EXCHANGE_CONTRACT_V2, COLLATERAL_TOKEN_NAME
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


def get_poly_signature(timestamp: int, method: str, request_path: str, body: str = "") -> str:
    """
    生成 Polymarket CLOB API 签名。
    
    签名格式：timestamp + method + requestPath + body
    使用 API_SECRET 进行 HMAC-SHA256 签名，然后 Base64 编码
    """
    if not API_SECRET:
        return ""
    
    message = str(timestamp) + method + request_path + body
    signature = hmac.new(
        API_SECRET.encode('utf-8'),
        message.encode('utf-8'),
        hashlib.sha256
    ).digest()
    
    import base64
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
            allowed_methods=["GET", "POST", "DELETE"],
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
        if PK:
            try:
                self.wallet = Account.from_key(PK)
                logger.info(f"钱包已加载：{self.wallet.address[:8]}...{self.wallet.address[-6:]}")
            except Exception as e:
                logger.error(f"加载钱包失败：{e}")
        
    def _get_auth_headers(self, method: str = "GET", request_path: str = "/", body: str = "") -> Dict[str, str]:
        """生成认证请求头。"""
        timestamp = int(time.time())
        signature = get_poly_signature(timestamp, method, request_path, body)
        
        headers = {
            "Content-Type": "application/json",
            "POLY_API_KEY": API_KEY or "",
            "POLY_PASSPHRASE": API_PASSPHRASE or "",
            "POLY_TIMESTAMP": str(timestamp),
            "POLY_SIGNATURE": signature,
        }
        return headers

    def _post_signed(self, endpoint: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """发送签名的 POST 请求。"""
        url = f"{self.host}{endpoint}"
        body = json.dumps(data, separators=(',', ':'))
        headers = self._get_auth_headers("POST", endpoint, body)
        
        response = self.session.post(url, data=body, headers=headers, timeout=10)
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
            return {
                "order_id": f"sim_{now_ms}",
                "status": "LIVE",
                "token_id": token_id,
                "side": side,
                "price": price,
                "amount": amount,
                "timestamp": now_ms,
                "metadata": zero_bytes32,
                "builder": zero_bytes32,
            }

        # 实盘模式：调用 CLOB V2 API
        try:
            # 构建 V2 订单数据
            order_data = {
                "token_id": token_id,
                "price": str(price),
                "size": str(amount),
                "side": side.upper(),
                "timestamp": now_ms,
                "metadata": zero_bytes32,
                "builder": zero_bytes32,
                "orderType": order_type.upper(),
            }
            
            result = self._post_signed("/order", order_data)
            logger.info(f"实盘 V2 下单成功：order_id={result.get('order_id', 'N/A')}")
            return result
            
        except Exception as e:
            logger.exception(f"实盘 V2 下单失败：{e}")
            return None

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
            batch_orders = []
            for i, o in enumerate(orders):
                batch_orders.append({
                    "token_id": o["token_id"],
                    "price": str(o["price"]),
                    "size": str(o["amount"]),
                    "side": o["side"].upper(),
                    "timestamp": now_ms + i,
                    "metadata": zero_bytes32,
                    "builder": zero_bytes32,
                    "orderType": "GTC",
                })
            
            payload = {"orders": batch_orders}
            result = self._post_signed("/batch-orders", payload)
            logger.info(f"批量 V2 下单成功：{len(result.get('orders', []))} 笔")
            return result
            
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
        """获取账户 USDC 余额。"""
        if not self.is_live:
            return {"usdc": 10000.0, "pending": 0.0}
        
        for attempt in range(3):
            try:
                # CLOB API 余额端点：GET /balance-allowance?asset_type=USDC&signature_type=0
                result = self._get_signed("/balance-allowance?asset_type=USDC&signature_type=0")
                # 响应格式: {"balance": "123.45", "allowance": "123.45"}
                balance = float(result.get("balance", 0))
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
            # 调用 CLOB redeem API
            result = self._post_signed("/redeem", {"condition_id": market_id})
            logger.info(f"Redeem 成功：market={market_id}, payout={result.get('payout', 0)}")
            return {
                "market_id": market_id,
                "status": "SUCCESS",
                "payout": float(result.get("payout", 0)),
            }
        except Exception as e:
            logger.exception(f"Redeem 失败：{e}")
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
    async def get_aio_session(self):
        import aiohttp
        if not hasattr(self, 'aio_session') or self.aio_session is None or self.aio_session.closed:
            proxy_url = None
            if hasattr(self, 'HTTP_PROXY') and self.HTTP_PROXY: proxy_url = self.HTTP_PROXY
            
            from polymarket.config import HTTP_PROXY, HTTPS_PROXY
            if HTTPS_PROXY: proxy_url = HTTPS_PROXY
            elif HTTP_PROXY: proxy_url = HTTP_PROXY
                
            connector = aiohttp.TCPConnector(ssl=False) if proxy_url else None
            self.aio_session = aiohttp.ClientSession(
                headers={'User-Agent': 'curl/8.13.0', 'Accept': 'application/json'},
                connector=connector,
                trust_env=False
            )
            self._aio_proxy = proxy_url
        return self.aio_session

    async def _post_signed_async(self, endpoint: str, data: Dict[str, Any]) -> Dict[str, Any]:
        session = await self.get_aio_session()
        url = f"{self.host}{endpoint}"
        body = json.dumps(data, separators=(',', ':'))
        headers = self._get_auth_headers('POST', endpoint, body)
        
        async with session.post(url, data=body, headers=headers, proxy=self._aio_proxy, timeout=10) as response:
            resp_text = await response.text()
            response.raise_for_status()
            return json.loads(resp_text)

    async def get_market_price_async(self, token_id: str) -> Optional[Dict[str, float]]:
        import asyncio
        import json
        session = await self.get_aio_session()
        for attempt in range(3):
            try:
                url = f"{self.host}/book?token_id={token_id}"
                async with session.get(url, proxy=self._aio_proxy, timeout=10, auto_decompress=False) as response:
                    response.raise_for_status()
                    data_bytes = await response.read()
                    encoding = response.headers.get("Content-Encoding", "").lower()
                    
                    if "br" in encoding:
                        try:
                            import brotli
                            data_bytes = brotli.decompress(data_bytes)
                        except ImportError:
                            pass # 未安装 brotli 时直接降级尝试解析明文
                        except Exception:
                            # 典型的中间人代理 Bug：代理已在底层透明解压，但忘了抹除 Header，这里直接吞异常容错
                            pass
                    elif "gzip" in encoding:
                        import gzip
                        try:
                            data_bytes = gzip.decompress(data_bytes)
                        except Exception:
                            pass
                            
                    data = json.loads(data_bytes)
                    asks = data.get('asks', []) or []
                    bids = data.get('bids', []) or []
                    best_ask = min((float(a['price']) for a in asks), default=1.0)
                    best_bid = max((float(b['price']) for b in bids), default=0.0)
                    return {'ask': best_ask, 'bid': best_bid}
            except Exception as e:
                logger.warning(f"[异步] 获取价格失败 token={token_id}: {e}")
                if attempt < 2: await asyncio.sleep(1.0 * (2 ** attempt))
        return None

    async def post_order_async(self, token_id: str, price: float, amount: float, side: str = 'BUY', order_type: str = 'GTC') -> Optional[Dict[str, Any]]:
        import asyncio
        mode_str = "实盘" if self.is_live else "模拟"
        logger.info(f"[{mode_str}] (异步) 下单：{side} {token_id} @ {price} x {amount} ({order_type})")
        now_ms = int(time.time() * 1000)
        zero_bytes32 = "0x0000000000000000000000000000000000000000000000000000000000000000"
        
        if not self.is_live:
            await asyncio.sleep(0.05)
            return {"order_id": f"sim_{now_ms}", "status": "LIVE", "token_id": token_id, "side": side, "price": price, "amount": amount, "timestamp": now_ms, "metadata": zero_bytes32, "builder": zero_bytes32}
            
        try:
            order_data = {"token_id": token_id, "price": str(price), "size": str(amount), "side": side.upper(), "timestamp": now_ms, "metadata": zero_bytes32, "builder": zero_bytes32, "orderType": order_type.upper()}
            result = await self._post_signed_async('/order', order_data)
            logger.info(f"[异步] V2 下单成功：order_id={result.get('order_id')}")
            return result
        except Exception as e:
            logger.exception(f"[异步] V2 下单失败：{e}")
            return None

# ================= 全局单例池 =================
_CLIENT_POOL: Dict[bool, PolyClient] = {}
_client_lock = threading.Lock()

def get_client(is_live: bool = False, rate_limit: float = 10.0) -> PolyClient:
    """获取单例 PolyClient 实例。基于 is_live 作为缓存键，实现全局连接池化。"""
    with _client_lock:
        if is_live not in _CLIENT_POOL:
            _CLIENT_POOL[is_live] = PolyClient(is_live=is_live, rate_limit=rate_limit)
        return _CLIENT_POOL[is_live]
