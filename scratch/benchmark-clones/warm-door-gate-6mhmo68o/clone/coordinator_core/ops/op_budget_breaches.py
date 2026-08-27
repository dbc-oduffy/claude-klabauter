"""coordinator_core.ops.op_budget_breaches — "op_census.breaches" JSON-RPC op.

Purpose: make an over-budget op FINDABLE so it can be deleted. Today a breach
is recorded to `op-latency.jsonl` and nothing surfaces it — it is invisible
unless somebody goes looking, which is what lets a slow op survive by being
handed a larger timeout instead of being removed. This op answers, in one
bounded read: which ops blew the brightline, how often, how badly, when first
and last seen, and which way they are trending.

Sibling of `op_census.report`, not an extension of it — the justification:
`census()`'s dominant cost is a sha256-plus-AST scan of the whole non-test
`coordinator_core/` tree (~125ms measured, scaling with corpus BYTES). A
breach view needs none of it; folding this in would make every breach query
pay that scan, and would put a telemetry question behind `census()`'s
`CorpusIdentityError` refusal, which is about the SOURCE TREE and has nothing
to say about a sink. The reverse direction is free and is taken:
`census()` already reads the same telemetry rows, so it now carries a compact
breach headline computed over rows it has in hand — one extra pass, no second
read.

Ranking is by damage to the shared box, not by raw count — see
`op_latency.breach_summary` for `stolen_ms` and why a single 30s breach
outranks fifty 520ms ones at this machine's load norm (~50 concurrent
sessions queued behind whichever op is holding the box).

The bar is DR-344's 500ms brightline, taken from
`coordinator_core.op_census.timing.PROCESS_TIME_BAR_MS` — the one place it is
stated. It is NOT the per-op caller timeout: a caller timeout is a dial
somebody chose, so measuring against it makes an op compliant by having been
given more grace, which is the exact habit this surface exists to end.
`_op_budget_breaches` REFUSES a caller-supplied `bar_ms` for the same reason
(see `_read_params`).

Negative-spec:
    - Reads ONLY the current (newest) op-latency generation
      (`op_latency.sink_generations(...)[:1]`), matching
      `op_census_report._read_current_generation_entries`'s own rule. Never
      walks the whole corpus: the rotated generations here are 26MB and 52MB.
    - Bounded by BYTES from the END (`MAX_TAIL_BYTES`), not by a row cap set
      above the live row count. `source.head_truncated` and
      `source.rows_capped` say plainly when either bound bit — a bounded read
      must never render as a whole-population figure. A DIRECTION is held to
      the same rule and not merely annotated: on a truncated read every
      `trend` reads `TREND_WINDOW_LIMITED`, because both of `_trend`'s halves
      then sit inside the tail and a rise older than the window renders as
      "flat". See `_tail_entries` for
      why `engine_report.iter_sink_entries`'s oldest-first row cap is the
      wrong bound here and why reading raw rows there carries no semantics.
    - Process time, never wall clock, in its own self-assessment (DR-344
      constraint: wall clock measures peer load, not this op).
    - Reports its own breach as a breach. `self_assessment.under_per_process_bar`
      is emitted unhedged; this op is subject to the brightline like every
      other (see `op_census_report`'s same discipline).
    - Produces evidence, never a verdict about what to delete — the kill
      disposition is the reader's, exactly as `op_census.timing` has it.
    - `headline` conforms to `docs/wiki/guard-messaging.md` § Register: one
      fact, once, plus a terse alternative. The alternative is never "raise
      the timeout"; there is no shape of this message in which that text is
      correct.

Spec backlink: docs/decisions/DR-344-the-brightline-process-budget-for-claude-klabauter.md
               docs/plans/2026-08-21-the-census-that-cannot-miss-an-op.md
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import List, Optional

from coordinator_core.ipc import CallerFacingValidationError, register_op
from coordinator_core.op_census.timing import PROCESS_TIME_BAR_MS
from coordinator_core.telemetry.op_latency import breach_summary, sink_generations

__all__ = [
    "BRIGHTLINE_BUDGET_MS",
    "DEFAULT_TOP_N",
    "MAX_TAIL_BYTES",
    "MAX_TELEMETRY_ROWS",
    "PER_PROCESS_BAR_MS",
    "TREND_WINDOW_LIMITED",
    "breach_report",
    "headline_for",
]

#: Bound on rows read from the current generation. Same number and same
#: discipline as `op_census_report.MAX_TELEMETRY_ROWS`, restated rather than
#: imported: importing it would drag this telemetry-only op's import graph
#: through `module_summary`/`line_count`/`spawn_bearing_ops` for one integer.
#: `coordinator_core.telemetry.tests.test_breach_summary` asserts the two
#: numbers still agree.
MAX_TELEMETRY_ROWS = 200_000

#: The bound that actually bites. `MAX_TELEMETRY_ROWS` sits at 200,000
#: against a live generation of ~47,000 rows, so it bounds nothing today and
#: the parse cost tracks sink growth instead — measured 140-219ms over the
#: whole current generation on 2026-08-21, already brushing DR-344's 200ms
#: per-process bar and rising as peers appended during the session. 6MB of
#: tail is ~21,000 rows and roughly a day of traffic at this box's load norm,
#: parses in well under half that budget, and stays flat as the sink grows.
#: Recency is what a breach view needs: the newest rows carry the trend, and
#: `source.head_truncated` says plainly when older ones went unread.
MAX_TAIL_BYTES = 6 * 1024 * 1024

#: Ranked rows returned by default. The list is a work queue for deletions,
#: not a dashboard — `totals.breaching_ops` always carries the untruncated
#: count, so a short list can never read as a clean box.
DEFAULT_TOP_N = 20

#: DR-344 constraint 1 and constraint 7 — this op asserts against both.
BRIGHTLINE_BUDGET_MS = 500.0
PER_PROCESS_BAR_MS = 200.0

#: What `trend` reads when the generation was read from the tail and older
#: rows in it went unread. `_trend` splits the rows IT WAS GIVEN into two
#: halves, so on a truncated read both halves sit inside the tail and a rise
#: that happened before the window is not merely unmeasured — it is invisible,
#: and the surviving rows can be genuinely flat against each other. The
#: direction is then unsupported in the one way that matters: it reads "flat"
#: for an op that is getting worse.
#:
#: Observed, not hypothesised. `ceremony.scoped_git_commit` reported "flat"
#: off a 6MB tail of a 14.8MB generation with 78.6MB of rotated history never
#: read, while a full-generation read showed hourly p50 going 3-8s to 85.4s on
#: the same day (claude-klabauter-84, session 6d3e6581, 2026-08-21).
#:
#: This is `TREND_MIN_ATTEMPTS_PER_HALF`'s rule applied to the other axis:
#: that constant refuses a direction when a half-window holds too FEW rows,
#: and this refuses one when the window itself is a fraction of the
#: generation. Both say "insufficient", never "flat" — reading "flat" off a
#: sample that cannot support it is the false-pass `op_census.timing`'s
#: three-state rule forbids.
#:
#: Negative-spec: this does NOT widen the read. Reading the whole corpus to
#: earn a trend would put 90+MB behind a 500ms op, which is the trade DR-344
#: refuses. The op stays cheap and stops claiming what a cheap read cannot
#: support; an unqualified direction is the thing being deleted here, not the
#: bound that made it unqualified.
TREND_WINDOW_LIMITED = "window_limited"


def _tail_entries(path: Path, *, tail_bytes: int, max_rows: int):
    """Parse the LAST `tail_bytes` of one JSONL generation, newest rows kept.

    Why a byte bound and not `engine_report.iter_sink_entries`'s row bound:
    that reader walks generations OLDEST-first and caps total lines read, a
    shape its own docstring flags as unable to protect recency, and a row cap
    set above the live row count (200,000 against ~47,000 today) bounds
    nothing at all — the parse cost tracks sink GROWTH, and this op is held to
    DR-344's 200ms per-process bar. Measured 2026-08-21: parsing the whole
    current generation cost 140-219ms and rose across the session as peers
    appended. A byte bound is flat against growth.

    No semantics live here. Which rows count, and as what, is entirely
    `op_latency.breach_summary`'s — this returns raw dicts and nothing else,
    so there is no second opinion about a row to drift from the first.

    The first line after the seek is almost always a partial row and is
    dropped. Never raises: a missing or unreadable generation yields nothing.
    """
    import json

    entries: List[dict] = []
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            start = max(0, size - tail_bytes)
            if start:
                fh.seek(start)
                fh.readline()
            for raw_line in fh:
                if len(entries) >= max_rows:
                    break
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line.decode("utf-8", errors="replace"))
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(entry, dict):
                    entries.append(entry)
    except OSError:
        return [], False
    return entries, size > tail_bytes


def _current_generation_paths(repo_root: Path) -> List[Path]:
    """The newest op-latency generation only, as a one-element list.

    Never raises: an unresolvable sink degrades to an empty list, matching
    `op_census_report._read_current_generation_entries`'s failure discipline.
    """
    try:
        return sink_generations(repo_root)[:1]
    except OSError:
        return []


def headline_for(summary: dict) -> str:
    """One operator-facing line: what happened, then what to do instead.

    Register-conforming (`docs/wiki/guard-messaging.md` § Register) — one
    content-bearing fact stated once, plus a terse imperative alternative.
    The alternative names deletion or a rebuild under the bar, and never a
    timeout: an op is not made correct by the caller waiting longer for it,
    and a message that says so teaches the habit this surface exists to
    remove.
    """
    totals = summary["totals"]
    bar_ms = summary["bar_ms"]
    breaching = totals["breaching_ops"]

    if breaching == 0:
        return (
            f"No op over the {bar_ms:.0f}ms bar in {totals['attempts']} attempts "
            f"({totals['vanished']} vanished, {totals['in_flight']} in flight)."
        )

    worst = summary["ops"][0]
    return (
        f"{breaching} ops over the {bar_ms:.0f}ms bar, "
        f"{totals['stolen_ms'] / 1000.0:.1f}s stolen from the box. "
        f"Worst: {worst['op']} ({worst['stolen_ms'] / 1000.0:.1f}s, "
        f"{worst['breaches']}/{worst['attempts']}, {worst['trend']}). "
        "Delete it, or rebuild it under the bar."
    )


def breach_report(
    *,
    repo_root: Optional[Path] = None,
    entries: Optional[List[dict]] = None,
    top_n: Optional[int] = DEFAULT_TOP_N,
    now: Optional[float] = None,
) -> dict:
    """Assemble the budget-breach report. Pure of IPC — the `register_op`
    handler below is a thin wrapper.

    `entries`, when given, is used verbatim and no sink is read (the test and
    `census()` path). Otherwise the current generation is read, bounded at
    `MAX_TELEMETRY_ROWS`.

    Adds to `breach_summary`'s shape a `source` block (which generation, how
    many rows, whether the bound bit), a `headline`, and this op's own
    `self_assessment` against DR-344's two bars.
    """
    handler_t0 = time.process_time()

    rows_capped = False
    head_truncated = False
    sink_name = None
    if entries is None:
        if repo_root is None:
            repo_root = Path.cwd()
        paths = _current_generation_paths(repo_root)
        sink_name = paths[0].name if paths else None
        if paths:
            entries, head_truncated = _tail_entries(
                paths[0], tail_bytes=MAX_TAIL_BYTES, max_rows=MAX_TELEMETRY_ROWS
            )
        else:
            entries = []
        rows_capped = len(entries) >= MAX_TELEMETRY_ROWS

    summary = breach_summary(entries, bar_ms=PROCESS_TIME_BAR_MS, now=now, top_n=top_n)
    if head_truncated:
        for row in summary.get("ops", ()):
            row["trend"] = TREND_WINDOW_LIMITED
    summary["op"] = "op_census.breaches"
    summary["source"] = {
        "generation": sink_name,
        "generations_read": 1 if sink_name else 0,
        "rows_read": len(entries),
        "max_rows": MAX_TELEMETRY_ROWS,
        "rows_capped": rows_capped,
        "tail_bytes": MAX_TAIL_BYTES,
        # True means older rows in this same generation went unread. Every
        # figure above is then "within the read window", not all-time —
        # `window.first_seen` in particular is the oldest row READ, never the
        # op's true first sighting. Reported so a bounded read can never be
        # mistaken for a whole-corpus one.
        "head_truncated": head_truncated,
        "top_n": top_n,
    }
    summary["headline"] = headline_for(summary)

    handler_total_ms = (time.process_time() - handler_t0) * 1000.0
    summary["self_assessment"] = {
        "handler_total_ms": round(handler_total_ms, 3),
        "brightline_budget_ms": BRIGHTLINE_BUDGET_MS,
        "per_process_bar_ms": PER_PROCESS_BAR_MS,
        "under_brightline": handler_total_ms < BRIGHTLINE_BUDGET_MS,
        "under_per_process_bar": handler_total_ms < PER_PROCESS_BAR_MS,
    }
    return summary


def _read_params(params: dict) -> Optional[int]:
    """Validate `params`, returning `top_n`.

    `bar_ms` is REFUSED, not ignored. Accepting it would let any caller
    produce a clean report by naming a bar the op already fits — the report
    would be true of the number supplied and useless as evidence, and the
    refusal is cheaper than a footnote nobody reads.
    """
    if not isinstance(params, dict):
        return DEFAULT_TOP_N

    for banned in ("bar_ms", "budget_ms", "threshold_ms"):
        if banned in params:
            raise CallerFacingValidationError(
                f"op_census.breaches takes no {banned}: the bar is DR-344's "
                f"{PROCESS_TIME_BAR_MS:.0f}ms brightline for every op. Pass top_n to widen the list."
            )

    top_n = params.get("top_n", DEFAULT_TOP_N)
    if top_n is None:
        return None
    if not isinstance(top_n, int) or isinstance(top_n, bool) or top_n < 1:
        raise CallerFacingValidationError(
            "op_census.breaches top_n must be a positive integer, or null for every breaching op."
        )
    return top_n


@register_op("op_census.breaches")
def _op_budget_breaches(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "op_census.breaches" handler — see module docstring and
    `breach_report()` for the assembled shape.

    Params: `top_n` (positive int, or null for every breaching op). A
    caller-supplied bar is refused — see `_read_params`.
    """
    return breach_report(repo_root=repo_root, top_n=_read_params(params))
