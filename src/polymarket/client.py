import os
import time
import json
import threading
import asyncio
from typing import Dict, List, Optional, Any, Tuple, Callable
import requests
from eth_account import Account

from polymarket.config import (
    PK, API_KEY, API_SECRET, API_PASSPHRASE,
    CLOB_HOST, GAMMA_HOST, HTTP_PROXY, HTTPS_PROXY,
    SIGNATURE_TYPE
)
from polymarket.logger import logger
from polymarket.gateway import (
    ITradingGateway, GatewayFactory, CLOBProtocolCodec
)

# ========= 兼容性工具函数 =========
class RateLimiter:
    """令牌桶限流器 (向后兼容)"""
    def __init__(self, rate: float = 10.0, period: float = 1.0):
        self.rate = rate
        self.period = period
        self.tokens = rate
        self.last_update = time.time()
        self._lock = threading.Lock()
    
    def acquire(self) -> None:
        with self._lock:
            now = time.time()
            elapsed = now - self.last_update
            self.tokens = min(self.rate, self.tokens + elapsed * (self.rate / self.period))
            self.last_update = now
            if self.tokens < 1:
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
    """重试装饰器 (向后兼容)"""
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
    """L2 HMAC-SHA256 签名生成 (向后兼容委托至 Codec)"""
    return CLOBProtocolCodec.get_poly_signature(timestamp, method, request_path, body, secret or "")


_client_instances: Dict[bool, Any] = {}
_client_lock = threading.Lock()

def get_client(is_live: bool = False, private_key: Optional[str] = None, **kwargs) -> "PolyClient":
    """获取 PolyClient 客户端单例 (向后兼容)"""
    global _client_instances
    with _client_lock:
        if is_live not in _client_instances:
            _client_instances[is_live] = PolyClient(private_key=private_key, is_live=is_live, **kwargs)
        return _client_instances[is_live]


