
from __future__ import annotations

VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_ADVISORY = "advisory"


def _tolerance_field(tolerance, field: str):
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
