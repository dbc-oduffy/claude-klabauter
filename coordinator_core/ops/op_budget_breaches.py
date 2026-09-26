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
    - `self_assessment.handler_total_ms` is a single-shot `time.process_time()`
      bracket, not `benchmarks.process_time.batched_process_time_ms`'s
      K-iteration amortisation. Deliberate, not an oversight: batching would
      mean re-running this handler's own read-and-summarise body K times
      inside one invocation, multiplying the real disk read and computation
      this figure is supposed to report by K -- corrupting the very number
      being measured rather than refining it, and pushing a cheap op toward
      the bar it exists to police. `state/bug-backlog/2026-08-21-process-time-
      on-windows-is-15-625ms-gran-c3711465cae3.yaml` checked this op's two
      bars against the ~15.625ms Windows scheduler tick directly (not
      assumed) and excluded both as not at risk: ~7.8% quantisation error at
      the 200ms per-process bar, ~3% at the 500ms brightline -- both far
      inside the >=150ms band where a single-shot reading stops carrying
      information. `self_assessment.clock_resolution_ms` states the tick
      this process has actually observed (see `process_clock_resolution_ms`)
      alongside the reading, so a reader judges precision from disclosed
      data rather than an assumed one, the same discipline `source.
      head_truncated` already applies to a bounded read.
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
from coordinator_core.op_census.kill_ledger_inventory import KILL_LEDGER, LedgerAbsent, fate_entries
from coordinator_core.op_census.timing import PROCESS_TIME_BAR_MS
from coordinator_core.telemetry.op_latency import (
    breach_summary,
    process_clock_resolution_ms,
    sink_generations,
)

__all__ = [
    "BRIGHTLINE_BUDGET_MS",
    "DEAD_DIAL_LEDGER_ABSENT",
    "DEAD_DIAL_LEDGER_OK",
    "DEAD_DIAL_MIN_ATTEMPTS",
    "DEFAULT_TOP_N",
    "MAX_HEADLINE_BYTES",
    "MAX_TAIL_BYTES",
    "MAX_TELEMETRY_ROWS",
    "PER_PROCESS_BAR_MS",
    "TEST_CALLER_PREFIX",
    "TREND_WINDOW_LIMITED",
    "breach_report",
    "dead_dial_findings",
    "headline_for",
]

#: discipline as `op_census_report.MAX_TELEMETRY_ROWS`, restated rather than
MAX_TELEMETRY_ROWS = 200_000

#: The bound that actually bites. `MAX_TELEMETRY_ROWS` sits at 200,000
MAX_TAIL_BYTES = 6 * 1024 * 1024

DEFAULT_TOP_N = 20

#: How an op DECLARES that one of its arms spends its time on a remote, so
#: `_ARM_NOOP`/`_ARM_NETWORK` is the worked example); an arm named with this
NETWORK_ARM_SUFFIX = ".network"

BRIGHTLINE_BUDGET_MS = 500.0
PER_PROCESS_BAR_MS = 200.0

MAX_HEADLINE_BYTES = 220

_OP_TRUNC_MARKER = "~"

#: This is `TREND_MIN_ATTEMPTS_PER_HALF`'s rule applied to the other axis:
TREND_WINDOW_LIMITED = "window_limited"

#: METHOD_NOT_FOUND completions over 33 hours; every other -32601-only op
#: (`MAX_TAIL_BYTES`, current generation only — see module docstring's
DEAD_DIAL_MIN_ATTEMPTS = 10

#: test fixture exercising the METHOD_NOT_FOUND path deliberately.
TEST_CALLER_PREFIX = "coordinator_core.tests."

#: `dead_dials.ledger_status` values. `LEDGER_ABSENT` is a distinguishable
DEAD_DIAL_LEDGER_OK = "ok"
DEAD_DIAL_LEDGER_ABSENT = "absent"


