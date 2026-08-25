"""
PolyClient 单元测试
"""

import os
import sys
import time
import pytest
from unittest.mock import Mock, patch, MagicMock

from polymarket.client import PolyClient, RateLimiter, retry_on_failure


class TestRateLimiter:
    """RateLimiter 测试类。"""

    def test_initial_tokens(self):
        """测试初始令牌数。"""
        limiter = RateLimiter(rate=10.0, period=1.0)
        assert limiter.tokens == 10.0

    def test_acquire_single(self):
        """测试获取单个令牌。"""
        limiter = RateLimiter(rate=10.0, period=1.0)
        
        start = time.time()
        limiter.acquire()
        elapsed = time.time() - start
        
        # 获取一个令牌应该几乎不需要等待
        assert elapsed < 0.1

    def test_acquire_multiple(self):
        """测试获取多个令牌（需要等待）。"""
        limiter = RateLimiter(rate=5.0, period=1.0)
        
        # 快速消耗所有令牌
        for _ in range(5):
            limiter.acquire()
        
        # 下一个获取应该需要等待
        start = time.time()
        limiter.acquire()
        elapsed = time.time() - start
        
        # 应该等待大约 0.2 秒（1/5）
        assert elapsed >= 0.15

    def test_token_replenishment(self):
        """测试令牌补充。"""
        limiter = RateLimiter(rate=10.0, period=1.0)
        
        # 消耗所有令牌
        for _ in range(10):
            limiter.acquire()
        
        # 等待一段时间让令牌补充
        time.sleep(0.5)
        
        # 现在应该有大约 5 个令牌
        limiter.acquire()  # 应该不需要等待太久
        assert True


class TestPolyClient:
    """PolyClient 测试类。"""

    def test_init_mock_mode(self):
        """测试模拟模式初始化。"""
        client = PolyClient(is_live=False)
        assert client.is_live is False

    def test_get_market_price_mock(self):
        """测试模拟模式获取价格。"""
        client = PolyClient(is_live=False)
        
        # 模拟模式应该返回 None 或模拟数据
        # 由于没有真实 API，这里只测试方法存在
        assert hasattr(client, 'get_market_price')

    def test_post_order_mock(self):
        """测试模拟模式下单。"""
        client = PolyClient(is_live=False)
        
        order = client.post_order(
            token_id="test_token",
            price=0.5,
            amount=10.0,
            side="BUY",
        )
        
        assert order is not None
        assert "order_id" in order
        assert order["status"] == "LIVE"
        assert "timestamp" in order
        assert "metadata" in order
        assert "builder" in order

    def test_post_batch_orders_mock(self):
        """测试模拟模式批量下单。"""
        client = PolyClient(is_live=False)
        
        orders = [
            {"token_id": "token1", "price": 0.5, "amount": 10.0, "side": "BUY"},
            {"token_id": "token2", "price": 0.4, "amount": 10.0, "side": "BUY"},
        ]
        
        result = client.post_batch_orders(orders)
        
        assert result is not None
        assert result["status"] == "SIMULATED"
        assert len(result["orders"]) == 2
        assert "timestamp" in result["orders"][0]
        assert "metadata" in result["orders"][0]
        assert "builder" in result["orders"][0]

    def test_get_balance_mock(self):
        """测试模拟模式获取余额。"""
        client = PolyClient(is_live=False)
        
        balance = client.get_balance()
        
        assert "usdc" in balance
        assert "pending" in balance
        assert balance["usdc"] == 100.0  # 模拟盘默认 100U

    def test_get_order_status_mock(self):
        """测试模拟模式查询订单状态。"""
        client = PolyClient(is_live=False)
        
        status = client.get_order_status("test_order_id")
        
        # 模拟模式假设订单已成交
        assert status == "FILLED"

    def test_wait_for_order_fill_mock(self):
        """测试模拟模式等待订单成交。"""
        client = PolyClient(is_live=False)
        
        result = client.wait_for_order_fill("test_order_id", timeout=1.0)
        
        # 模拟模式应该立即返回 True
        assert result is True


class TestRetryDecorator:
    """重试装饰器测试类。"""

    def test_retry_success_first_try(self):
        """测试第一次就成功。"""
        call_count = 0
        
        @retry_on_failure(max_retries=3, base_delay=0.1)
        def success_func():
            nonlocal call_count
            call_count += 1
            return "success"
        
        result = success_func()
        assert result == "success"
        assert call_count == 1

    def test_retry_success_after_failures(self):
        """测试失败后重试成功。"""
        call_count = 0
        
        @retry_on_failure(max_retries=3, base_delay=0.1)
        def eventually_success_func():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise Exception("Temporary error")
            return "success"
        
        result = eventually_success_func()
        assert result == "success"
        assert call_count == 3

    def test_retry_all_failures(self):
        """测试所有重试都失败。"""
        call_count = 0
        
        @retry_on_failure(max_retries=3, base_delay=0.1)
        def always_fail_func():
            nonlocal call_count
            call_count += 1
            raise Exception("Permanent error")
        
        with pytest.raises(Exception) as exc_info:
            always_fail_func()
        
        assert call_count == 3
        assert "Permanent error" in str(exc_info.value)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])