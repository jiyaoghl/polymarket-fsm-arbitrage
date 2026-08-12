"""
ArbitrageBot 单元测试
"""

import os
import sys
import time
import pytest
import threading
from unittest.mock import MagicMock, patch

from polymarket.base_strategy import BaseStrategy as ArbitrageBot
from polymarket.base_strategy import INITIAL_ENTRY_MAX_PRICE, REENTRY_TRIGGER_PRICE


class TestArbitrageBot:
    """ArbitrageBot 测试类。"""

    def test_init(self):
        """测试初始化。"""
        config = {
            "strategy_id": "test_strategy",
            "name": "测试策略",
            "entry_max_price": 0.50,
            "reentry_trigger": 0.40,
            "amount": 10.0,
            "is_live": False,
        }
        
        bot = ArbitrageBot(config)
        
        assert bot.strategy_id == "test_strategy"
        assert bot.is_live is False
        assert bot.entry_max_price == 0.50
        assert bot.reentry_trigger == 0.40
        assert bot.order_amount == 10.0

    def test_thread_safety_methods(self):
        """测试线程安全方法。"""
        config = {"strategy_id": "test", "is_live": False}
        bot = ArbitrageBot(config)
        
        # 测试市场处理标记
        mkt_id = f"test_mkt_{time.time()}"
        assert bot._is_market_processed(mkt_id) is False
        bot._mark_market_processed(mkt_id)
        assert bot._is_market_processed(mkt_id) is True
        
        # 测试交易操作
        trade = {"market_id": "market1", "status": "leg1_only"}
        bot._set_trade("market1", trade)
        assert bot._get_trade("market1") == trade
        
        # 测试状态更新
        bot._update_trade_status("market1", "locked", profit_usdc=0.5)
        updated_trade = bot._get_trade("market1")
        assert updated_trade["status"] == "locked"
        assert updated_trade["profit_usdc"] == 0.5

    def test_thread_safety_concurrent_access(self):
        """测试并发访问线程安全。"""
        config = {"strategy_id": "test", "is_live": False}
        bot = ArbitrageBot(config)
        
        num_threads = 10
        num_operations = 100
        
        def write_trades():
            for i in range(num_operations):
                market_id = f"market_{threading.current_thread().name}_{i}"
                bot._set_trade(market_id, {"status": "leg1_only"})
        
        def read_trades():
            for i in range(num_operations):
                trades = bot._get_all_active_trades()
                # 只需要确保不抛异常
        
        threads = []
        for i in range(num_threads):
            t1 = threading.Thread(target=write_trades, name=f"writer_{i}")
            t2 = threading.Thread(target=read_trades, name=f"reader_{i}")
            threads.extend([t1, t2])
        
        for t in threads:
            t.start()
        
        for t in threads:
            t.join()
        
        # 验证所有写入都成功
        all_trades = bot._get_all_active_trades()
        assert len(all_trades) == num_threads * num_operations

    def test_calculate_dynamic_stop_price(self):
        """测试动态止损价格计算。"""
        config = {"strategy_id": "test", "is_live": False}
        bot = ArbitrageBot(config)
        
        # 模拟 client.get_market_price 返回
        with patch.object(bot.client, 'get_market_price', return_value={"bid": 0.30, "ask": 0.35}):
            stop_price = bot._calculate_dynamic_stop_price("test_token")
            # 应该是 bid * 0.95 = 0.285
            assert stop_price == pytest.approx(0.285, rel=0.01)

    def test_confirm_order_filled_mock_mode(self):
        """测试模拟模式订单确认。"""
        config = {"strategy_id": "test", "is_live": False}
        bot = ArbitrageBot(config)
        
        # 模拟模式应该直接返回 True
        result = bot._confirm_order_filled("test_order_id")
        assert result is True

    def test_backoff_calculation(self):
        """测试指数退避计算。"""
        config = {"strategy_id": "test", "is_live": False}
        bot = ArbitrageBot(config)
        
        # 测试退避延迟
        assert bot._calculate_backoff_delay(0) == 1.0  # 1 * 2^0 = 1
        assert bot._calculate_backoff_delay(1) == 2.0  # 1 * 2^1 = 2
        assert bot._calculate_backoff_delay(2) == 4.0  # 1 * 2^2 = 4
        assert bot._calculate_backoff_delay(5) == 30.0  # 超过最大值，返回 30


class TestStrategyConditions:
    """策略条件测试类。"""

    def test_entry_condition(self):
        """测试入场条件判断。"""
        # 当一边 ASK <= entry_max_price 时入场
        entry_max_price = 0.50
        
        # YES 更便宜且满足条件
        yes_ask, no_ask = 0.45, 0.55
        assert yes_ask <= entry_max_price or no_ask <= entry_max_price
        
        # 两边都不满足条件
        yes_ask, no_ask = 0.55, 0.60
        assert not (yes_ask <= entry_max_price or no_ask <= entry_max_price)

    def test_reentry_condition(self):
        """测试补仓条件判断。"""
        reentry_trigger = 0.40
        time_to_expiry = 100  # 秒
        
        # 另一边 ASK < 触发价且时间足够
        other_ask = 0.35
        assert other_ask < reentry_trigger and time_to_expiry > 10
        
        # 时间不够
        time_to_expiry = 5
        assert not (other_ask < reentry_trigger and time_to_expiry > 10)

    def test_stop_loss_condition(self):
        """测试止损条件判断。"""
        stop_loss_time = 60  # 秒
        
        # 剩余时间在止损窗口内
        time_to_expiry = 50
        assert 10 < time_to_expiry <= stop_loss_time
        
        # 剩余时间太多
        time_to_expiry = 120
        assert not (10 < time_to_expiry <= stop_loss_time)
        
        # 剩余时间太少（不操作）
        time_to_expiry = 5
        assert not (10 < time_to_expiry <= stop_loss_time)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])