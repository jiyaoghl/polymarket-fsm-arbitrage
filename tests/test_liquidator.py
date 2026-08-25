import time
import pytest
from polymarket.domain.models import TradeContext, LegPosition
from polymarket.services.liquidator import AdaptiveLiquidatorService

def test_adaptive_ttl_calm_market():
    # 平稳行情，距离到期 300s，TTL 保持基础 90s
    ttl = AdaptiveLiquidatorService.calculate_adaptive_ttl(
        base_ttl=90.0,
        time_to_expiry=300.0,
        asset_amplitude_pct=0.10,
        asset_amplitude_threshold=0.30
    )
    assert ttl == 90.0

def test_adaptive_ttl_high_volatility():
    # 波动率达到阈值 0.30 (ratio = 1.0 >= 0.7)，TTL 应被压缩至 35s
    ttl = AdaptiveLiquidatorService.calculate_adaptive_ttl(
        base_ttl=90.0,
        time_to_expiry=300.0,
        asset_amplitude_pct=0.30,
        asset_amplitude_threshold=0.30
    )
    assert ttl == 35.0

def test_adaptive_ttl_expiry_truncation():
    # 距离到期仅剩 30s，强平时间应截断在交割前 10s (30s - 10s = 20s)
    ttl = AdaptiveLiquidatorService.calculate_adaptive_ttl(
        base_ttl=90.0,
        time_to_expiry=30.0,
        asset_amplitude_pct=0.05,
        asset_amplitude_threshold=0.30
    )
    assert ttl == 20.0

def test_adaptive_ttl_monotonic_decay():
    # 单调递减：持仓期间 dynamic_ttl 已经收紧至 40s，即便后续波动率回落，TTL 也不允许反向扩大
    ttl = AdaptiveLiquidatorService.calculate_adaptive_ttl(
        base_ttl=90.0,
        time_to_expiry=300.0,
        asset_amplitude_pct=0.05,
        asset_amplitude_threshold=0.30,
        current_dynamic_ttl=40.0
    )
    assert ttl == 40.0

def test_evaluate_timeout():
    now = time.time()
    ctx = TradeContext(
        market_id="m1",
        end_time=now + 300,
        leg1=LegPosition(order_id="o1", token="t1", cost=0.4, size=10.0),
        leg1_filled_time=now - 95.0  # 已持有 95 秒
    )
    is_timed_out, elapsed, ttl = AdaptiveLiquidatorService.evaluate_timeout(ctx, base_ttl=90.0)
    assert is_timed_out is True
    assert elapsed >= 95.0
    assert ttl == 90.0
