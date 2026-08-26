import time
import bisect
import threading
from typing import Dict, List, Optional, Tuple, Any
from collections import deque

def format_labels(labels: Optional[Dict[str, str]]) -> str:
    """标准化序列化标签字典为唯一字符串键"""
    if not labels:
        return ""
    return ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))


class Counter:
    """
    轻量级累计计数器 (Counter)。
    支持多维标签隔离与纳秒级递增。
    """
    def __init__(self, name: str, description: str = ""):
        self.name = name
        self.description = description
        self._values: Dict[str, float] = {}
        self._labels_map: Dict[str, Dict[str, str]] = {}
        self._lock = threading.Lock()

    def inc(self, amount: float = 1.0, labels: Optional[Dict[str, str]] = None) -> None:
        """递增计数"""
        if amount < 0:
            raise ValueError("Counter 递增量不能为负数")
        lbl_key = format_labels(labels)
        with self._lock:
            self._values[lbl_key] = self._values.get(lbl_key, 0.0) + amount
            if lbl_key not in self._labels_map:
                self._labels_map[lbl_key] = dict(labels) if labels else {}

    def get(self, labels: Optional[Dict[str, str]] = None) -> float:
        """获取指定标签下的计数值"""
        lbl_key = format_labels(labels)
        with self._lock:
            return self._values.get(lbl_key, 0.0)

    def get_all(self) -> List[Dict[str, Any]]:
        """获取当前指标下所有标签组的详细数据"""
        with self._lock:
            return [
                {"labels": self._labels_map[k], "value": val}
                for k, val in self._values.items()
            ]


class Gauge:
    """
    轻量级瞬时仪表盘 (Gauge)。
    用于表示账户余额、未对冲敞口、活跃协程数等可增可减的瞬时指标。
    """
    def __init__(self, name: str, description: str = ""):
        self.name = name
        self.description = description
        self._values: Dict[str, float] = {}
        self._labels_map: Dict[str, Dict[str, str]] = {}
        self._lock = threading.Lock()

    def set(self, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        """设置瞬时值"""
        lbl_key = format_labels(labels)
        with self._lock:
            self._values[lbl_key] = float(value)
            if lbl_key not in self._labels_map:
                self._labels_map[lbl_key] = dict(labels) if labels else {}

    def inc(self, amount: float = 1.0, labels: Optional[Dict[str, str]] = None) -> None:
        """增加瞬时值"""
        lbl_key = format_labels(labels)
        with self._lock:
            self._values[lbl_key] = self._values.get(lbl_key, 0.0) + amount
            if lbl_key not in self._labels_map:
                self._labels_map[lbl_key] = dict(labels) if labels else {}

    def dec(self, amount: float = 1.0, labels: Optional[Dict[str, str]] = None) -> None:
        """减少瞬时值"""
        self.inc(-amount, labels)

    def get(self, labels: Optional[Dict[str, str]] = None) -> float:
        lbl_key = format_labels(labels)
        with self._lock:
            return self._values.get(lbl_key, 0.0)

    def get_all(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                {"labels": self._labels_map[k], "value": val}
                for k, val in self._values.items()
            ]


class Histogram:
    """
    轻量级耗时直方图 (Histogram)。
    预设固定分桶，支持样本均值、总数与 P50/P90/P99 百分位数统计。
    """
    DEFAULT_BUCKETS = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

    def __init__(
        self,
        name: str,
        description: str = "",
        buckets: Tuple[float, ...] = DEFAULT_BUCKETS,
        max_samples_retained: int = 500
    ):
        self.name = name
        self.description = description
        self.buckets = tuple(sorted(buckets))
        self.max_samples = max_samples_retained
        
        self._counts: Dict[str, int] = {}
        self._sums: Dict[str, float] = {}
        self._bucket_counts: Dict[str, List[int]] = {}
        self._recent_samples: Dict[str, deque] = {}
        self._labels_map: Dict[str, Dict[str, str]] = {}
        self._lock = threading.Lock()

    def observe(self, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        """记录一次耗时/采样值 (秒)"""
        val = float(value)
        lbl_key = format_labels(labels)
        
        with self._lock:
            if lbl_key not in self._bucket_counts:
                self._bucket_counts[lbl_key] = [0] * len(self.buckets)
                self._counts[lbl_key] = 0
                self._sums[lbl_key] = 0.0
                self._recent_samples[lbl_key] = deque(maxlen=self.max_samples)
                self._labels_map[lbl_key] = dict(labels) if labels else {}

            self._counts[lbl_key] += 1
            self._sums[lbl_key] += val
            self._recent_samples[lbl_key].append(val)

            # 二分查找快速命中分桶
            idx = bisect.bisect_right(self.buckets, val)
            for i in range(idx, len(self.buckets)):
                self._bucket_counts[lbl_key][i] += 1

    def get_summary(self, labels: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """获取指定标签维度的统计摘要 (包含 P50/P90/P99 延迟)"""
        lbl_key = format_labels(labels)
        with self._lock:
            count = self._counts.get(lbl_key, 0)
            total_sum = self._sums.get(lbl_key, 0.0)
            samples = list(self._recent_samples.get(lbl_key, []))

        if not samples:
            return {"count": 0, "sum": 0.0, "avg": 0.0, "p50": 0.0, "p90": 0.0, "p99": 0.0}

        sorted_samples = sorted(samples)
        n = len(sorted_samples)
        p50 = sorted_samples[int(n * 0.50)]
        p90 = sorted_samples[min(int(n * 0.90), n - 1)]
        p99 = sorted_samples[min(int(n * 0.99), n - 1)]
        avg = total_sum / count if count > 0 else 0.0

        return {
            "count": count,
            "sum": round(total_sum, 6),
            "avg": round(avg, 6),
            "p50": round(p50, 6),
            "p90": round(p90, 6),
            "p99": round(p99, 6),
        }

    def get_all(self) -> List[Dict[str, Any]]:
        """获取所有标签组的摘要"""
        with self._lock:
            keys = list(self._counts.keys())
        return [
            {"labels": self._labels_map[k], "summary": self.get_summary(self._labels_map[k])}
            for k in keys
        ]
