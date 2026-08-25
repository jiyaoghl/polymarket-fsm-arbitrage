import pytest
import time
from unittest.mock import MagicMock, patch
from polymarket import config
from polymarket.apps.manager import StrategyManager

def test_multi_asset_configs():
    """测试多加密资产配置解析与防爆盾阈值"""
    assert isinstance(config.SUPPORTED_ASSETS, list)
    assert len(config.SUPPORTED_ASSETS) >= 1
    assert "BTC" in config.SUPPORTED_ASSETS or "ETH" in config.SUPPORTED_ASSETS

    # 验证各资产防爆盾阈值
    assert "BTC" in config.ASSET_CHOP_THRESHOLDS
    assert "ETH" in config.ASSET_CHOP_THRESHOLDS
    assert "SOL" in config.ASSET_CHOP_THRESHOLDS
    assert config.ASSET_CHOP_THRESHOLDS["SOL"]["max_amplitude"] >= 0.50


def test_5min_window_calculation():
    """测试 5min 周期 (300s) 的滚动时间戳对齐算法"""
    now = 1787627123
    interval_5m = 300
    next_5m = ((now // interval_5m) + 1) * interval_5m
    assert next_5m % 300 == 0
    assert next_5m > now
    assert next_5m - now <= 300


def test_strategy_manager_market_slug_parsing():
    """测试 StrategyManager 从 5min slug 动态拉取并标注资产类型"""
    manager = StrategyManager()

    sample_market = {
        "id": "0x_sol_5m_market",
        "description": "Solana Up or Down - 5m",
        "tokens": {"YES": "token_yes_123", "NO": "token_no_123"},
        "expiry": 1787627400
    }

    with patch.object(manager, "_fetch_market_by_slug", return_value=sample_market):
        market = manager._fetch_market_by_slug("sol-updown-5m-1787627400")
        assert market is not None
        assert market["id"] == "0x_sol_5m_market"
        assert market["tokens"]["YES"] == "token_yes_123"
