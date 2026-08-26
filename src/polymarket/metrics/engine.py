import time
import threading
from typing import Dict, Optional, Any, Union
from polymarket.metrics.types import Counter, Gauge, Histogram
from polymarket.logger import logger

class _TimerContext:
    """同时支持同步 (with) 与异步 (async with) 的计时上下文管理器"""
    def __init__(self, histogram: Histogram, labels: Optional[Dict[str, str]] = None):
        self.histogram = histogram
        self.labels = labels
        self.start_ts = 0.0

    def __enter__(self):
        self.start_ts = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        duration = time.perf_counter() - self.start_ts
        self.histogram.observe(duration, self.labels)

    async def __aenter__(self):
        self.start_ts = time.perf_counter()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        duration = time.perf_counter() - self.start_ts
        self.histogram.observe(duration, self.labels)


class MetricsEngine:
    """
    轻量级内部时序指标引擎 (Internal Metrics Engine)。
    以单例模式运行，支持纳秒级极速打点与结构化导出。
    """
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(MetricsEngine, cls).__new__(cls)
                cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        with self._lock:
            if self._initialized:
                return

            self._counters: Dict[str, Counter] = {}
            self._gauges: Dict[str, Gauge] = {}
            self._histograms: Dict[str, Histogram] = {}

            # 初始化核心量化交易指标
            self.orders_total = self.register_counter("poly_orders_total", "累计下单总数")
            self.trades_locked_total = self.register_counter("poly_trades_locked_total", "累计套利锁仓成交数")
            self.liquidations_total = self.register_counter("poly_liquidations_total", "累计强平触发数")
            self.api_errors_total = self.register_counter("poly_api_errors_total", "API 异常与限流拦截次数")

            self.balance_usdc = self.register_gauge("poly_balance_usdc", "账户抵押品余额")
            self.unhedged_exposure_usdc = self.register_gauge("poly_unhedged_exposure_usdc", "未对冲单边敞口金额")
            self.active_positions = self.register_gauge("poly_active_positions", "各状态活跃持仓数")
            self.active_tasks = self.register_gauge("poly_active_tasks", "异步运行时活跃协程数")

            self.order_latency_seconds = self.register_histogram("poly_order_latency_seconds", "下单请求往返耗时分布")
            self.tick_process_latency_seconds = self.register_histogram("poly_tick_process_latency_seconds", "Tick 状态机处理耗时分布")

            self._initialized = True
            logger.info("[MetricsEngine] 轻量级时序指标引擎初始化完毕。")

    @classmethod
    def get_instance(cls) -> "MetricsEngine":
        return cls()

    def register_counter(self, name: str, description: str = "") -> Counter:
        c = Counter(name, description)
        self._counters[name] = c
        return c

    def register_gauge(self, name: str, description: str = "") -> Gauge:
        g = Gauge(name, description)
        self._gauges[name] = g
        return g

    def register_histogram(self, name: str, description: str = "") -> Histogram:
        h = Histogram(name, description)
        self._histograms[name] = h
        return h

    def timer(self, histogram_or_name: Union[str, Histogram], labels: Optional[Dict[str, str]] = None) -> _TimerContext:
        """
        双模自动耗时捕获上下文管理器 (支持 with 与 async with)。
        """
        if isinstance(histogram_or_name, str):
            hist = self._histograms.get(histogram_or_name)
            if not hist:
                hist = self.register_histogram(histogram_or_name)
        else:
            hist = histogram_or_name
        return _TimerContext(hist, labels)

    def export_dashboard_json(self) -> Dict[str, Any]:
        """导出结构化 JSON 供 Dashboard 前端图表消费"""
        return {
            "timestamp": time.time(),
            "counters": {
                name: c.get_all() for name, c in self._counters.items()
            },
            "gauges": {
                name: g.get_all() for name, g in self._gauges.items()
            },
            "histograms": {
                name: h.get_all() for name, h in self._histograms.items()
            }
        }
