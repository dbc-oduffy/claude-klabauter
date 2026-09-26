
from __future__ import annotations

MIN_SAMPLES = 20

DEGRADED_WARM_RATE = 0.5


def emit_warm_engine_health() -> str:
    """Render the ``## Warm engine`` section's single body line, or ``""`` to omit
    the section entirely.

    Returns "" (omit) when: `warm.telemetry` cannot be imported or read, the
    combined warm+cold sample size is below `MIN_SAMPLES`, or the observed warm
    rate is at or above `DEGRADED_WARM_RATE`. Renders a line only when there is
    both enough signal and a real degradation -- the inverse of
    `hook_cancellation_signal.emit_hook_cancellation_rate`'s always-show-once-
    there's-data posture, deliberately: see module docstring.
    """
    try:
        from coordinator_core.warm.telemetry import warm_rate
    except Exception:
        return ""

    try:
        stats = warm_rate()
    except Exception:
        return ""

    rate = stats.get("warm_rate")
    total = stats.get("total", 0)
    if rate is None or total < MIN_SAMPLES:
        return ""
    if rate >= DEGRADED_WARM_RATE:
        return ""

    pct = rate * 100
    warm_count = stats.get("warm_count", 0)
    cold_count = stats.get("cold_count", 0)
    return (
        f"- ⚠ {pct:.1f}% warm ({warm_count}/{total} dispatches; {cold_count} cold) "
        "-- warmth is providing little to no benefit on this clone right now. "
        "Not a diagnosis: `python -c \"from coordinator_core.warm.telemetry import "
        "warm_rate; print(warm_rate())\"` for the raw counts, `warm-engine-stop` "
        "(coordinator/bin/warm-engine-stop.py) to force a fresh generation."
    )