def dead_dial_findings(entries: List[dict]) -> List[dict]:
    """Ops whose every recent completed dial is `-32601 METHOD_NOT_FOUND` —
    a caller still dialling a name the registry no longer serves (a
    gravestoned op) or never served (a typo/synthetic test name).

    Computed over `entries` already read by the caller — no second sink read
    (see module docstring). Two discriminators keep this from firing on every
    one-off typo, both applied BEFORE the all--32601 test: rows from a
    `TEST_CALLER_PREFIX` caller are dropped first (a test fixture dialling a
    nonexistent op on purpose is not a leak), then an op qualifies only once
    its surviving completed-row count reaches `DEAD_DIAL_MIN_ATTEMPTS`.

    Does not consult the kill ledger — `breach_report` joins each finding's
    ledger fate afterward, so this function stays testable without a ledger
    fixture and its output is unaffected by the ledger being absent.

    Returns one dict per qualifying op:
        {"op": str, "caller": str, "callers": {caller: count, ...},
         "attempts": int, "first_seen": float, "last_seen": float}
    ordered by `attempts` descending, then `op` ascending, for a deterministic
    report. `caller` is the top caller by count (ties broken by encounter
    order, i.e. whichever caller's row hit the sink first) — `callers` carries
    the full per-caller breakdown so a tied or split-caller leak (e.g.
    `ops.list` dialled 6/6 by two different callers) is never reduced to one
    caller's name.
    """
    per_op: dict = {}
    for row in entries:
        if not isinstance(row, dict) or row.get("kind") != "complete":
            continue
        caller = row.get("caller")
        if isinstance(caller, str) and caller.startswith(TEST_CALLER_PREFIX):
            continue
        op = row.get("op")
        if not isinstance(op, str):
            continue
        t_start = row.get("t_start")
        bucket = per_op.setdefault(
            op,
            {"attempts": 0, "all_method_not_found": True, "callers": {}, "first": None, "last": None},
        )
        bucket["attempts"] += 1
        if row.get("error_code") != -32601:
            bucket["all_method_not_found"] = False
        if isinstance(caller, str):
            bucket["callers"][caller] = bucket["callers"].get(caller, 0) + 1
        if isinstance(t_start, (int, float)):
            if bucket["first"] is None or t_start < bucket["first"]:
                bucket["first"] = t_start
            if bucket["last"] is None or t_start > bucket["last"]:
                bucket["last"] = t_start

    findings = []
    for op, bucket in per_op.items():
        if not bucket["all_method_not_found"]:
            continue
        if bucket["attempts"] < DEAD_DIAL_MIN_ATTEMPTS:
            continue
        callers = bucket["callers"]
        top_caller = max(callers, key=lambda c: callers[c]) if callers else None
        findings.append(
            {
                "op": op,
                "caller": top_caller,
                "callers": dict(callers),
                "attempts": bucket["attempts"],
                "first_seen": bucket["first"],
                "last_seen": bucket["last"],
            }
        )
    findings.sort(key=lambda f: (-f["attempts"], f["op"]))
    return findings


def _join_ledger_fate(findings: List[dict]) -> dict:
    """Attach each finding's kill-ledger `Fate:` value(s) — the fact that
    tells a reader "a gravestoned op is still being dialled" (fate DEAD)
    apart from "somebody dialled a name that never existed" (no ledger
    entry). Returns the `dead_dials` block; never mutates `findings`.

    Zero findings still checks `KILL_LEDGER.is_file()` before labelling
    `ledger_status` — an empty `findings` list here already means "the sink
    had no qualifying rows" (`dead_dial_findings` never touches the ledger),
    but the label must reflect whether the ledger itself exists, not merely
    assert `ok` on a path that never looked."""
    if not findings:
        status = DEAD_DIAL_LEDGER_OK if KILL_LEDGER.is_file() else DEAD_DIAL_LEDGER_ABSENT
        return {"ledger_status": status, "findings": []}

    try:
        entries = fate_entries()
    except LedgerAbsent:
        return {
            "ledger_status": DEAD_DIAL_LEDGER_ABSENT,
            "findings": [dict(f, fate=None) for f in findings],
        }

    fate_by_op: dict = {}
    for entry in entries:
        for key in entry.op_keys:
            fate_by_op.setdefault(key, []).extend(entry.fate_values)

    enriched = []
    for f in findings:
        values = fate_by_op.get(f["op"])
        fate = None
        if values:
            unique = sorted(set(values))
            fate = unique[0] if len(unique) == 1 else "/".join(unique)
        enriched.append(dict(f, fate=fate))
    return {"ledger_status": DEAD_DIAL_LEDGER_OK, "findings": enriched}


def _tail_entries(path: Path, *, tail_bytes: int, max_rows: int):
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
    try:
        return sink_generations(repo_root)[:1]
    except OSError:
        return []


def _remedy_for(op: str) -> str:
    """The imperative half of the headline, chosen by what the op declared.

    A local op is told to delete or rebuild. An arm the op itself named with
    `NETWORK_ARM_SUFFIX` is told the one thing that can actually move its
    number — fewer round trips — because no amount of local work reduces a
    remote's latency, and `headline_for`'s register rule requires the
    alternative to be one the reader can take.

    Never names a timeout, for either arm: that is `headline_for`'s standing
    rule and a wider budget is not a fix on a network arm either.
    """
    if op.endswith(NETWORK_ARM_SUFFIX):
        return (
            "Its cost is a remote round trip, not local work — "
            "cut round trips, or accept it and stop ranking it."
        )
    return "Confirm on process time, then delete it or rebuild it under the bar."


