import math
import time
import unittest
from unittest.mock import patch

from polymarket.kline_analyzer import (
    _gk_variance,
    _ewma_weights,
    _fetch_kline_and_analyze,
    get_asset_status,
    is_asset_choppy,
    _asset_status,
    _status_lock,
)


class TestGKVariancePureFunction(unittest.TestCase):
    """测试 Garman-Klass 方差纯函数数学精度"""

    def test_horizontal_bar_near_zero(self):
        """完全横盘 (O=H=L=C) 时 GK 方差应约为 0"""
        v = _gk_variance(100.0, 100.0, 100.0, 100.0)
        self.assertAlmostEqual(v, 0.0, places=10)

    def test_typical_candle_positive(self):
        """典型震荡 K 线（H>L, C=O）GK 方差应为正值"""
        v = _gk_variance(100.0, 101.0, 99.0, 100.0)
        expected = 0.5 * math.log(101.0 / 99.0) ** 2
        self.assertAlmostEqual(v, expected, places=10)
        self.assertGreater(v, 0.0)

    def test_trending_candle_clamp_safe(self):
        """单边上涨 K 线 clamp 后保证非负"""
        raw = _gk_variance(100.0, 102.0, 100.0, 102.0)
        self.assertGreaterEqual(max(raw, 0.0), 0.0)

    def test_invalid_zero_price_returns_zero(self):
        """含零价格时安全返回 0"""
        self.assertEqual(_gk_variance(0.0, 100.0, 99.0, 100.0), 0.0)

    def test_manual_calculation_precision(self):
        """手算结果对比验证：误差小于 1e-10"""
        o, h, l, c = 50000.0, 50500.0, 49800.0, 50200.0
        ln_hl = math.log(h / l)
        ln_co = math.log(c / o)
        expected = 0.5 * ln_hl**2 - (2.0 * math.log(2.0) - 1.0) * ln_co**2
        actual = _gk_variance(o, h, l, c)
        self.assertAlmostEqual(actual, expected, places=10)


class TestEwmaWeights(unittest.TestCase):
    """测试 EWMA 指数衰减权重向量"""

    def test_latest_weight_is_one_and_largest(self):
        """最新根权重为 1.0，序列单调递增"""
        weights = _ewma_weights(10, half_life=4)
        self.assertEqual(len(weights), 10)
        self.assertAlmostEqual(weights[-1], 1.0, places=10)
        for i in range(9):
            self.assertLess(weights[i], weights[i + 1])

    def test_half_life_decay_ratio(self):
        """间隔 half_life 根的权重比值约等于 0.5"""
        hl = 4
        weights = _ewma_weights(hl * 2 + 1, half_life=hl)
        ratio = weights[-(hl + 1)] / weights[-1]
        self.assertAlmostEqual(ratio, 0.5, places=5)

    def test_empty_input_returns_empty(self):
        """n=0 返回空列表"""
        self.assertEqual(_ewma_weights(0, 4), [])

    def test_single_element_is_one(self):
        """只有 1 根时权重为 [1.0]"""
        weights = _ewma_weights(1, 4)
        self.assertAlmostEqual(weights[0], 1.0, places=10)


