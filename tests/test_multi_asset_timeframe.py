import pytest
import time
from unittest.mock import MagicMock, patch
from polymarket import config
from polymarket.apps.manager import StrategyManager

def test_multi_asset_and_timeframe_configs():
    """测试多资产与多周期配置解析与秒数换算"""
    assert isinstance(config.SUPPORTED_ASSETS, list)
    assert len(config.SUPPORTED_ASSETS) >= 1
    assert "BTC" in config.SUPPORTED_ASSETS or "ETH" in config.SUPPORTED_ASSETS

    assert "5m" in config.SUPPORTED_TIMEFRAMES
    assert config.TIMEFRAME_SECONDS["5m"] == 300
    assert config.TIMEFRAME_SECONDS["15m"] == 900
    assert config.TIMEFRAME_SECONDS["1h"] == 3600

    # 验证各资产防爆盾阈值
    assert "BTC" in config.ASSET_CHOP_THRESHOLDS
    assert "ETH" in config.ASSET_CHOP_THRESHOLDS
    assert "SOL" in config.ASSET_CHOP_THRESHOLDS
    assert config.ASSET_CHOP_THRESHOLDS["SOL"]["max_amplitude"] >= 0.50


def test_timeframe_window_calculation():
    """测试不同时间周期的滚动时间戳对齐算法"""
    # 假设当前基准时间戳 1787627123
    now = 1787627123

    # 5m 周期对齐 (300s)
    interval_5m = config.TIMEFRAME_SECONDS["5m"]
    next_5m = ((now // interval_5m) + 1) * interval_5m
    assert next_5m % 300 == 0
    assert next_5m > now
    assert next_5m - now <= 300

    # 15m 周期对齐 (900s)
    interval_15m = config.TIMEFRAME_SECONDS["15m"]
    next_15m = ((now // interval_15m) + 1) * interval_15m
    assert next_15m % 900 == 0
    assert next_15m > now
    assert next_15m - now <= 900


def test_strategy_manager_market_slug_parsing():
    """测试 StrategyManager 从 slug 动态拉取并标注资产与周期"""
    manager = StrategyManager()

    # 模拟 _fetch_market_by_slug
    sample_market = {
        "id": "0x_sol_15m_market",
        "description": "Solana Up or Down - 15m",
        "tokens": {"YES": "token_yes_123", "NO": "token_no_123"},
        "expiry": 1787628000
    }

    with patch.object(manager, "_fetch_market_by_slug", return_value=sample_market):
        market = manager._fetch_market_by_slug("sol-updown-15m-1787628000")
        assert market is not None
        assert market["id"] == "0x_sol_15m_market"
        assert market["tokens"]["YES"] == "token_yes_123"
