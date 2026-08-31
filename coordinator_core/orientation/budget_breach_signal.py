"""
coordinator_core.orientation.budget_breach_signal — over-bar op probe for the orientation cache.

Purpose: `ops/op_budget_breaches.py` is a correctly wired instrument — it reads the
op-latency sink, classifies the three breach kinds, ranks by box occupancy past
the bar (WALL CLOCK -- see `headline_for`, which names its own unit and why), and
`headline_for` already renders an operator-facing line — but as of
2026-08-26 nothing outside its own test file calls it. No hook, no ceremony, no
cadence. It is a query surface waiting to be asked a question nobody asks.

What that cost, measured rather than supposed: `ceremony.commit` timed out on 28 of
31 calls for at least 28 hours across this box, and the failure was found by
accident, while someone was measuring drain length for an unrelated baton. Running
`op_census.breaches` by hand against the live sink named it instantly — 97% breach
rate, 50s stolen — an answer that had been available the entire time. The op was
built and never wired to a bell. This module is the bell.

Same gap, same shape, same repo as `warm_health_signal.py`, whose docstring records
the identical failure one instrument over ("the data was being recorded into a file
nobody opened"). Two independent occurrences make the pattern the point: an
instrument nobody calls is indistinguishable from an instrument that was never
built, and the second one is cheaper to notice.

Posture follows `warm_health_signal`, not `hook_cancellation_signal`: a healthy box
renders NOTHING here, every session, forever. An op over the bar is a defect under
DR-344's kill bar, not an accepted residual, so a standing line would train the eye
to skip it. Only a breach with enough attempts behind it to mean something earns
the line — see `emit_budget_breaches` for the two thresholds and why they sit where
they do.

Cold, orientation-regen-only, matching every other `emit_*` helper in
`regenerate_cache.py` — never a `PreToolUse` or dispatch hot path. One bounded tail
read of the current sink generation (`op_budget_breaches.MAX_TAIL_BYTES`, 6MB), no
subprocess, no lock. Measured at 77-118ms process time on a 21MB sink, inside the
brightline for a cadence path.

Fail-open throughout: an unreadable sink, a missing generation, an import failure,
or any exception at all resolves to "" and the section is omitted — matching every
omit-when-empty section in `regenerate_cache.py`. A repo that has never dispatched
is indistinguishable from a healthy one BY DESIGN; "no signal yet" is not the
failure mode this module exists to catch.

Negative-spec:
  - Does NOT re-implement the tally, the bar, or the breach classification. Every
    number comes from `op_budget_breaches.breach_report`, and the rendered text from
    its `headline_for` — a second implementation would drift from the op, and the
    op is the authority DR-344 names.
  - Does NOT raise the bar, soften a breach into a warning, or suggest waiting.
    `headline_for`'s own register rule ("never a timeout: an op is not made correct
    by the caller waiting longer for it") governs the text this module emits.
"""

from __future__ import annotations

from pathlib import Path

#: Attempts an op needs before its breach rate is allowed to speak. A cold clone's
#: first few dispatches routinely land over the bar (imports, page cache, a warm
#: server not yet elected) and say nothing about the op. 10 is the same order as
#: `warm_health_signal.MIN_SAMPLES` (20) scaled to a PER-OP count rather than a
#: whole-clone one: it is reachable inside one working session for any op a session
#: actually uses, and it is above the 1-2 attempt noise that dominates the tail of
#: any real sink. Not a measured constant -- `ceremony.commit` cleared it 3x over at
#: n=31 while every METHOD_NOT_FOUND straggler in the same window sat at n=2.
MIN_ATTEMPTS = 10

#: Breach rate at which an op stops being occasionally-unlucky and becomes a defect
#: an operator should see at boot. Deliberately well under a half: DR-344's bar is a
#: kill bar, so an op breaching a quarter of the time is already failing its
#: contract for one caller in four, and the two live examples this module was built
#: against sat far above it (`ceremony.commit` 34/35, `push.outstanding` 9/35). Set
#: low because the cost of a false line is one ignorable boot message and the cost
#: of a missed one is 28 hours of a broken commit path nobody looked at.
BREACH_RATE = 0.25


def emit_budget_breaches(repo_root: Path) -> str:
    """Render the ``## Budget breaches`` section's single body line, or ``""`` to
    omit the section entirely.

    Returns "" (omit) when: `op_budget_breaches` cannot be imported or read, no op
    is over the bar, or no breaching op clears BOTH `MIN_ATTEMPTS` and
    `BREACH_RATE`. Renders `headline_for`'s line only when an op is failing its
    budget often enough, and on enough evidence, to be worth a boot message.

    Never raises.
    """
    try:
        from coordinator_core.ops.op_budget_breaches import breach_report, headline_for
    except Exception:
        return ""

    try:
        summary = breach_report(repo_root=repo_root, top_n=5)
    except Exception:
        return ""

    try:
        ops = summary.get("ops") or ()
        qualifying = [
            row
            for row in ops
            if (row.get("attempts") or 0) >= MIN_ATTEMPTS
            and (row.get("breach_rate") or 0.0) >= BREACH_RATE
        ]
        if not qualifying:
            return ""
        return "- ⚠ " + headline_for(summary)
    except Exception:
        return ""
