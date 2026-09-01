import time
import random
import threading
import json
import httpx
from typing import Dict, List, Optional, Any

from polymarket.gateway.base import ITradingGateway
from polymarket.gateway.codec import CLOBProtocolCodec
from polymarket.config import (
    CLOB_HOST, GAMMA_HOST, SIM_LATENCY_MIN_MS, SIM_LATENCY_MAX_MS, SIM_SLIPPAGE_MAX,
    HTTPS_PROXY, HTTP_PROXY
)
from polymarket.logger import logger
from polymarket.metrics import metrics

class SimulatedOrderBookLedger:
    """
    轻量级模拟订单账本 (Simulated Order Book Ledger)。
    在内存中真实跟踪模拟挂单的存活与撤销生命周期。
    """
    def __init__(self):
        self._orders: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def add_order(self, order: Dict[str, Any]) -> None:
        with self._lock:
            order_id = str(order.get("order_id") or "")
            if order_id:
                self._orders[order_id] = order

    def remove_order(self, order_id: str) -> bool:
        with self._lock:
            return self._orders.pop(str(order_id), None) is not None

    def get_open_orders(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._orders.values())


class PaperTradingGateway(ITradingGateway):
    """
    高保真模拟交易网关 (Paper Trading Gateway)。
    
    核心特性：
    1. 真实网络延迟注入 (Latency Injection)；
    2. 市场价格滑点仿真 (Slippage Simulation)；
    3. 模拟订单账本生命周期管理；
    4. HTTP/2 客户端兼容与纯内存零网络风险。
    """

    def __init__(
        self,
        host: str = CLOB_HOST,
        gamma_host: str = GAMMA_HOST,
        initial_balance: Optional[float] = None
    ):
        self._host = host
        self._gamma_host = gamma_host
        self._custom_balance = float(initial_balance) if initial_balance is not None else None
        self._ledger = SimulatedOrderBookLedger()
        self.wallet = None

        # 初始化兼容性 HTTP/2 客户端
        proxy_url = HTTPS_PROXY or HTTP_PROXY
        self.http2_client = httpx.Client(
            http2=True,
            timeout=10.0,
            proxy=proxy_url if proxy_url else None,
            verify=True,
            trust_env=False if proxy_url else True
        )
        self.session = self.http2_client
        self._http_lock = threading.RLock()
        logger.info("[PaperGateway] 模拟交易网关初始化完成。")

    @property
    def is_live(self) -> bool:
        return False

    @property
    def host(self) -> str:
        return self._host

    @property
    def gamma_host(self) -> str:
        return self._gamma_host

    def _warm_up_connections(self, async_background: bool = True) -> None:
        """预热连接"""
        def _do():
            try:
                self.http2_client.get(f"{self.host}/time")
                self.http2_client.get(f"{self.gamma_host}/markets?limit=1")
            except Exception:
                pass
        if async_background:
            threading.Thread(target=_do, daemon=True).start()
        else:
            _do()

    def _get_auth_headers(self, method: str = "GET", request_path: str = "/", body: str = "") -> Dict[str, str]:
        timestamp = int(time.time())
        signature = CLOBProtocolCodec.get_poly_signature(timestamp, method, request_path, body) or "sim_poly_signature"
        return {
            "Content-Type": "application/json",
            "POLY_TIMESTAMP": str(timestamp),
            "POLY_SIGNATURE": signature,
        }

    def _post_signed(self, endpoint: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """模拟模式兼容的 POST 签名辅助方法"""
        url = f"{self.host}{endpoint}"
        body = json.dumps(data, separators=(',', ':'))
        headers = self._get_auth_headers("POST", endpoint, body)
        with self._http_lock:
            resp = self.http2_client.post(url, content=body, headers=headers)
        return resp.json()

    def _get_signed(self, endpoint: str) -> Dict[str, Any]:
        """模拟模式兼容的 GET 签名辅助方法"""
        url = f"{self.host}{endpoint}"
        headers = self._get_auth_headers("GET", endpoint)
        with self._http_lock:
            resp = self.http2_client.get(url, headers=headers)
        return resp.json()

    def _inject_latency_and_slippage(self, price: float, side: str) -> float:
        """注入同步延迟与滑点 (用于同步接口)"""
        latency_ms = random.randint(SIM_LATENCY_MIN_MS, SIM_LATENCY_MAX_MS)
        time.sleep(latency_ms / 1000.0)
        slippage = round(random.uniform(0, SIM_SLIPPAGE_MAX), 4)
        sim_price = round(price + slippage, 4) if side.upper() == "BUY" else round(price - slippage, 4)
        return sim_price

    async def _inject_latency_and_slippage_async(self, price: float, side: str) -> float:
        """注入异步非阻塞延迟与滑点 (用于 async 协程，零阻塞 EventLoop)"""
        latency_ms = random.randint(SIM_LATENCY_MIN_MS, SIM_LATENCY_MAX_MS)
        await asyncio.sleep(latency_ms / 1000.0)
        slippage = round(random.uniform(0, SIM_SLIPPAGE_MAX), 4)
        sim_price = round(price + slippage, 4) if side.upper() == "BUY" else round(price - slippage, 4)
        return sim_price

    def post_order(
        self,
        token_id: str,
        price: float,
        amount: float,
        side: str = "BUY",
        order_type: str = "GTC",
    ) -> Optional[Dict[str, Any]]:
        now_ms = int(time.time() * 1000)
        sim_price = self._inject_latency_and_slippage(price, side)
        order_id = f"sim_{now_ms}"
        zero_bytes32 = "0x0000000000000000000000000000000000000000000000000000000000000000"

        order = {
            "order_id": order_id,
            "status": "LIVE",
            "token_id": str(token_id),
            "side": side.upper(),
            "price": sim_price,
            "amount": float(amount),
            "order_type": order_type.upper(),
            "timestamp": now_ms,
            "metadata": zero_bytes32,
            "builder": zero_bytes32,
        }
        self._ledger.add_order(order)
        metrics.orders_total.inc(labels={"strategy": "paper", "side": side.upper(), "order_type": order_type.upper(), "status": "LIVE"})
        logger.info(f"[模拟] (PaperGateway) 下单成功：{side} {token_id} @ {sim_price} x {amount} ({order_type}) -> {order_id}")
        return order

    async def post_order_async(
        self,
        token_id: str,
        price: float,
        amount: float,
        side: str = "BUY",
        order_type: str = "GTC",
    ) -> Optional[Dict[str, Any]]:
        now_ms = int(time.time() * 1000)
        sim_price = await self._inject_latency_and_slippage_async(price, side)
        order_id = f"sim_{now_ms}"
        zero_bytes32 = "0x0000000000000000000000000000000000000000000000000000000000000000"

        order = {
            "order_id": order_id,
            "status": "LIVE",
            "token_id": str(token_id),
            "side": side.upper(),
            "price": sim_price,
            "amount": float(amount),
            "order_type": order_type.upper(),
            "timestamp": now_ms,
            "metadata": zero_bytes32,
            "builder": zero_bytes32,
        }
        self._ledger.add_order(order)
        metrics.orders_total.inc(labels={"strategy": "paper", "side": side.upper(), "order_type": order_type.upper(), "status": "LIVE"})
        logger.info(f"[模拟] (PaperGateway) 异步下单成功：{side} {token_id} @ {sim_price} x {amount} ({order_type}) -> {order_id}")
        return order

    def post_batch_orders(self, orders: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        now_ms = int(time.time() * 1000)
        zero_bytes32 = "0x0000000000000000000000000000000000000000000000000000000000000000"
        sim_orders = []

        for i, o in enumerate(orders):
            side = str(o.get("side", "BUY")).upper()
            price = float(o.get("price", 0.5))
            sim_price = self._inject_latency_and_slippage(price, side)
            order_id = f"sim_batch_{i}_{now_ms}"
            sim_order = {
                **o,
                "order_id": order_id,
                "price": sim_price,
                "status": "LIVE",
                "timestamp": now_ms + i,
                "metadata": zero_bytes32,
                "builder": zero_bytes32,
            }
            self._ledger.add_order(sim_order)
            metrics.orders_total.inc(labels={"strategy": "paper", "side": side.upper(), "order_type": str(o.get("order_type", "GTC")).upper(), "status": "LIVE"})
            sim_orders.append(sim_order)

        logger.info(f"[模拟] (PaperGateway) 批量下单成功：{len(sim_orders)} 笔")
        return {"status": "SIMULATED", "orders": sim_orders}

    async def post_batch_orders_async(self, orders: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        return self.post_batch_orders(orders)

    def cancel_order(self, order_id: str) -> bool:
        logger.info(f"[模拟] (PaperGateway) 取消订单: {order_id}")
        self._ledger.remove_order(order_id)
        return True

    async def cancel_order_async(self, order_id: str) -> bool:
        return self.cancel_order(order_id)

    def get_order_status(self, order_id: str) -> Optional[str]:
        return "FILLED"

    def wait_for_order_fill(self, order_id: str, timeout: float = 10.0) -> bool:
        return True

    def get_balance(self) -> Dict[str, float]:
        if self._custom_balance is not None:
            metrics.balance_usdc.set(self._custom_balance, labels={"asset_type": "paper_collateral"})
            return {"usdc": self._custom_balance, "pending": 0.0}
        from polymarket import config
        paper_cap = getattr(config, "PAPER_INITIAL_CAPITAL", 100.0)
        metrics.balance_usdc.set(float(paper_cap), labels={"asset_type": "paper_collateral"})
        return {"usdc": float(paper_cap), "pending": 0.0}

    def get_position(self, token_id: str) -> Dict[str, Any]:
        return {"position": 0.0, "pnl": 0.0}

    def get_open_orders(self) -> List[Dict[str, Any]]:
        return self._ledger.get_open_orders()

    def get_market_price(self, token_id: str) -> Dict[str, float]:
        try:
            from polymarket.services.grid import OrderbookMemoryGrid
            snap = OrderbookMemoryGrid().get_snapshot(token_id)
            if snap and snap.best_bid is not None and snap.best_ask is not None:
                return {"bid": snap.best_bid, "ask": snap.best_ask}
        except Exception:
            pass
        return {"bid": 0.45, "ask": 0.55}

    async def get_market_price_async(self, token_id: str) -> Dict[str, float]:
        return self.get_market_price(token_id)

    def get_orderbook(self, token_id: str) -> Dict[str, Any]:
        return {"bids": [{"price": "0.45", "size": "100.0"}], "asks": [{"price": "0.55", "size": "100.0"}]}

    def redeem(self, market_id: str) -> Dict[str, Any]:
        return {"market_id": market_id, "status": "SIMULATED", "payout": 0.0}

    async def redeem_async(self, market_id: str) -> Dict[str, Any]:
        return self.redeem(market_id)

    def check_user_trade_filled(self, token_id: str, max_age_seconds: float = 30.0) -> bool:
        return True

    def list_closed_markets(self) -> List[Dict[str, Any]]:
        return []

    def close(self) -> None:
        try:
            self.http2_client.close()
        except Exception:
            pass
