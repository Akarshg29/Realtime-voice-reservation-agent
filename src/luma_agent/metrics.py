"""Lightweight latency metrics.

Records named millisecond samples and reports count / p50 / p95 / avg / max.
Used for two families of measurement:

  * ``api.*``  — reservation API round-trip latency (measured in api_client)
  * ``turn.*`` — voice turn latency, e.g. ``turn.eos_to_first_audio``
                 (end of user speech -> first byte of bot audio), captured
                 from Pipecat metrics frames in bot.py.
"""

from __future__ import annotations

import time
from collections import defaultdict
from contextlib import contextmanager
from statistics import mean
from typing import Iterator


def _percentile(samples: list[float], pct: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(ordered) - 1)
    frac = k - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


class LatencyRecorder:
    def __init__(self) -> None:
        self._samples: dict[str, list[float]] = defaultdict(list)

    def record(self, name: str, ms: float) -> None:
        self._samples[name].append(ms)

    @contextmanager
    def timer(self, name: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            self.record(name, (time.perf_counter() - start) * 1000.0)

    def names(self) -> list[str]:
        return sorted(self._samples)

    def summary(self, name: str) -> dict[str, float]:
        s = self._samples.get(name, [])
        return {
            "count": len(s),
            "p50_ms": round(_percentile(s, 0.50), 1),
            "p95_ms": round(_percentile(s, 0.95), 1),
            "avg_ms": round(mean(s), 1) if s else 0.0,
            "max_ms": round(max(s), 1) if s else 0.0,
        }

    def report(self) -> dict[str, dict[str, float]]:
        return {name: self.summary(name) for name in self.names()}

    def reset(self) -> None:
        self._samples.clear()
