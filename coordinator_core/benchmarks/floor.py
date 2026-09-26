
from __future__ import annotations

import statistics

from coordinator_core.benchmarks.timer import time_invocation

PING_PARAMS_JSON = "{}"
"""Empty JSON-RPC params object -- `ping` takes no arguments."""


def measure_floor(n: int) -> dict[str, float]:
    samples_ms = [time_invocation("ping", PING_PARAMS_JSON, None) for _ in range(n)]

    cold_start_floor_ms = min(samples_ms)
    mean_ms = statistics.mean(samples_ms)
    if mean_ms <= 0:
        raise ValueError(f"floor.measure_floor: non-positive mean_ms={mean_ms!r} (broken clock?)")
    stdev_ms = statistics.stdev(samples_ms) if len(samples_ms) > 1 else 0.0
    floor_cov = stdev_ms / mean_ms

    return {
        "cold_start_floor_ms": cold_start_floor_ms,
        "floor_cov": floor_cov,
    }