def _fit_op_name(op: str, budget_bytes: int) -> str:
    """Elide `op` from the tail so it fits in `budget_bytes` UTF-8 bytes.

    Truncates by encoded bytes, not characters, because the caller's budget
    is itself byte-denominated (`MAX_HEADLINE_BYTES`) and a character slice
    of a multi-byte op name could still overflow it. Degrades the DISPLAY
    only — the op name a caller would delete or rebuild is unaffected;
    `_remedy_for` is resolved against the untruncated `op` before this runs.
    """
    if budget_bytes <= 0:
        return ""
    op_bytes = op.encode("utf-8")
    if len(op_bytes) <= budget_bytes:
        return op
    marker_bytes = _OP_TRUNC_MARKER.encode("utf-8")
    keep = budget_bytes - len(marker_bytes)
    if keep <= 0:
        return _OP_TRUNC_MARKER[:budget_bytes]
    return op_bytes[:keep].decode("utf-8", errors="ignore") + _OP_TRUNC_MARKER


def headline_for(summary: dict) -> str:
    """One operator-facing line: what happened, then what to do instead.

    Register-conforming (`docs/wiki/guard-messaging.md` § Register) — one
    content-bearing fact stated once, plus a terse imperative alternative.
    The alternative names deletion or a rebuild under the bar, and never a
    timeout: an op is not made correct by the caller waiting longer for it,
    and a message that says so teaches the habit this surface exists to
    remove.

    NAMES ITS UNIT, and the naming is load-bearing rather than cosmetic.
    `stolen_ms` is summed WALL CLOCK past the bar (`op_latency.breach_summary`
    says so in its own docstring, and says why re-keying it to `process_ms`
    would blind it to subprocess cost). CLAUDE.md convicts on process time and
    spawn count, never wall clock, so this line reports box OCCUPANCY and says
    which axis it is on rather than asserting a cost attribution `elapsed_ms`
    cannot support. It previously read "N s stolen from the box", which claims
    CPU this unit never measured: on 2026-08-30 it reported `memo.transition`
    as one of the worst thieves on the box at 140.7s stolen, 227/354 over the
    bar, while the job-object primitive measured 187.5ms process / 6 procs per
    call -- under the bar the whole time. Two sessions in one day proposed
    rebuilding or killing an op on this signal, one of them inside a write-up
    about why wall-clock percentiles are not evidence of cost, and one
    retracted a published verdict (914f12c6c1). This is the honest interim
    named by `state/bug-backlog/2026-08-30-the-op-census-ranks-breaches-by-wall-clock.yaml`
    -- report wall clock as wall clock and drop the CPU-attribution framing --
    and it is NOT the fix, which needs a trustworthy per-op process figure the
    sink does not yet carry (`time.process_time()` excludes children).
    The kill bar is not softened by this: `_remedy_for` still says delete or
    rebuild, and adds only the measurement that can carry the conviction.
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
    remedy = _remedy_for(worst["op"])
    prefix = (
        f"{breaching} ops past the {bar_ms:.0f}ms bar, "
        f"{totals['stolen_ms'] / 1000.0:.1f}s wall-clock excess. "
        f"Worst: "
    )
    suffix = (
        f" ({worst['stolen_ms'] / 1000.0:.1f}s, "
        f"{worst['breaches']}/{worst['attempts']}, {worst['trend']}). "
        + remedy
    )
    op_budget = MAX_HEADLINE_BYTES - len(prefix.encode("utf-8")) - len(suffix.encode("utf-8"))
    return prefix + _fit_op_name(worst["op"], op_budget) + suffix


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
        "head_truncated": head_truncated,
        "top_n": top_n,
    }
    summary["headline"] = headline_for(summary)
    dead_dials = _join_ledger_fate(dead_dial_findings(entries))
    dead_dials["min_attempts"] = DEAD_DIAL_MIN_ATTEMPTS
    dead_dials["window"] = {
        # generation, not the op's lifetime. See DEAD_DIAL_MIN_ATTEMPTS's
        "generation": sink_name,
        "head_truncated": head_truncated,
    }
    summary["dead_dials"] = dead_dials

    handler_total_ms = (time.process_time() - handler_t0) * 1000.0
    summary["self_assessment"] = {
        "handler_total_ms": round(handler_total_ms, 3),
        "clock_resolution_ms": process_clock_resolution_ms(),
        "brightline_budget_ms": BRIGHTLINE_BUDGET_MS,
        "per_process_bar_ms": PER_PROCESS_BAR_MS,
        "under_brightline": handler_total_ms < BRIGHTLINE_BUDGET_MS,
        "under_per_process_bar": handler_total_ms < PER_PROCESS_BAR_MS,
    }
    return summary


def _read_params(params: dict) -> Optional[int]:
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
