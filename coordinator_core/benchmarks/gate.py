"""Gate verdict — pure function mapping (observed_statistic, target, tolerance, N) -> verdict.

Spec backlink: docs/plans/2026-07-10-qsub-01-latency-benchmark-harness.md § C2 (AC3).

This module is a deliberate leaf: zero I/O, no imports from record.py or any other
benchmarks module. A verdict must be recomputable from the four scalar inputs alone,
so any consumer (harness, CI gate, a human re-checking a stored ConformanceRecord)
can re-derive 'pass'/'fail'/'advisory' offline without re-running anything.

Gating discipline (do NOT deviate — see plan Anti-scope):
- Gates on the RAW end-to-end band. There is no floor subtraction anywhere in this
  module — floor_delta_ms is advisory-only and lives in record.py, never here.
- Gates on the `min` statistic in practice (the caller passes whichever statistic it
  wants gated; this function does not care which one it is, but the harness's
  intended contract is min-vs-target, not p50-vs-target — p50 over small N is a
  coin-flip).
"""

from __future__ import annotations

# Canonical verdict strings — the return contract. Any consumer of evaluate() may
# import these instead of hardcoding the literals.
VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_ADVISORY = "advisory"


def _tolerance_field(tolerance, field: str):
    """Read a field off `tolerance` regardless of whether it's an object (.kind/.value)
    or a dict ({'kind': ..., 'value': ...}) — keeps this module decoupled from
    whatever concrete tolerance type record.py (C1) defines.
    """
    if isinstance(tolerance, dict):
        return tolerance[field]
    return getattr(tolerance, field)


def evaluate(
    observed_statistic: float,
    target_ms: float,
    tolerance,
    sample_count: int,
    min_gating_sample_count: int,
) -> str:
    """Return 'pass' | 'fail' | 'advisory' for one op's observed latency statistic.

    Rules (AC3):
    1. sample_count < min_gating_sample_count -> 'advisory', regardless of the
       statistic's value — an underpowered sample never gates green or red.
    2. tolerance.kind == 'relative' -> band = target_ms * (1 + value);
       tolerance.kind == 'absolute' -> band = target_ms + value.
    3. observed_statistic <= band -> 'pass', else 'fail'. No floor subtraction.
    """
    if sample_count < min_gating_sample_count:
        return VERDICT_ADVISORY

    kind = _tolerance_field(tolerance, "kind")
    value = _tolerance_field(tolerance, "value")

    if kind == "relative":
        band = target_ms * (1 + value)
    elif kind == "absolute":
        band = target_ms + value
    else:
        raise ValueError(f"unknown tolerance.kind: {kind!r} (expected 'relative' or 'absolute')")

    return VERDICT_PASS if observed_statistic <= band else VERDICT_FAIL
