import pytest
import asyncio
from unittest.mock import MagicMock, patch
from polymarket.user_streamer import UserOrderStreamer
from polymarket.services.execution import OrderExecutionService
from polymarket.client import PolyClient

def test_user_order_streamer_singleton_and_event_dispatch():
    """测试 UserOrderStreamer 单例与事件毫秒级分发唤醒"""
    async def _test():
        streamer = UserOrderStreamer.get_instance()
        assert streamer is not None
        assert UserOrderStreamer.get_instance() is streamer

        test_order_id = "0x_test_order_123"
        
        async def wait_task():
            return await streamer.wait_for_order_fill(test_order_id, timeout=2.0)

        task = asyncio.create_task(wait_task())
        await asyncio.sleep(0.05)

        # 模拟推送
        mock_fill_payload = {
            "event_type": "trade",
            "order_id": test_order_id,
            "status": "FILLED",
            "price": "0.45",
            "size": "10.0"
        }
        streamer._handle_incoming_event(mock_fill_payload)

        res = await task
        assert res is not None
        assert res.get("order_id") == test_order_id
        assert res.get("status") == "FILLED"
        assert res.get("price") == 0.45
        assert res.get("size") == 10.0

    asyncio.run(_test())


def test_user_order_streamer_timeout():
    """测试订单未成交时超时返回 None"""
    async def _test():
        streamer = UserOrderStreamer.get_instance()
        res = await streamer.wait_for_order_fill("0x_non_existent_order", timeout=0.1)
        assert res is None

    asyncio.run(_test())


def test_execution_service_async_reconcile_flow():
    """测试 OrderExecutionService 异步对账流程 (模拟模式与私有 WS 模式)"""
    async def _test():
        mock_paper_client = MagicMock(spec=PolyClient)
        mock_paper_client.is_live = False

        # 1. 模拟模式直接返回成交
        is_fill, pos = await OrderExecutionService.async_reconcile_phantom_fill(
            mock_paper_client, "sim_order_1", "token_yes", 10.0
        )
        assert is_fill is True
        assert pos is not None
        assert pos.size == 10.0

        # 2. 实盘模式模拟 WS 优先捕获
        mock_live_client = MagicMock(spec=PolyClient)
        mock_live_client.is_live = True

        streamer = UserOrderStreamer.get_instance()
        streamer.is_authenticated = True

        test_live_order = "0x_live_fill_456"

        async def reconcile_task():
            return await OrderExecutionService.async_reconcile_phantom_fill(
                mock_live_client, test_live_order, "token_yes", 15.0, timeout=1.0
            )

        task = asyncio.create_task(reconcile_task())
        await asyncio.sleep(0.05)

        streamer._handle_incoming_event({
            "order_id": test_live_order,
            "status": "FILLED",
            "price": "0.48",
            "size": "15.0"
        })

        fill_ok, fill_pos = await task
        assert fill_ok is True
        assert fill_pos is not None
        assert fill_pos.cost == 0.48
        assert fill_pos.size == 15.0

    asyncio.run(_test())
