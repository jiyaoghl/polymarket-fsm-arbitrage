import time
import asyncio
import pytest
from fastapi.testclient import TestClient

from polymarket.metrics import metrics, MetricsEngine, Counter, Gauge, Histogram
from polymarket.apps.dashboard import app

def test_counter_increment_and_labels():
    """测试 Counter 累计计数器与标签隔离"""
    c = Counter("test_orders_total", "测试订单数")
    assert c.get() == 0.0
    
    # 无标签递增
    c.inc()
    assert c.get() == 1.0
    
    # 带标签递增
    c.inc(2.0, labels={"strategy": "test_strat", "side": "BUY"})
    assert c.get(labels={"strategy": "test_strat", "side": "BUY"}) == 2.0
    assert c.get() == 1.0  # 无标签不污染
    
    # 负数异常保护
    with pytest.raises(ValueError):
        c.inc(-1.0)


def test_gauge_set_inc_dec():
    """测试 Gauge 瞬时仪表盘设置与增减"""
    g = Gauge("test_balance_usdc", "测试余额")
    g.set(100.0)
    assert g.get() == 100.0
    
    g.inc(50.0)
    assert g.get() == 150.0
    
    g.dec(30.0)
    assert g.get() == 120.0
    
    # 标签隔离
    g.set(500.0, labels={"account": "sub1"})
    assert g.get(labels={"account": "sub1"}) == 500.0
    assert g.get() == 120.0


def test_histogram_observe_and_percentiles():
    """测试 Histogram 延迟分桶与 P50/P90/P99 百分位数"""
    h = Histogram("test_latency_seconds", "测试延迟")
    
    # 插入一批模拟延迟样本 (0.01s ~ 0.10s)
    for v in [0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.09, 0.10]:
        h.observe(v)
        
    summary = h.get_summary()
    assert summary["count"] == 10
    assert summary["sum"] == pytest.approx(0.55, rel=1e-3)
    assert summary["avg"] == pytest.approx(0.055, rel=1e-3)
    assert summary["p50"] == 0.06
    assert summary["p90"] == 0.10


def test_metrics_timer_sync_and_async():
    """测试 metrics.timer 同步与异步双模自动耗时捕获"""
    # 1. 同步计时
    with metrics.timer("test_sync_timer", labels={"mode": "sync"}):
        time.sleep(0.01)
        
    hist = metrics._histograms["test_sync_timer"]
    summ = hist.get_summary(labels={"mode": "sync"})
    assert summ["count"] == 1
    assert summ["sum"] >= 0.008

    # 2. 异步计时
    async def _async_work():
        async with metrics.timer("test_async_timer", labels={"mode": "async"}):
            await asyncio.sleep(0.01)
            
    asyncio.run(_async_work())
    hist_async = metrics._histograms["test_async_timer"]
    summ_async = hist_async.get_summary(labels={"mode": "async"})
    assert summ_async["count"] == 1
    assert summ_async["sum"] >= 0.008


def test_metrics_export_dashboard_json():
    """测试 MetricsEngine 结构化 JSON 导出"""
    data = metrics.export_dashboard_json()
    assert "timestamp" in data
    assert "counters" in data
    assert "gauges" in data
    assert "histograms" in data
    assert "poly_orders_total" in data["counters"]
    assert "poly_balance_usdc" in data["gauges"]
    assert "poly_order_latency_seconds" in data["histograms"]


def test_dashboard_api_metrics_endpoint():
    """测试 FastAPI /api/metrics 内部监控端点"""
    client = TestClient(app)
    resp = client.get("/api/metrics")
    assert resp.status_code == 200
    json_data = resp.json()
    assert "counters" in json_data
    assert "gauges" in json_data
    assert "histograms" in json_data
