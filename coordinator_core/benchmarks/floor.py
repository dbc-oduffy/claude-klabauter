"""
coordinator_core.benchmarks.floor -- Cold-start floor probe.

Purpose: measures the DR-215 spawn-per-call cold-start floor by timing `n`
bare (no `--repo`) invocations of the `ping` op via the shared timing
primitive `coordinator_core.benchmarks.timer.time_invocation` -- this module
does not duplicate the spawn-to-exit child-process loop, it only draws
samples from it and reduces them to a floor statistic + stability metric.

Floor statistic: `min` across the n draws, matching the `min`-based
`gating_statistic` convention used elsewhere in this harness (see gate.py) --
`min` is the least-perturbed-by-scheduler-noise draw and is what op-level
gating already keys off, so the floor uses the same convention rather than
introducing a second one.

floor_delta_ms (downstream, NOT computed here): each op's `ConformanceRecord`
(record.py) computes `floor_delta_ms` = the op's own end-to-end gating
statistic minus this module's `cold_start_floor_ms`. That subtraction is
SIGNED and advisory-only -- it is never a gate input (gate.py gates on the
raw end-to-end band, no floor subtraction anywhere in that module). A
negative `floor_delta_ms` is a VALID outcome, not a data error: it just means
that particular op's draw landed faster than this run's floor draw.

Variance caveat: `floor_delta_ms` combines two independently-noisy draws (the
op's own end-to-end sample and this module's floor sample). Empirically each
draw carries roughly ~1.57ms sigma under DR-215's spawn-per-call model, and
variances add under subtraction of independent draws, so the resulting delta
carries roughly ~2.2ms sigma (sqrt(1.57^2 + 1.57^2) ~= 2.22). Do not treat a
small negative or small positive `floor_delta_ms` as significant on its own --
it is well within this combined-draw noise band.

Spec backlink: pln-qsub-01-per-op-end-to-end-late-53ff10 § C5
"""

from __future__ import annotations

from coordinator_core.benchmarks import declare_benchmark_origin

import statistics

from coordinator_core.benchmarks.timer import time_invocation

PING_PARAMS_JSON = "{}"
"""Empty JSON-RPC params object -- `ping` takes no arguments."""


def measure_floor(n: int) -> dict[str, float]:
    """
    Draws n bare (repo=None) `ping` samples via `time_invocation` and reduces
    them to the run-level cold-start floor statistic + its stability metric.

    Returns {"cold_start_floor_ms": float, "floor_cov": float} where
    cold_start_floor_ms is min(samples) (see module docstring for why `min`)
    and floor_cov is the coefficient of variation (stdev/mean) across the n
    raw samples, characterizing how noisy this run's floor draw was.

    Propagates BenchmarkSampleInvalid (via time_invocation) unmodified on any
    invalid sample -- a floor draw containing an erroring/non-zero-exit ping
    invocation is not silently dropped or substituted.
    """
    declare_benchmark_origin()
    samples_ms = [time_invocation("ping", PING_PARAMS_JSON, None) for _ in range(n)]

    cold_start_floor_ms = min(samples_ms)
    mean_ms = statistics.mean(samples_ms)
    # Review: code-reviewer (Slice B F4, nit) — a zero-or-negative mean spawn-
    # to-exit wall-clock time across N real subprocess invocations is not a
    # legitimate benchmark outcome; it signals a broken clock/harness, not a
    # benign "perfectly stable floor." Fail loud instead of silently
    # substituting floor_cov=0.0.
    if mean_ms <= 0:
        raise ValueError(f"floor.measure_floor: non-positive mean_ms={mean_ms!r} (broken clock?)")
    stdev_ms = statistics.stdev(samples_ms) if len(samples_ms) > 1 else 0.0
    floor_cov = stdev_ms / mean_ms

    return {
        "cold_start_floor_ms": cold_start_floor_ms,
        "floor_cov": floor_cov,
    }
