"""
TradeStateStore 单元测试
"""

import os
import time
import pytest
import threading

from polymarket.trade_state import TradeStateStore


class TestTradeStateStore:
    """TradeStateStore 测试类。"""

    def setup_method(self):
        """每个测试方法前的设置。"""
        self.test_file = "test_state.json"
        # 清理可能存在的测试文件
        if os.path.exists(self.test_file):
            os.remove(self.test_file)
        if os.path.exists(f"{self.test_file}.tmp"):
            os.remove(f"{self.test_file}.tmp")
        
        self.store = TradeStateStore(path=self.test_file, initial_capital=1000.0)

    def teardown_method(self):
        """每个测试方法后的清理。"""
        if os.path.exists(self.test_file):
            os.remove(self.test_file)
        if os.path.exists(f"{self.test_file}.tmp"):
            os.remove(f"{self.test_file}.tmp")

    def test_initial_state(self):
        """测试初始状态。"""
        assert self.store.state == {}
        assert self.store.initial_capital == 1000.0

    def test_record_pnl(self):
        """测试记录盈亏。"""
        # 记录盈利
        self.store.record_pnl(10.0)
        stats = self.store.get_today_stats()
        assert stats["pnl"] == 10.0
        assert stats["equity"] == 1010.0

        # 记录亏损
        self.store.record_pnl(-5.0)
        stats = self.store.get_today_stats()
        assert stats["pnl"] == 5.0
        assert stats["equity"] == 1005.0

    def test_drawdown_calculation(self):
        """测试回撤计算。"""
        # 记录盈利
        self.store.record_pnl(50.0)
        stats = self.store.get_today_stats()
        assert stats["max_drawdown"] == 0.0

        # 记录亏损，产生回撤
        self.store.record_pnl(-100.0)
        stats = self.store.get_today_stats()
        assert stats["max_drawdown"] == 50.0  # 从 1050 跌到 950

    def test_should_pause_for_drawdown(self):
        """测试回撤暂停判断。"""
        # 5% 回撤限制 = 50 USDC
        self.store.record_pnl(-60.0)  # 6% 回撤
        assert self.store.should_pause_for_drawdown(0.05) is True

        # 重置后测试
        self.store.reset_today()
        assert self.store.should_pause_for_drawdown(0.05) is False

    def test_thread_safety(self):
        """测试线程安全。"""
        num_threads = 10
        num_operations = 100
        
        def record_pnl_thread():
            for _ in range(num_operations):
                self.store.record_pnl(1.0)
        
        threads = []
        for _ in range(num_threads):
            t = threading.Thread(target=record_pnl_thread)
            threads.append(t)
            t.start()
        
        for t in threads:
            t.join()
        
        # 验证最终结果
        stats = self.store.get_today_stats()
        expected_pnl = num_threads * num_operations  # 1000
        assert stats["pnl"] == expected_pnl

    def test_get_today_stats(self):
        """测试获取今日统计。"""
        self.store.record_pnl(25.0)
        self.store.record_pnl(-10.0)
        
        stats = self.store.get_today_stats()
        assert "date" in stats
        assert "initial_capital" in stats
        assert "pnl" in stats
        assert "equity" in stats
        assert "max_drawdown" in stats
        assert stats["pnl"] == 15.0
        assert stats["equity"] == 1015.0

    def test_reset_today(self):
        """测试重置今日统计。"""
        self.store.record_pnl(100.0)
        assert self.store.get_today_stats()["pnl"] == 100.0
        
        self.store.reset_today()
        stats = self.store.get_today_stats()
        assert stats["pnl"] == 0.0
        assert stats["max_drawdown"] == 0.0

    def test_atomic_write(self):
        """测试原子写入。"""
        # 记录一些数据
        self.store.record_pnl(50.0)
        
        # 检查文件存在
        assert os.path.exists(self.test_file)
        
        # 检查临时文件不存在
        assert not os.path.exists(f"{self.test_file}.tmp")
        
        # 重新加载验证数据持久化
        new_store = TradeStateStore(path=self.test_file, initial_capital=1000.0)
        stats = new_store.get_today_stats()
        assert stats["pnl"] == 50.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])