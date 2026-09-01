import unittest
import time
from unittest.mock import patch, MagicMock

from polymarket.kline_analyzer import (
    KlineRefresherDaemon,
    get_asset_status,
    is_asset_choppy,
    _asset_status,
    _status_lock
)
from polymarket.apps.dashboard import _get_dashboard_html


class TestKlineRefresherAndDashboard(unittest.TestCase):
    """测试 K 线后台守护刷新与仪表盘模板解耦"""

    def test_kline_daemon_singleton_and_memory_access(self):
        """测试 K 线守护线程单例与纯内存毫秒级读取"""
        d1 = KlineRefresherDaemon()
        d2 = KlineRefresherDaemon()
        self.assertIs(d1, d2)

        # 模拟内存数据注入
        now = time.time()
        with _status_lock:
            _asset_status["BTC"] = {
                "is_choppy": True,
                "amplitude": 0.35,
                "net_change": 0.12,
                "last_1m_net_change": 0.05,
                "latest_price": 65000.0,
                "error": "",
                "timestamp": now
            }

        # 验证纳秒级读取
        t0 = time.perf_counter()
        st = get_asset_status("BTC")
        t_cost = (time.perf_counter() - t0) * 1000  # ms
        
        self.assertEqual(st["amplitude"], 0.35)
        self.assertTrue(is_asset_choppy("BTC"))
        self.assertLess(t_cost, 5.0, "内存读取耗时必须小于 5ms")

    def test_dashboard_template_loading(self):
        """测试 dashboard 前端独立模板加载与缓存"""
        html = _get_dashboard_html()
        self.assertIsInstance(html, str)
        self.assertIn("5min Symmetric Bot Dashboard", html)
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("function updatePrices", html)


if __name__ == "__main__":
    unittest.main()