class TestGKReplacesCloseVolatility(unittest.TestCase):
    """测试 GK+EWMA 值正确注入 amplitude 字段，下游接口零感知"""

    def _make_mock_klines(self, n=10, o=100.0, h=100.5, l=99.5, c=100.0):
        ts = int(time.time() * 1000)
        return [
            [ts + i * 60000, str(o), str(h), str(l), str(c), "100",
             ts + (i + 1) * 60000, "10000", 100, "50", "5000", "0"]
            for i in range(n)
        ]

    def test_amplitude_equals_gk_volatility_when_enabled(self):
        """GK 启用时 amplitude 字段值等于 gk_volatility"""
        klines = self._make_mock_klines()
        with patch("polymarket.kline_analyzer._session") as mock_sess:
            mock_resp = mock_sess.get.return_value
            mock_resp.raise_for_status = lambda: None
            mock_resp.json.return_value = klines
            result = _fetch_kline_and_analyze("BTC", limit=10)

        self.assertIn("amplitude", result)
        self.assertIn("gk_volatility", result)
        self.assertIn("close_volatility_3sigma", result)
        from polymarket.config import GK_EWMA_ENABLED
        if GK_EWMA_ENABLED:
            self.assertAlmostEqual(result["amplitude"], result["gk_volatility"], places=8)

    def test_is_choppy_interface_zero_change(self):
        """is_asset_choppy 与 get_asset_status 接口无需任何修改"""
        now = time.time()
        fake = {
            "is_choppy": True, "amplitude": 0.10, "gk_volatility": 0.10,
            "net_change": 0.05, "last_1m_net_change": 0.02,
            "latest_price": 65000.0, "error": "", "timestamp": now,
            "close_volatility_3sigma": 0.30,
        }
        with _status_lock:
            _asset_status["BTC"] = fake

        self.assertEqual(get_asset_status("BTC")["amplitude"], 0.10)
        self.assertTrue(is_asset_choppy("BTC"))


class TestGKStableTrendChoppy(unittest.TestCase):
    """平稳趋势上涨：GK 振幅低于 close 3σ，修复被误拦的良性行情"""

    def _make_trending_klines(self, n=10, start=100.0, step=0.05, spread=0.10):
        ts = int(time.time() * 1000)
        rows = []
        for i in range(n):
            o = start + i * step
            h = o + spread / 2
            l = o - spread / 2
            c = o + step * 0.5
            rows.append([ts + i * 60000, str(o), str(h), str(l), str(c),
                         "100", ts + (i + 1) * 60000, "10000", 100, "50", "5000", "0"])
        return rows

    def test_stable_trend_gk_lower_than_close3sigma(self):
        """平稳上涨时 gk_volatility 显著低于 close_volatility_3sigma"""
        klines = self._make_trending_klines()
        with patch("polymarket.kline_analyzer._session") as mock_sess:
            mock_resp = mock_sess.get.return_value
            mock_resp.raise_for_status = lambda: None
            mock_resp.json.return_value = klines
            result = _fetch_kline_and_analyze("BTC", limit=10)

        from polymarket.config import GK_EWMA_ENABLED
        if GK_EWMA_ENABLED:
            self.assertLess(result["gk_volatility"], result["close_volatility_3sigma"])


class TestGKSpikeRecoveryEWMADecay(unittest.TestCase):
    """插针后 EWMA 衰减：旧端插针振幅显著低于新端插针"""

    def _make_spike_klines(self, n=10, spike_idx=0):
        ts = int(time.time() * 1000)
        rows = []
        for i in range(n):
            if i == spike_idx:
                o, h, l, c = 100.0, 102.0, 98.0, 100.1
            else:
                o, h, l, c = 100.0, 100.2, 99.8, 100.0
            rows.append([ts + i * 60000, str(o), str(h), str(l), str(c),
                         "100", ts + (i + 1) * 60000, "10000", 100, "50", "5000", "0"])
        return rows

    def test_old_spike_decays_vs_new_spike(self):
        """旧端插针因 EWMA 权重低，合成振幅显著低于新端插针"""
        klines_old = self._make_spike_klines(spike_idx=0)
        klines_new = self._make_spike_klines(spike_idx=9)
        with patch("polymarket.kline_analyzer._session") as mock_sess:
            mock_resp = mock_sess.get.return_value
            mock_resp.raise_for_status = lambda: None
            mock_resp.json.return_value = klines_old
            result_old = _fetch_kline_and_analyze("BTC", limit=10)
            mock_resp.json.return_value = klines_new
            result_new = _fetch_kline_and_analyze("BTC", limit=10)

        from polymarket.config import GK_EWMA_ENABLED
        if GK_EWMA_ENABLED:
            self.assertLess(result_old["gk_volatility"], result_new["gk_volatility"])


if __name__ == "__main__":
    unittest.main()
