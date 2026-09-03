import asyncio
import time
from typing import Dict, Any, Optional, Tuple

from polymarket.logger import logger
from polymarket.client import PolyClient
from polymarket.risk_manager import RiskManager
from polymarket.domain.models import LegPosition

class OrderExecutionService:
    """
    统一订单安全执行服务 (Order Execution Service)。
    
    职责：
    1. 份数与价格安全钳制 (safe_price in [0.001, 0.999], safe_shares >= 5.0)
    2. 自适应 FOK 滑点微重试 (带延迟与滑点阈值控制)
    3. 幻象失败防御与免签 Data API 终极对账 (Phantom-Fill Reconciliation)
    """

    @staticmethod
    def sanitize_order_params(price: float, amount_usdc: float) -> Tuple[float, float]:
        """
        对价格和金额执行安全钳制与份数折算。
        
        Returns:
            (safe_price, safe_shares)
        """
        safe_price = round(min(max(float(price), 0.001), 0.999), 4)
        calc_shares = amount_usdc / safe_price
        safe_shares = round(max(calc_shares, 5.0), 2)
        return safe_price, safe_shares

    @staticmethod
    async def adaptive_post_order(
        client: PolyClient,
        risk_manager: RiskManager,
        token_id: str,
        initial_price: float,
        amount_shares: float,
        side: str,
        initial_order_type: str,
        max_slippage: float = 0.005,
        max_retries: int = 3,
        strategy_id: str = "default"
    ) -> Optional[Dict[str, Any]]:
        """
        带滑点保护与自动微重试的异步发单。
        """
        safe_price, safe_shares = OrderExecutionService.sanitize_order_params(initial_price, initial_price * amount_shares)
        
        # 首发冲击
        order = await client.post_order_async(token_id, safe_price, safe_shares, side, initial_order_type)
        if order and order.get("status") not in ("ERROR", None):
            return order

        # 仅针对 FOK 吃单模式启动微重试
        if initial_order_type != "FOK":
            return None

        logger.warning(f"[执行服务：{strategy_id}] 首发 FOK 失败，启动滑点微重试 (max_slippage={max_slippage})")
        
        for i in range(max_retries):
            await asyncio.sleep(0.35)  # 350ms 避开同秒并发与撮合限频
            
            price_info = await client.get_market_price_async(token_id)
            if not price_info:
                continue

            new_price = price_info.get("ask") if side == "BUY" else price_info.get("bid")
            if new_price is None:
                continue

            if side == "BUY" and new_price > initial_price + max_slippage:
                logger.warning(f"[执行服务：{strategy_id}] 重试终止：卖一价 {new_price} 超出滑点上限 {initial_price + max_slippage}")
                break
            if side == "SELL" and new_price < initial_price - max_slippage:
                logger.warning(f"[执行服务：{strategy_id}] 重试终止：买一价 {new_price} 低于滑点下限 {initial_price - max_slippage}")
                break

            safe_new_price, safe_new_shares = OrderExecutionService.sanitize_order_params(new_price, new_price * amount_shares)
            logger.info(f"[执行服务：{strategy_id}] 第 {i+1} 次微重试，以新价格 {safe_new_price} 发送订单")
            
            order = await client.post_order_async(token_id, safe_new_price, safe_new_shares, side, initial_order_type)
            if order and order.get("status") not in ("ERROR", None):
                risk_manager.record_adaptive_retry(True)
                return order

        risk_manager.record_adaptive_retry(False)
        return None

    @staticmethod
    async def async_reconcile_phantom_fill(
        client: PolyClient,
        order_id: Optional[str],
        token_id: str,
        expected_size: float,
        strategy_id: str = "default",
        timeout: float = 10.0
    ) -> Tuple[bool, Optional[LegPosition]]:
        """
        全异步高阶成交确认服务：
        1. [P0 极速防线] 优先通过 UserOrderStreamer 私有 WebSocket 监听成交推送 (<5ms 响应)。
        2. [P0 终极对账] 若 WS 超时或未捕获，无缝降级到 CLOB REST 与 Data API 链上对账。
        """
        if not client.is_live:
            # 模拟盘直接按成交返回
            return True, LegPosition(order_id=order_id or "sim_order", token=token_id, cost=0.5, size=expected_size)

        if not order_id:
            return False, None

        # 1. 优先尝试私有 WebSocket 事件流
        try:
            from polymarket.user_streamer import UserOrderStreamer
            streamer = UserOrderStreamer.get_instance()
            if streamer.is_authenticated:
                ws_result = await streamer.wait_for_order_fill(order_id, timeout=min(timeout, 3.0))
                if ws_result and ws_result.get("status") in ("FILLED", "PARTIALLY_FILLED"):
                    actual_sz = float(ws_result.get("size") or expected_size)
                    is_partial = (ws_result.get("status") == "PARTIALLY_FILLED") or (actual_sz < expected_size * 0.99)
                    logger.info(
                        f"[执行服务：{strategy_id}] [私有WS] 捕获到订单成交回报！Order: {order_id}, "
                        f"Size: {actual_sz:.2f}/{expected_size:.2f}{' [部分成交]' if is_partial else ''}"
                    )
                    return True, LegPosition(
                        order_id=order_id,
                        token=token_id,
                        cost=float(ws_result.get("price") or 0.5),
                        size=actual_sz,
                        original_size=expected_size,
                        is_partially_filled=is_partial
                    )

        except Exception as e:
            logger.warning(f"[执行服务：{strategy_id}] 私有 WS 监听异常，转入 REST 对账: {e}")

        # 2. 降级到线程池调度同步终极对账
        return await asyncio.to_thread(
            OrderExecutionService.reconcile_phantom_fill,
            client, order_id, token_id, expected_size, strategy_id, timeout
        )

    @staticmethod
    def reconcile_phantom_fill(
        client: PolyClient,
        order_id: Optional[str],
        token_id: str,
        expected_size: float,
        strategy_id: str = "default",
        timeout: float = 10.0
    ) -> Tuple[bool, Optional[LegPosition]]:
        """
        终极防线：在 REST 超时或抛出 400/401 疑似丢单时，向免签公共 Data API 对账链上真实成交。
        
        Returns:
            (is_filled, leg_position_or_none)
        """
        if not client.is_live:
            # 模拟盘直接按成交返回
            return True, LegPosition(order_id=order_id or "sim_order", token=token_id, cost=0.5, size=expected_size)

        start_ts = time.time()
        while time.time() - start_ts < timeout:
            try:
                # 1. 优先通过标准 CLOB API 查单
                if order_id:
                    status = client.get_order_status(order_id)
                    if status == "FILLED":
                        return True, LegPosition(order_id=order_id, token=token_id, size=expected_size)
                    elif status in ("CANCELLED", "EXPIRED", "REJECTED"):
                        return False, None

                # 2. 调用公共免签 Data API 检查该用户最近成交
                user_addr = getattr(client, "address", None)
                if user_addr:
                    recent_trades = client.get_user_trades_public(user_addr, token_id)
                    for tr in recent_trades:
                        tr_time = float(tr.get("timestamp", 0))
                        # 30秒内产生的同 Token 买入成交
                        if abs(time.time() - tr_time) < 30 and float(tr.get("size", 0)) >= expected_size * 0.95:
                            logger.info(f"[执行服务：{strategy_id}] [Data API 对账成功] 捕获到真实链上成交！Order: {order_id}")
                            return True, LegPosition(
                                order_id=tr.get("order_id") or order_id,
                                token=token_id,
                                cost=float(tr.get("price", 0)),
                                size=float(tr.get("size", expected_size))
                            )
            except Exception as e:
                logger.warning(f"[执行服务：{strategy_id}] 对账查询异常: {e}")

            time.sleep(1.0)

        return False, None
