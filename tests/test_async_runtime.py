import asyncio
import time
import pytest
from unittest.mock import MagicMock, patch

from polymarket.runtime import AsyncRuntime, MarketTaskSupervisor, BoundedDropOldestQueue
from polymarket.streamer import MarketDataStreamer
from polymarket.user_streamer import UserOrderStreamer

def test_bounded_queue_drop_oldest():
    """测试 BoundedDropOldestQueue 满载时自动丢弃最旧元素"""
    async def _test():
        q = BoundedDropOldestQueue(maxsize=3)
        q.put_nowait(1)
        q.put_nowait(2)
        q.put_nowait(3)
        assert q.full()
        
        # 压入第 4 个元素，应该丢弃 1
        q.put_nowait(4)
        assert q.qsize() == 3
        
        items = []
        while not q.empty():
            items.append(q.get_nowait())
            
        assert items == [2, 3, 4]

    asyncio.run(_test())


def test_market_task_supervisor_lifecycle():
    """测试 MarketTaskSupervisor 的任务生命周期与自动注销"""
    async def _test():
        supervisor = MarketTaskSupervisor()
        
        async def mock_coro():
            await asyncio.sleep(0.05)
            return "done"

        task = asyncio.create_task(mock_coro())
        supervisor.register_task("task_1", task, strategy_id="test_strat", market_id="m_1")
        assert supervisor.get_active_task_count() == 1
        
        # 等待完成
        await task
        await asyncio.sleep(0.01)
        # 完成后自动注销
        assert supervisor.get_active_task_count() == 0

    asyncio.run(_test())


def test_market_task_supervisor_exception_capture():
    """测试 MarketTaskSupervisor 在任务异常崩溃时捕获并上报"""
    async def _test():
        supervisor = MarketTaskSupervisor()
        
        async def failing_coro():
            await asyncio.sleep(0.02)
            raise ValueError("测试崩溃异常")

        task = asyncio.create_task(failing_coro())
        supervisor.register_task("failing_task", task, strategy_id="test_strat", market_id="m_err")
        
        with pytest.raises(ValueError):
            await task

        await asyncio.sleep(0.01)
        assert supervisor.get_active_task_count() == 0

    asyncio.run(_test())


def test_async_runtime_singleton_and_spawn():
    """测试 AsyncRuntime 单例与异步协程派发"""
    runtime1 = AsyncRuntime.get_instance()
    runtime2 = AsyncRuntime.get_instance()
    assert runtime1 is runtime2
    assert runtime1.get_loop() is not None
    
    async def sample_coro():
        await asyncio.sleep(0.01)
        return 42

    task = runtime1.spawn_task(sample_coro(), key="test_sample_task")
    res = runtime1.run_coroutine_sync(sample_coro(), timeout=2.0)
    assert res == 42


def test_streamer_single_loop_fast_dispatch():
    """测试 MarketDataStreamer 在统一主 Loop 下的队列分发与注销"""
    streamer = MarketDataStreamer()
    assert streamer is not None
    
    queue = BoundedDropOldestQueue(maxsize=10)
    market_id = "test_market_999"
    tokens = ["tok_a", "tok_b"]
    
    streamer.subscribe(market_id, tokens, queue)
    assert market_id in streamer.subscribers
    assert "tok_a" in streamer.active_assets
    assert "tok_b" in streamer.active_assets
    
    streamer.unsubscribe(market_id, queue)
    assert market_id not in streamer.subscribers
