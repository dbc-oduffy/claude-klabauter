"""
coordinator_core.orientation.warm_health_signal — warm-engine cold-fallback rate probe.

Purpose: `warm.telemetry.warm_rate()` and `client_cold_count()` are correctly wired
instruments — server lives flush warm/cold counts on shutdown
(`warm.server._ServerContext.telemetry.flush`), and every client-observed cold
fallback is appended by `warm.client._record_cold_fallback` — but as of 2026-08-21
nothing anywhere calls either reader: no CLI, no `ops/` op, no doctor surface. The
data was being recorded into a file nobody opened. Pulled by hand for the first
time that day, it showed a median 337-second server life, 55% of generations
killed by engine skew, and 6 generations that served zero requests before dying —
none of which had ever been loud. This module is the fix for THAT gap specifically:
making the existing instrument readable without anyone going looking, not a new
diagnosis of why servers churn (that is a separate, in-flight effort — see
`state/handoffs/` for the churn investigation this module deliberately does not
duplicate).

Mirrors `hook_cancellation_signal.py`'s shape (a single rate, a single rendered
line, omit-when-undefined) but NOT its "always show once there's data" posture:
that module's rate is an accepted residual (DR-310) meant to stay visible as a
standing fact. This module's rate is the opposite kind of signal — warmth existing
at all is supposed to make it good, so a healthy box should render NOTHING here,
every session, forever. Only a rate that says the mechanism the engine is paying
for isn't delivering earns a line. See `emit_warm_engine_health`'s own docstring
for the threshold and why it is set where it is.

Self-scoped, not repo-scoped: warm telemetry is keyed to the ENGINE CLONE this
interpreter is running from (`warm.breadcrumb.svc_dir`'s own per-clone-hash
directory), not to whichever repo's orientation cache is being regenerated — the
same distinction `warm.client`/`warm.server` draw throughout. Every call here
therefore takes no `repo_root` argument and reads `warm.telemetry.warm_rate()` at
its default (this process's own resolved clone).

Cold, orientation-regen-only call, matching every other `emit_*` helper in
`regenerate_cache.py` — never runs on a `PreToolUse` or dispatch hot path. Two
plain file reads (`telemetry.jsonl`, `client-cold.jsonl`), no subprocess, no lock
wait beyond `warm.telemetry`'s own — cheap enough for a session-start path per
`CLAUDE.md`'s brightline (process time, not wall clock, is the axis that matters
here, and this pays none).

Fail-open throughout: a missing/unreadable telemetry file, an undefined rate (zero
samples), or any exception from the `warm.telemetry` import itself all resolve to
"" (section omitted), matching every other omit-when-empty section in
`regenerate_cache.py`. A fresh clone that has never dispatched through the warm
path is indistinguishable from a healthy one here BY DESIGN — "no signal yet" is
not the failure mode this module exists to catch.
"""

from __future__ import annotations

#: Below this many total observed outcomes (warm + cold, across every recorded
#: server life plus every recorded client-side cold fallback), the rate is too
#: thin to mean anything -- a clone's first few dispatches after a cold boot are
#: expected to be cold (nothing has spawned a server yet) and are not, on their
#: own, a degraded-engine signal. 20 is not a measured constant (no comparable
#: prior threshold exists in this module's precedent, `hook_cancellation_signal`,
#: which has no floor at all because its rate is meant to render unconditionally)
#: -- it is picked to average past first-boot noise while staying reachable
#: within a single working session, which multiple dispatches per minute make
#: trivial once warmth is actually in play.
MIN_SAMPLES = 20

#: Below this fraction warm, the mechanism is providing marginal-to-negative
#: value while still paying its own overhead (spawn, election, pipe open) on
#: every miss. `warm.telemetry`'s own docstring cites ~130 served invocations
#: per 2.1-minute server life as what warmth paying off looks like -- the
#: median case there is overwhelmingly warm. 0.5 is the loosest threshold that
#: still means "most calls are going cold": below half warm, an operator
#: reading this line should not need a second number to know something is
#: wrong, whatever the underlying cause (see this module's own docstring for
#: why diagnosing the cause is explicitly out of scope here).
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
