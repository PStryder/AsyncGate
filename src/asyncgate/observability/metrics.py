"""Lightweight metrics registry for AsyncGate."""

from dataclasses import dataclass
from threading import Lock
from typing import Any


@dataclass
class Counter:
    value: float = 0.0

    def inc(self, amount: float = 1.0) -> None:
        self.value += amount


@dataclass
class Gauge:
    value: float = 0.0

    def set(self, value: float) -> None:
        self.value = value


@dataclass
class Histogram:
    count: int = 0
    total: float = 0.0
    minimum: float | None = None
    maximum: float | None = None

    def observe(self, value: float) -> None:
        self.count += 1
        self.total += value
        if self.minimum is None or value < self.minimum:
            self.minimum = value
        if self.maximum is None or value > self.maximum:
            self.maximum = value

    def snapshot(self) -> dict[str, Any]:
        average = self.total / self.count if self.count else 0.0
        return {
            "count": self.count,
            "total": self.total,
            "min": self.minimum,
            "max": self.maximum,
            "avg": average,
        }


class MetricsRegistry:
    """Thread-safe registry for counters, gauges, and histograms."""

    def __init__(self) -> None:
        self._lock = Lock()
        self.counters: dict[str, Counter] = {}
        self.gauges: dict[str, Gauge] = {}
        self.histograms: dict[str, Histogram] = {}

    def inc_counter(self, name: str, amount: float = 1.0) -> None:
        with self._lock:
            counter = self.counters.setdefault(name, Counter())
            counter.inc(amount)

    def set_gauge(self, name: str, value: float) -> None:
        with self._lock:
            gauge = self.gauges.setdefault(name, Gauge())
            gauge.set(value)

    def observe(self, name: str, value: float) -> None:
        with self._lock:
            histogram = self.histograms.setdefault(name, Histogram())
            histogram.observe(value)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "counters": {name: counter.value for name, counter in self.counters.items()},
                "gauges": {name: gauge.value for name, gauge in self.gauges.items()},
                "histograms": {name: hist.snapshot() for name, hist in self.histograms.items()},
            }


metrics = MetricsRegistry()
