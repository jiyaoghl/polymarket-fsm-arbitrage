import os
import time
import json
import threading
import asyncio
import httpx
from typing import Dict, List, Optional, Any
from eth_account import Account

from polymarket.gateway.base import ITradingGateway
from polymarket.gateway.codec import CLOBProtocolCodec
from polymarket.config import (
    PK, API_KEY, API_PASSPHRASE, CLOB_HOST, GAMMA_HOST, HTTP_PROXY, HTTPS_PROXY, SIGNATURE_TYPE
)
from polymarket.logger import logger
from polymarket.metrics import metrics

class LiveClobV2Gateway(ITradingGateway):
    """
    Polymarket CLOB V2 真实交易网关 (Live CLOB V2 Gateway)。
    
    核心特性：
    1. HTTP/2 多路复用连接池与主动预热通道；
    2. 纯原生 EIP-712 内存签名 (零外部网络延迟)；
    3. 401 跨秒动态重签自愈；
    4. 撮合异常响应深度提取 orderID 防止单边孤儿仓；
    5. 免签 Data API 终极对账防线与合约自动 Redeem。
    """

    def __init__(
        self,
        host: str = CLOB_HOST,
        gamma_host: str = GAMMA_HOST,
        private_key: Optional[str] = None,
        warm_up: bool = True
    ):
        self._host = host
        self._gamma_host = gamma_host
        self._http_lock = threading.RLock()

        # 初始化 HTTP/2 客户端与异步客户端
        proxy_url = HTTPS_PROXY or HTTP_PROXY
        self.http2_client = httpx.Client(
            http2=True,
            timeout=10.0,
            proxy=proxy_url if proxy_url else None,
            verify=True,
            trust_env=False if proxy_url else True
        )
        self.async_http2_client = httpx.AsyncClient(
            http2=True,
            timeout=10.0,
            proxy=proxy_url if proxy_url else None,
            verify=True,
            trust_env=False if proxy_url else True
        )
        self.session = self.http2_client

        # 加载钱包
        pk_to_use = private_key or PK
        self.wallet: Optional[Account] = None
        if pk_to_use and not pk_to_use.startswith("your_"):
            try:
                self.wallet = Account.from_key(pk_to_use)
                logger.info(f"[LiveGateway] 钱包已加载：{self.wallet.address[:8]}...{self.wallet.address[-6:]} (Type={SIGNATURE_TYPE})")
            except Exception as e:
                logger.warning(f"[LiveGateway] 钱包私钥加载异常: {e}")

        # 初始化链上智能合约赎回服务
        from polymarket.services.onchain_redeemer import OnChainRedeemer
        self._onchain_redeemer = OnChainRedeemer(private_key=pk_to_use)

        if warm_up:
            self._warm_up_connections()

    @property
    def is_live(self) -> bool:
        return True

    @property
    def host(self) -> str:
        return self._host

    @property
    def gamma_host(self) -> str:
        return self._gamma_host

    def _warm_up_connections(self) -> None:
        """主动预热 CLOB 与 Gamma 的 HTTP/2 连接池"""
        def _do():
            try:
                self.http2_client.get(f"{self.host}/time")
                self.http2_client.get(f"{self.gamma_host}/markets?limit=1")
                logger.info("[LiveGateway] HTTP/2 连接池预热完成，多路复用通道已就绪！")
            except Exception as e:
                logger.debug(f"[LiveGateway] 预热完成: {e}")

        threading.Thread(target=_do, daemon=True, name="LiveGateway_Warmup").start()

    def _get_auth_headers(self, method: str = "GET", request_path: str = "/", body: str = "") -> Dict[str, str]:
        """生成标准 L2 HMAC 鉴权请求头"""
        from polymarket import config
        api_key = config.API_KEY or os.getenv("POLX_API_KEY", "")
        passphrase = config.API_PASSPHRASE or os.getenv("POLX_API_PASSPHRASE", "")
        timestamp = int(time.time())
        signature = CLOBProtocolCodec.get_poly_signature(timestamp, method, request_path, body)

        headers = {
            "Content-Type": "application/json",
            "POLY_API_KEY": api_key,
            "POLY_PASSPHRASE": passphrase,
            "POLY_TIMESTAMP": str(timestamp),
            "POLY_SIGNATURE": signature,
        }
        if self.wallet:
            headers["POLY_ADDRESS"] = self.wallet.address
        return headers

    def _post_signed(self, endpoint: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """发送签名的 POST 请求，内置 401 跨秒动态重签自愈"""
        url = f"{self.host}{endpoint}"
        body = json.dumps(data, separators=(',', ':'))
        headers = self._get_auth_headers("POST", endpoint, body)

        with self._http_lock:
            response = self.http2_client.post(url, content=body, headers=headers)

        if response.status_code == 401:
            logger.warning(f"[LiveGateway] {endpoint} 遭遇 401 拦截，触发动态重签自愈...")
            time.sleep(0.15)
            headers = self._get_auth_headers("POST", endpoint, body)
            with self._http_lock:
                response = self.http2_client.post(url, content=body, headers=headers)

        response.raise_for_status()
        return response.json()

    def _get_signed(self, endpoint: str) -> Dict[str, Any]:
        """发送签名的 GET 请求"""
        url = f"{self.host}{endpoint}"
        headers = self._get_auth_headers("GET", endpoint)
        with self._http_lock:
            response = self.http2_client.get(url, headers=headers)
        response.raise_for_status()
        return response.json()

    def post_order(
        self,
        token_id: str,
        price: float,
        amount: float,
        side: str = "BUY",
        order_type: str = "GTC",
    ) -> Optional[Dict[str, Any]]:
        """实盘 CLOB V2 单笔下单"""
        if amount <= 0:
            logger.error(f"[LiveGateway] 下单数量非法: amount={amount}")
            return None
        if not self.wallet:
            logger.error("[LiveGateway] 钱包未就绪，请配置有效私钥")
            return None

        safe_price, safe_size = CLOBProtocolCodec.sanitize_order_params(price, amount)

        try:
            with metrics.timer(metrics.order_latency_seconds, labels={"gateway": "live", "action": "post_order"}):
                signed_order = CLOBProtocolCodec.create_v2_signed_order(
                    wallet=self.wallet,
                    token_id=str(token_id),
                    price=safe_price,
                    amount=safe_size,
                    side=side.upper()
                )

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
                metrics.orders_total.inc(labels={"strategy": "live", "side": side.upper(), "order_type": order_type.upper(), "status": "LIVE"})
                logger.info(f"[LiveGateway] 实盘 V2 下单成功：order_id={order_id}")
                return result

        except httpx.HTTPStatusError as he:
            err_code = str(he.response.status_code) if he.response is not None else "500"
            metrics.api_errors_total.inc(labels={"gateway": "live", "endpoint": "/order", "code": err_code})
            err_body = he.response.text if he.response is not None else ""
            logger.error(f"[LiveGateway] 实盘 V2 下单 HTTP 异常 ({he}): {err_body}")

            # 撮合层异常提取 orderID 防止幻象失败
            extracted_id = None
            if err_body:
                try:
                    err_json = json.loads(err_body)
                    if isinstance(err_json, dict):
                        extracted_id = err_json.get("orderID") or err_json.get("order_id") or err_json.get("id")
                except Exception:
                    pass

            if extracted_id:
                logger.warning(f"[LiveGateway 防御] 异常但捕获撮合 orderID={extracted_id}，转入 UNCONFIRMED 对账链路！")
                return {
                    "order_id": extracted_id,
                    "status": "UNCONFIRMED",
                    "error": err_body or str(he),
                    "token_id": token_id,
                    "side": side,
                    "price": safe_price,
                    "amount": safe_size,
                }
            return None
        except Exception as e:
            logger.exception(f"[LiveGateway] 实盘下单异常：{e}")
            return None

    async def post_order_async(
        self,
        token_id: str,
        price: float,
        amount: float,
        side: str = "BUY",
        order_type: str = "GTC",
    ) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(self.post_order, token_id, price, amount, side, order_type)

    def post_batch_orders(self, orders: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """实盘 CLOB V2 批量下单"""
        if not self.wallet:
            logger.error("[LiveGateway] 钱包未就绪，无法执行批量下单")
            return {"status": "ERROR", "error": "Wallet not ready"}

        from polymarket import config
        api_key = config.API_KEY or os.getenv("POLX_API_KEY", "")

        try:
            batch_payload = []
            for o in orders:
                amt_val = o.get("amount") if o.get("amount") is not None else o.get("size", 10.0)
                safe_price, safe_size = CLOBProtocolCodec.sanitize_order_params(
                    float(o["price"]), float(amt_val)
                )
                order_type_val = str(o.get("order_type", "GTC")).upper()
                signed = CLOBProtocolCodec.create_v2_signed_order(
                    wallet=self.wallet,
                    token_id=str(o["token_id"]),
                    price=safe_price,
                    amount=safe_size,
                    side=str(o["side"]).upper()
                )
                batch_payload.append({
                    "order": signed,
                    "owner": api_key,
                    "orderType": order_type_val,
                    "deferExec": False,
                    "postOnly": False,
                })

            result = self._post_signed("/batch-orders", {"orders": batch_payload})
            logger.info(f"[LiveGateway] 实盘批量下单成功：{len(result.get('orders', []))} 笔")
            return result
        except Exception as e:
            logger.exception(f"[LiveGateway] 批量下单失败：{e}")
            return {"status": "ERROR", "error": str(e)}

    async def post_batch_orders_async(self, orders: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(self.post_batch_orders, orders)

    def cancel_order(self, order_id: str) -> bool:
        """实盘撤单"""
        try:
            with metrics.timer(metrics.order_latency_seconds, labels={"gateway": "live", "action": "cancel_order"}):
                endpoint = f"/order/{order_id}"
                headers = self._get_auth_headers("DELETE", endpoint)
                url = f"{self.host}{endpoint}"
                with self._http_lock:
                    response = self.http2_client.delete(url, headers=headers, timeout=10)
                response.raise_for_status()
                logger.info(f"[LiveGateway] 取消订单成功：{order_id}")
                return True
        except Exception as e:
            logger.exception(f"[LiveGateway] 取消订单失败 ({order_id}): {e}")
            return False

    async def cancel_order_async(self, order_id: str) -> bool:
        return await asyncio.to_thread(self.cancel_order, order_id)

    def get_order_status(self, order_id: str) -> Optional[str]:
        """查询指定订单状态"""
        try:
            res = self._get_signed(f"/order/{order_id}")
            return res.get("status")
        except Exception as e:
            logger.warning(f"[LiveGateway] 查询订单状态失败 ({order_id}): {e}")
            return None

    def wait_for_order_fill(self, order_id: str, timeout: float = 10.0) -> bool:
        """同步轮询等待订单成交"""
        start = time.time()
        while time.time() - start < timeout:
            status = self.get_order_status(order_id)
            if status in ("FILLED", "MATCHED"):
                return True
            if status in ("CANCELLED", "EXPIRED", "REJECTED"):
                return False
            time.sleep(0.5)
        return False

    def get_balance(self) -> Dict[str, float]:
        """获取账户真实抵押品余额 (USDC / pUSD)"""
        # 优先官方 py_clob_client
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
                res = c.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
                raw_bal = float(res.get("balance", 0.0))
                balance = raw_bal / 1e6 if raw_bal > 1000 else raw_bal
                metrics.balance_usdc.set(balance, labels={"asset_type": "collateral"})
                return {"usdc": balance, "pending": 0.0}
        except Exception:
            pass

        # 降级 CLOB REST
        try:
            result = self._get_signed("/balance-allowance?asset_type=COLLATERAL")
            raw_bal = float(result.get("balance", 0))
            balance = raw_bal / 1e6 if raw_bal > 1000 else raw_bal
            metrics.balance_usdc.set(balance, labels={"asset_type": "collateral"})
            return {"usdc": balance, "pending": 0.0}
        except Exception as e:
            logger.error(f"[LiveGateway] 获取余额失败: {e}")
            return {"usdc": 0.0, "pending": 0.0}

    def get_position(self, token_id: str) -> Dict[str, Any]:
        """获取持仓数据"""
        try:
            result = self._get_signed(f"/position/{token_id}")
            return {
                "position": float(result.get("position", 0)),
                "pnl": float(result.get("pnl", 0)),
            }
        except Exception as e:
            logger.warning(f"[LiveGateway] 获取持仓失败 ({token_id}): {e}")
            return {"position": 0.0, "pnl": 0.0}

    def get_open_orders(self) -> List[Dict[str, Any]]:
        """获取当前活跃挂单"""
        try:
            result = self._get_signed("/orders")
            return result.get("orders", [])
        except Exception as e:
            logger.error(f"[LiveGateway] 获取活跃挂单失败: {e}")
            return []

    def get_market_price(self, token_id: str) -> Dict[str, float]:
        """获取买卖一价（优先从 OrderbookMemoryGrid 内存零锁读取，回退走 HTTP）"""
        try:
            from polymarket.services.grid import OrderbookMemoryGrid
            snap = OrderbookMemoryGrid().get_snapshot(token_id)
            if snap and snap.best_bid is not None and snap.best_ask is not None:
                return {"bid": snap.best_bid, "ask": snap.best_ask}
        except Exception:
            pass

        try:
            url = f"{self.host}/price?token_id={token_id}&side=buy"
            resp_buy = self.http2_client.get(url, timeout=5)
            buy_price = float(resp_buy.json().get("price", 0.5)) if resp_buy.status_code == 200 else 0.5

            url = f"{self.host}/price?token_id={token_id}&side=sell"
            resp_sell = self.http2_client.get(url, timeout=5)
            sell_price = float(resp_sell.json().get("price", 0.5)) if resp_sell.status_code == 200 else 0.5

            return {"bid": buy_price, "ask": sell_price}
        except Exception as e:
            logger.warning(f"[LiveGateway] 获取盘口价格异常 ({token_id}): {e}")
            return {"bid": 0.5, "ask": 0.5}

    async def get_market_price_async(self, token_id: str) -> Dict[str, float]:
        """异步获取买卖一价（优先从 OrderbookMemoryGrid 内存零锁读取）"""
        try:
            from polymarket.services.grid import OrderbookMemoryGrid
            snap = OrderbookMemoryGrid().get_snapshot(token_id)
            if snap and snap.best_bid is not None and snap.best_ask is not None:
                return {"bid": snap.best_bid, "ask": snap.best_ask}
        except Exception:
            pass

        try:
            resp_buy = await self.async_http2_client.get(f"{self.host}/price?token_id={token_id}&side=buy", timeout=5)
            buy_price = float(resp_buy.json().get("price", 0.5)) if resp_buy.status_code == 200 else 0.5

            resp_sell = await self.async_http2_client.get(f"{self.host}/price?token_id={token_id}&side=sell", timeout=5)
            sell_price = float(resp_sell.json().get("price", 0.5)) if resp_sell.status_code == 200 else 0.5

            return {"bid": buy_price, "ask": sell_price}
        except Exception as e:
            logger.warning(f"[LiveGateway] 异步获取盘口价格异常 ({token_id}): {e}")
            return {"bid": 0.5, "ask": 0.5}

    def get_orderbook(self, token_id: str) -> Dict[str, Any]:
        """获取订单簿深度"""
        try:
            url = f"{self.host}/book?token_id={token_id}"
            resp = self.http2_client.get(url, timeout=5)
            if resp.status_code == 200:
                return resp.json()
            return {"bids": [], "asks": []}
        except Exception as e:
            logger.warning(f"[LiveGateway] 获取 Orderbook 失败 ({token_id}): {e}")
            return {"bids": [], "asks": []}

    def redeem(self, market_id: str) -> Dict[str, Any]:
        """已到期市场链上智能合约结算与抵押品赎回"""
        try:
            with metrics.timer(metrics.order_latency_seconds, labels={"gateway": "live", "action": "redeem"}):
                if hasattr(self, "_onchain_redeemer") and self._onchain_redeemer:
                    result = self._onchain_redeemer.redeem_positions(condition_id=market_id)
                    logger.info(f"[LiveGateway] 链上 Redeem 结算完成：market={market_id}, status={result.get('status')}")
                    return result
                
                logger.warning(f"[LiveGateway] 未初始化 OnChainRedeemer，跳过赎回: {market_id}")
                return {"market_id": market_id, "status": "SKIPPED", "reason": "No redeemer"}
        except Exception as e:
            logger.exception(f"[LiveGateway] Redeem 异常 ({market_id}): {e}")
            return {"market_id": market_id, "status": "ERROR", "error": str(e), "payout": 0.0}

    async def redeem_async(self, market_id: str) -> Dict[str, Any]:
        return await asyncio.to_thread(self.redeem, market_id)

    def check_user_trade_filled(self, token_id: str, max_age_seconds: float = 30.0) -> bool:
        """免签 Data API 终极对账防线"""
        if not self.wallet:
            return False
        try:
            url = f"https://data-api.polymarket.com/trades?user={self.wallet.address.lower()}&limit=10"
            resp = self.http2_client.get(url, timeout=5)
            if resp.status_code == 200:
                trades = resp.json()
                if isinstance(trades, list):
                    now_ts = time.time()
                    for t in trades:
                        if str(t.get("asset") or "") == str(token_id):
                            t_ts = t.get("timestamp") or t.get("created_at") or 0
                            if isinstance(t_ts, (int, float)):
                                if t_ts > 1e11:
                                    t_ts = t_ts / 1000.0
                                if (now_ts - t_ts) <= max_age_seconds:
                                    logger.info(f"[LiveGateway 终极防线] Data API 确认 token={token_id} 链上成交！")
                                    return True
        except Exception as e:
            logger.warning(f"[LiveGateway 终极防线] Data API 查单异常: {e}")
            return False
        return False

    def list_closed_markets(self) -> List[Dict[str, Any]]:
        """获取已关闭市场"""
        try:
            url = f"{self.gamma_host}/markets?closed=true&limit=50"
            resp = self.http2_client.get(url, timeout=10)
            resp.raise_for_status()
            return resp.json() or []
        except Exception as e:
            logger.exception(f"[LiveGateway] 获取已关闭市场失败: {e}")
            return []

    def close(self) -> None:
        try:
            self.http2_client.close()
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(self.async_http2_client.aclose())
                else:
                    loop.run_until_complete(self.async_http2_client.aclose())
            except Exception:
                pass
            logger.info("[LiveGateway] HTTP/2 连接池已安全释放")
        except Exception:
            pass