class PolyClient:
    """
    Polymarket 交易客户端轻量级门面 (Facade Pattern)。
    
    内部全量委托给 ITradingGateway (LiveClobV2Gateway 或 PaperTradingGateway)。
    对现有所有调用者保持 100% 接口与属性向后兼容。
    """

    def __init__(
        self,
        private_key: Optional[str] = None,
        host: str = CLOB_HOST,
        gamma_host: str = GAMMA_HOST,
        rate_limit: float = 10.0,
        is_live: bool = False,
        warm_up: bool = True,
    ):
        self.host = host
        self.gamma_host = gamma_host
        self.is_live = is_live
        
        # 通过工厂创建对应的交易网关
        self._gateway: ITradingGateway = GatewayFactory.create_gateway(
            is_live=is_live,
            host=host,
            gamma_host=gamma_host,
            private_key=private_key or PK,
            warm_up=warm_up
        )

        # 显式暴露属性
        self.wallet = getattr(self._gateway, "wallet", None)
        self.session = getattr(self._gateway, "session", None)
        self.http2_client = getattr(self._gateway, "http2_client", None)

    def __getattr__(self, name: str) -> Any:
        """透明代理底层网关的所有未显式声明属性与方法"""
        return getattr(self._gateway, name)

    # ========= 核心下单接口 =========

    def post_order(
        self,
        token_id: str,
        price: float,
        amount: float,
        side: str = "BUY",
        order_type: str = "GTC",
    ) -> Optional[Dict[str, Any]]:
        return self._gateway.post_order(token_id, price, amount, side, order_type)

    async def post_order_async(
        self,
        token_id: str,
        price: float,
        amount: float,
        side: str = "BUY",
        order_type: str = "GTC",
    ) -> Optional[Dict[str, Any]]:
        return await self._gateway.post_order_async(token_id, price, amount, side, order_type)

    def post_batch_orders(self, orders: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        return self._gateway.post_batch_orders(orders)

    async def post_batch_orders_async(self, orders: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        return await self._gateway.post_batch_orders_async(orders)

    # ========= 撤单接口 =========

    def cancel_order(self, order_id: str) -> bool:
        return self._gateway.cancel_order(order_id)

    async def cancel_order_async(self, order_id: str) -> bool:
        return await self._gateway.cancel_order_async(order_id)

    def get_order_status(self, order_id: str) -> Optional[str]:
        return self._gateway.get_order_status(order_id)

    def wait_for_order_fill(self, order_id: str, timeout: float = 10.0) -> bool:
        return self._gateway.wait_for_order_fill(order_id, timeout)

    def _post_signed(self, endpoint: str, data: Dict[str, Any]) -> Dict[str, Any]:
        if hasattr(self._gateway, "_post_signed"):
            return self._gateway._post_signed(endpoint, data)
        return {"status": "SIMULATED"}

    def _get_signed(self, endpoint: str) -> Dict[str, Any]:
        if hasattr(self._gateway, "_get_signed"):
            return self._gateway._get_signed(endpoint)
        return {"status": "SIMULATED"}

    # ========= 资产与持仓 =========

    def get_balance(self) -> Dict[str, float]:
        return self._gateway.get_balance()

    def get_position(self, token_id: str) -> Dict[str, Any]:
        return self._gateway.get_position(token_id)

    def get_open_orders(self) -> List[Dict[str, Any]]:
        return self._gateway.get_open_orders()

    # ========= 行情与盘口 =========

    def get_market_price(self, token_id: str) -> Dict[str, float]:
        return self._gateway.get_market_price(token_id)

    async def get_market_price_async(self, token_id: str) -> Dict[str, float]:
        return await self._gateway.get_market_price_async(token_id)

    def get_orderbook(self, token_id: str) -> Dict[str, Any]:
        return self._gateway.get_orderbook(token_id)

    # ========= 结算与核对 =========

    def redeem(self, market_id: str) -> Dict[str, Any]:
        return self._gateway.redeem(market_id)

    async def redeem_async(self, market_id: str) -> Dict[str, Any]:
        return await self._gateway.redeem_async(market_id)

    def check_user_trade_filled(self, token_id: str, max_age_seconds: float = 30.0) -> bool:
        return self._gateway.check_user_trade_filled(token_id, max_age_seconds)

    def list_closed_markets(self) -> List[Dict[str, Any]]:
        return self._gateway.list_closed_markets()

    def close(self) -> None:
        self._gateway.close()

    def warm_up_connections(self, async_background: bool = True) -> None:
        if hasattr(self._gateway, "_warm_up_connections"):
            self._gateway._warm_up_connections(async_background=async_background)

    def _create_v2_signed_order(self, token_id: str, price: float, amount: float, side: str = "BUY", salt: Optional[int] = None) -> Dict[str, Any]:
        """向后兼容辅助方法"""
        if not self.wallet:
            raise ValueError("钱包未就绪")
        return CLOBProtocolCodec.create_v2_signed_order(self.wallet, token_id, price, amount, side, salt)

    # ========= 市场发现 =========
    @retry_on_failure(max_retries=3, base_delay=1.0, max_delay=10.0, exceptions=(requests.exceptions.RequestException, Exception))
    def discover_btc_5m_markets(self) -> List[Dict[str, Any]]:
        """基于时间戳 + Gamma API 发现最新 BTC 5 分钟市场"""
        try:
            now_ts = int(time.time())
            current_window = (now_ts // 300) * 300
            predict_ts_list = [current_window + 300, current_window]
            btc_5m_markets: List[Dict[str, Any]] = []

            for ts in predict_ts_list:
                slug = f"btc-updown-5m-{ts}"
                url = f"{self.gamma_host}/markets/slug/{slug}"
                
                resp = None
                if self.session:
                    try:
                        resp = self.session.get(url)
                    except Exception:
                        pass
                if resp is None or resp.status_code != 200:
                    resp = requests.get(url, timeout=5)

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
                expiry = 0.0
                if end_date_raw and isinstance(end_date_raw, str):
                    from datetime import datetime as _dt, timezone as _tz
                    try:
                        expiry = _dt.fromisoformat(end_date_raw.replace("Z", "+00:00")).timestamp()
                    except Exception:
                        pass

                btc_5m_markets.append({
                    "id": m.get("conditionId") or m.get("id"),
                    "question": m.get("question"),
                    "description": m.get("description"),
                    "slug": slug,
                    "tokens": {"YES": token_ids[0], "NO": token_ids[1]},
                    "expiry": expiry or float(ts + 300),
                    "rewards": m.get("rewards", {}),
                })
            return btc_5m_markets
        except Exception as e:
            logger.warning(f"discover_btc_5m_markets 异常: {e}")
            return []
