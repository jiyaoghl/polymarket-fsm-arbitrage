from polymarket.metrics.types import Counter, Gauge, Histogram
from polymarket.metrics.engine import MetricsEngine

# 全局打点便捷单例
metrics = MetricsEngine.get_instance()

__all__ = [
    "metrics",
    "MetricsEngine",
    "Counter",
    "Gauge",
    "Histogram",
]
