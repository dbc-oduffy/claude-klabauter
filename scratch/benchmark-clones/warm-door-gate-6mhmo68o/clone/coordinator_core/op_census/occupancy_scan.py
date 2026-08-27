"""coordinator_core.op_census.occupancy_scan — the windowed, stamped
population source for the roster-pruning census.

Purpose: a streaming, offline scan of the op-latency corpus
(`coordinator_core.telemetry.op_latency`'s sink) that accumulates, per op,
the raw occupancy figures the roster-pruning plan reasons from: count,
sum/mean/max of `elapsed_ms` over `kind: complete` rows, the `started` count
for reconciliation, an outcome partition (`ok`/`error`/`timeout`), a robust
`n * p50` twin of the summed occupancy, and a `shape` classification. Every
result carries a STAMP naming the generation files read, their byte sizes,
total row count, unparseable-row count, and this scan's own read time — a
figure with no stamp is not reportable (plan `2026-08-23-the-roster-stops-
being-hand-curated.md`, C1 task body).

NOT an op. No registry entry, no `ops/` module, no hot-path caller (AC2) —
see § Why a new module below.

Spike verdict (proven shape, measured cost): current generation, 188ms
process time over 80,024 rows; all three generations, 844ms over 414,163
rows; zero unparseable rows. Plain stdlib line-by-line JSON decode is
sufficient — this module does not reach for `orjson` or an index, and does
not add a dependency to an offline path.

REUSE, not re-authored — this module composes, rather than restates:
    - `coordinator_core.telemetry.op_latency.sink_generations` — locates and
      orders the generation files, newest-first.
    - `coordinator_core.op_census.timing.routed_entries` /
      `coordinator_core.telemetry.op_latency.EXECUTION_ROUTES` — re-exported
      for a caller that wants the routed-only view, but NOT applied as a
      filter by this module's own aggregation loop — see § Routed vs
      routeless below. The substrate claim this docstring carried until
      2026-08-23 ("applying it changes nothing on the current corpus") was
      FALSE: measured against the real 203,809-row `kind: complete`
      population, 153,154 rows (75.1%) carry no `route` field at all, and the
      filter silently dropped every one of them from every occupancy/max
      figure. See § Routed vs routeless (correction) and
      `state/audits/2026-08-23-the-op-table-against-both-admission-criteria.md`
      § "The substrate claim was false".
    - `coordinator_core.telemetry.op_latency.breach_summary` — not called by
      this module's own aggregation loop (a second full pass over the same
      rows would double the read cost this module is measured against), but
      re-exported at `__all__` so a caller building a combined report can
      hand it the SAME `entries` list this module read, at no extra IO cost.

Why a NEW module and not a flag on `op_budget_breaches.py`: the repo's
reuse-first norm makes this owed. `op_budget_breaches.py`'s entire docstring
argues for its bounded (6MB) tail read; an unbounded-mode flag there would
put an 844ms unbounded scan inside a module built to keep callers under a
500ms-budgeted op, and would hand every caller of that budgeted op a way to
blow its own budget by passing an argument. The bound is the thing that
module is FOR. This module keeps the unbounded read off the op registry
entirely (AC2) and keeps that argument intact. No existing `op_census/`
module (`line_count`, `module_summary`, `spawn_bearing_ops`, `timing`) does a
full-generation scan, so nothing here duplicates them.

§ Routed vs routeless (correction, 2026-08-23)

This module previously applied `entry.get("route") in EXECUTION_ROUTES` as a
hard filter BEFORE accumulating a row into any per-op figure, on the strength
of a docstring claim that doing so "changes nothing on the current corpus".
That claim was measured and is FALSE (75.1% of `kind: complete` rows carry no
`route` field at all — see the REUSE bullet above) and the filter was hiding
the entire breach on twelve ops, `coverage.gate` and `hooks.session_heartbeat`
among them.

`route` is written unconditionally by every current writer
(`op_latency._write_entry`, `entry["route"] = execution_route()`, added in
commit `e7a914e7f`, "C12: one route per logical op, and the record follows
the executor"). A routeless row is therefore not a live write-path gap — it
predates that commit, exactly the same shape as an absent `kind` field
predating ITS own introduction (`op_latency` module docstring's own
backward-reading rule: absent `kind` reads as `"complete"`, never dropped).
The corpus this scan reads spans generations old enough that the large
majority of rows predate the route-stamping change.

DR-332 § Item 2 (`docs/decisions/DR-332-the-directive-cost-unit-half-is-
deferred-and-the-warmth-gate-is-refused.md`) is the source of the "unobservable, not cold"
language this module's docstring cited for the filter. Read in full, DR-332
Item 2 answers a DIFFERENT question: what share of routed traffic runs
`warm_server` vs `in_process`, over a trailing-24h window, so that "cold" vs
"observed-in-a-route" is the right axis for a WARMTH measurement — a route is
definitionally required there, because the thing being measured IS which
route executed the op. It says nothing about whether an op's own
`elapsed_ms` — computed identically regardless of `route`, at
`ipc.dispatch_message`'s `perf_counter()` site — is trustworthy as
caller-facing latency. It is: the clock that produced it does not read
`route` at all. Occupancy and caller-harm (this module's purpose, and the
suspension roster's) are a different question from cold-start/warmth
attribution, and DR-332's reasoning does not extend to it.

Decision: `route` is NOT applied as an inclusion filter for occupancy, max,
or any other per-op figure. A routeless row is accumulated exactly like a
routed one. What IS preserved, so the choice is visible rather than silent
(this section's whole reason to exist): every `OpOccupancy` row separately
carries `routed_count` / `routeless_count` and `routed_max_observed_ms` /
`routeless_max_observed_ms` (both computed over ALL outcomes, mirroring
`max_observed_ms`'s own scope), and `OccupancyStamp` carries a corpus-level
`routeless_complete_rows` / `routed_complete_rows` / `routeless_share` so a
reader can always see how much of a figure's population lacked a route,
without that population ever being silently discarded first.

§ Liveness is a third filter (AC1b)

`coordinator_core.ipc._REGISTRY`, after eagerly importing BOTH
`coordinator_core.ops` and `coordinator_core.hooks`, is the one registry
`register_op()` writes into for every op regardless of its `ops.`/`hooks.`
prefix — `coordinator_core.hooks.__init__`'s own module docstring states
`register_op()` is called by every hook submodule at import time under the
"hooks.<name>" namespace, into the SAME registry `coordinator_core.ops`
modules register into. Reading `coordinator_core.op_census.spawn_bearing_ops
.live_registry_op_names()` alone (which imports only `coordinator_core.ops`)
therefore reports every `hooks.*` op dead — this module's own liveness
oracle (`live_registry_op_names`, deliberately re-defined here rather than
importing that sibling function, precisely because the sibling's coverage
is the gap this section closes) imports both packages before reading the
registry.

Verified against AC1b's three known answers: `coverage.gate` is DEAD (no
longer a registered method name — a plain `ipc._REGISTRY` grep confirms no
`register_op("coverage.gate")` site remains under `coordinator_core/ops/`),
`hooks.track_touched_files` is LIVE, `handoff.reconcile_open` is LIVE
(`coordinator_core/ops/handoff_reconcile.py`'s own
`@register_op("handoff.reconcile_open")`).

UNCLASSIFIABLE, not defaulted to live: if importing either package leaves
ANY module poisoned (`coordinator_core.ops.get_poisoned_modules()` or
`coordinator_core.hooks.get_poisoned_modules()` non-empty), this scan cannot
tell a genuinely-removed op from one whose owning module merely failed to
import on THIS run -- both look identical from here (absent from the
registry). Every op absent from the combined registry is reported
`"unclassifiable"` rather than `"dead"` for the whole of that run, and
`OccupancyStamp.liveness_poisoned_modules` names which modules poisoned it,
so a caller can tell a real, clean DEAD verdict from one that could not be
trusted this run. An `"unclassifiable"` op is excluded from any
publication that gates on liveness (module docstring's own instruction:
"exclude them from publication rather than defaulting them to live") --
callers reading `OpOccupancy.liveness` MUST treat that value as a third
state, never coerce it toward `"live"` or `"dead"`.

§ Partition by outcome (AC6b) — not optional, not a filter flag

Every accumulator carries THREE outcome buckets (`ok`, `error`, `timeout`),
never collapsed to one before publication. `occupancy_ms`/`occupancy_secs`
(the box-seconds a caller reads as "how much this op cost") sum `ok` rows
ONLY. `max_observed_ms` is the max `elapsed_ms` across ALL `kind: complete`
rows regardless of outcome — the worst thing this scan has SEEN.
`max_completed_ms` is the max across `ok` + `error` rows only, excluding
`timeout` — a `timeout` row measures how long a record stayed open, not how
long the box was held (the caller gave up; the handler may still have been
running or already committed — see `op_latency`'s own "timeout" outcome
negative-spec). Publishing only one of the two loses either "the worst this
op has done to a caller" or "the worst it has actually cost the box", so
both are always emitted, labelled, side by side (AC6b: "No table shows a
single unlabelled column where 'worst observed' and 'worst completed'
differ").

§ Shape (AC15, staff-data-sci S8)

Computed per op, from statistics this scan already holds after one sorted-
values pass over its `ok`-outcome elapsed samples (the same population
`occupancy_ms` and `n_times_p50_ms` are drawn from):

    CEILING-DOMINATED — non-`ok` share of the op's total box-seconds
                        (`(error_ms + timeout_ms) / (ok_ms + error_ms +
                        timeout_ms)`) exceeds 40%. Checked FIRST: an op
                        whose box time is dominated by failures/timeouts is
                        not usefully described by a tail statistic over its
                        (much smaller) `ok` population.
    TAIL-DRIVEN        — otherwise, if the top 1% of the op's `ok` elapsed
                        samples account for over 20% of that population's
                        summed `ok` box-seconds, OR `mean_ok_ms / p50_ok_ms`
                        is at least 2.0 (the docstring's stated "equivalently
                        mean/p50 >~ 2" reading of the same signal).
    BROAD              — neither of the above.

An op with zero `ok`-outcome rows cannot support a tail statistic; it is
classified purely on the CEILING-DOMINATED test (100% non-`ok` share if it
has ANY complete rows at all) and otherwise reports shape `None` (no
complete rows of any outcome — nothing to classify).

§ Every figure is WALL CLOCK (AC7b)

The `kind: complete` rows this scan reads carry `elapsed_ms`, computed at
`ipc.dispatch_message`'s `perf_counter()` site — wall clock, never process
time (see `coordinator_core.op_census.timing`'s own corrected module
docstring for the history of this exact confusion). Every occupancy/max/
p50/mean figure this module emits is suffixed `_ms` or `_secs` and MUST be
read as wall clock; `OccupancyStamp` and every `OpOccupancy` fixed field
name below is deliberately un-abbreviated ("occupancy_ms", never
"box_seconds") so a caller cannot rename it to something that implies a
cost this scan cannot support (module docstring's own instruction: never
name a column "box-seconds" without the wall-clock label attached).

§ Windowing (merged from the original C1+C2 split, staff-eng F6; cites
DR-337-the-coverage-refusal-is-windowed.md; revised 2026-08-23 per plan
`2026-08-23-the-roster-stops-being-hand-curated.md` § "The window is not a
parameter, it is the population" — see AC4b)

DR-337 ratified the identical shape one axis over: a ratio computed
correctly on its own axis (DR-328's share-numerator fix) was still diluted
by however much history had accumulated on the TIME axis, and the fix was a
bounded, STATED window rather than an unbounded all-time read. An all-time
max over a corpus straddling a fix measures the op before the fix —
`hooks.track_touched_files` is this module's own worked example and test
oracle: it carries a historical max (6,939.7ms, measured 2026-08-21) that
predates its own fix and reinstatement, and it must NOT surface as a
candidate under a 7-day `Window.WALL_CLOCK_CUTOFF` scan, though it MUST
surface under `Window.FULL_HISTORY` (proving the window excludes it
deliberately, not hiding the defect).

DECISION (plan § "The window is not a parameter, it is the population"):
window BY WALL-CLOCK CUTOFF, never by generation boundary. Generation
boundaries are rotation artifacts of unequal length (measured 83.8h / 97.1h
/ 176.1h on this corpus) — a stamped date cutoff is reproducible against any
generation set, including one that rotates mid-audit, and "the newest
generation" and "the last N days" are demonstrably different windows over
this corpus, not two names for one idea.

    Window.CURRENT_GENERATION — only the live (unrotated) sink file, i.e.
        `sink_generations(repo_root)[0]`. Retained for callers that
        genuinely want "whatever is still in the live file", not a stated
        population — this is the historical default, kept for
        backward compatibility with existing callers/tests.
    Window.FULL_HISTORY       — every generation `sink_generations` returns,
        no cutoff.
    Window.WALL_CLOCK_CUTOFF  — every generation `sink_generations` returns
        is READ (a wall-clock cutoff cannot be resolved without reading
        every row of every generation to find the ones inside it, unlike a
        generation-scoped window), but only rows whose `t_start` is
        `>= since` are folded into the windowed population. `since` (a unix
        epoch float) is REQUIRED for this window and is stamped on
        `OccupancyStamp.since_epoch` / `.since_iso` — a figure whose window
        is not stated is not reportable (AC4b).

Where a windowed max would genuinely lose information, the answer is a
SECOND COLUMN, not a wider window (plan § "The window is not a parameter"):
every `OpOccupancy` row carries `max_observed_ms_full_history` /
`max_completed_ms_full_history` beside its windowed `max_observed_ms` /
`max_completed_ms`. Under `Window.WALL_CLOCK_CUTOFF` these two differ
whenever a row outside the cutoff carried a higher max than any row inside
it (`hooks.track_touched_files`'s own worked example); under
`Window.CURRENT_GENERATION` / `Window.FULL_HISTORY` (no `since` filter) the
full-history columns mirror the windowed ones, since every row read is
already unfiltered.

`window` is a REQUIRED field on `OccupancyStamp` — a figure whose window is
not stated is not reportable (task body).

Test surface: `coordinator_core/op_census/tests/test_occupancy_scan.py`.
Never asserts a timing — only structure, reconciliation counts, and the
windowing/liveness oracle behaviour.

Spec backlink: state/dispatch-briefs/2026-08-23-the-roster-stops-being-hand-curated/C1.md
               docs/plans/2026-08-23-the-roster-stops-being-hand-curated.md
               docs/decisions/DR-337-the-coverage-refusal-is-windowed.md
"""

from __future__ import annotations

import dataclasses
import datetime
import enum
import json
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from coordinator_core.op_budget_suspension import admitted_on
from coordinator_core.op_census.timing import routed_entries
from coordinator_core.telemetry.op_latency import (
    EXECUTION_ROUTES,
    _percentile_idx,
    breach_summary,
    sink_generations,
)

__all__ = [
    "Window",
    "Liveness",
    "Shape",
    "CEILING_DOMINATED_NON_OK_SHARE_BAR",
    "TAIL_DRIVEN_TOP1PCT_SHARE_BAR",
    "TAIL_DRIVEN_MEAN_OVER_P50_BAR",
    "OpOccupancy",
    "OccupancyStamp",
    "OccupancyResult",
    "live_registry_op_names",
    "classify_liveness",
    "scan_occupancy",
    "routed_entries",
    "breach_summary",
    "EXECUTION_ROUTES",
    "admitted_on",
    "SEVEN_DAYS_SECS",
]

OUTCOMES = ("ok", "error", "timeout")


class Window(str, enum.Enum):
    """Which generation(s) a scan reads — see module docstring § Windowing.

    A plain `str` subclass so a stamped `window` value serializes as its own
    name via `.value` without a caller needing to know this is an enum.
    """

    CURRENT_GENERATION = "current_generation"
    FULL_HISTORY = "full_history"
    WALL_CLOCK_CUTOFF = "wall_clock_cutoff"


class Liveness(str, enum.Enum):
    """Three states, never two — see module docstring § Liveness is a third
    filter. `UNCLASSIFIABLE` is not `DEAD`; a caller gating on liveness must
    exclude it rather than coerce it toward either real answer."""

    LIVE = "live"
    DEAD = "dead"
    UNCLASSIFIABLE = "unclassifiable"


class Shape(str, enum.Enum):
    """Exactly three, see module docstring § Shape."""

    CEILING_DOMINATED = "CEILING-DOMINATED"
    TAIL_DRIVEN = "TAIL-DRIVEN"
    BROAD = "BROAD"


#: Non-`ok` share of an op's total box-seconds above which it is
#: CEILING-DOMINATED, checked before any tail statistic (module docstring).
CEILING_DOMINATED_NON_OK_SHARE_BAR: float = 0.40

#: Share of an op's summed `ok` box-seconds the top 1% of its `ok` samples
#: must exceed for TAIL-DRIVEN (module docstring).
TAIL_DRIVEN_TOP1PCT_SHARE_BAR: float = 0.20

#: `mean_ok_ms / p50_ok_ms` at or above which TAIL-DRIVEN fires even if the
#: top-1%-share test does not — the docstring's stated "equivalently mean/p50
#: >~ 2" reading of the same signal.
TAIL_DRIVEN_MEAN_OVER_P50_BAR: float = 2.0

#: The plan's ratified candidate wall-clock window for the audit's candidate
#: list (docs/plans/2026-08-23-the-roster-stops-being-hand-curated.md § "The
#: window is not a parameter, it is the population" — 7 days is NOT one of
#: the six PM ratification items, it is the module's own engineering choice).
SEVEN_DAYS_SECS: float = 7 * 24 * 60 * 60.0


def live_registry_op_names() -> frozenset:
    """The combined `ipc._REGISTRY` key set after eagerly importing BOTH
    `coordinator_core.ops` and `coordinator_core.hooks`.

    Deliberately re-defined here rather than reusing
    `coordinator_core.op_census.spawn_bearing_ops.live_registry_op_names`,
    which imports `coordinator_core.ops` alone and therefore reports every
    `hooks.*` op dead — see module docstring § Liveness is a third filter.
    """
    import coordinator_core.hooks as _hooks_pkg
    import coordinator_core.ops as _ops_pkg
    from coordinator_core import ipc

    _ops_pkg._eager_import_all()
    _hooks_pkg._eager_import_all()
    return frozenset(ipc._REGISTRY.keys())


def _poisoned_modules() -> Dict[str, str]:
    """`{module dotted-path: str(exception)}` across both `ops` and `hooks`
    poisoned-module tables, read AFTER `live_registry_op_names()` has run
    (both packages' `_eager_import_all()` populate their own table as a
    side effect of that call)."""
    import coordinator_core.hooks as _hooks_pkg
    import coordinator_core.ops as _ops_pkg

    poisoned: Dict[str, str] = {}
    for module_path, exc in _ops_pkg.get_poisoned_modules().items():
        poisoned[module_path] = str(exc)
    for module_path, exc in _hooks_pkg.get_poisoned_modules().items():
        poisoned[module_path] = str(exc)
    return poisoned


def classify_liveness(
    op_names: Iterable[str],
    *,
    live_registry: Optional[frozenset] = None,
    poisoned_modules: Optional[Dict[str, str]] = None,
) -> Dict[str, Liveness]:
    """`op_name -> Liveness` for every name in `op_names`.

    `live_registry` / `poisoned_modules` are injectable so a caller (and
    this module's own tests) can classify against a fixed registry snapshot
    without paying a real eager-import pass per call; both default to a
    live read via `live_registry_op_names()` / `_poisoned_modules()`.

    An op present in `live_registry` is `LIVE`. An op absent from it is
    `DEAD` UNLESS `poisoned_modules` is non-empty, in which case it is
    `UNCLASSIFIABLE` — see module docstring § Liveness is a third filter for
    why an import failure anywhere makes every absence untrustworthy for
    the whole of that run, not only for ops belonging to the poisoned
    module (this scan has no per-op module attribution without a second,
    more expensive resolution pass).
    """
    if live_registry is None:
        live_registry = live_registry_op_names()
    if poisoned_modules is None:
        poisoned_modules = _poisoned_modules()

    any_poisoned = bool(poisoned_modules)
    result: Dict[str, Liveness] = {}
    for op_name in op_names:
        if op_name in live_registry:
            result[op_name] = Liveness.LIVE
        elif any_poisoned:
            result[op_name] = Liveness.UNCLASSIFIABLE
        else:
            result[op_name] = Liveness.DEAD
    return result


@dataclasses.dataclass
class _Accumulator:
    """Mutable per-op scratch state, one instance per op seen while
    streaming. Converted to a frozen `OpOccupancy` only once, at publish
    time — see `_finalize`."""

    op: str
    started_count: int = 0
    complete_count: int = 0
    sum_ms: Dict[str, float] = dataclasses.field(
        default_factory=lambda: {o: 0.0 for o in OUTCOMES}
    )
    count: Dict[str, int] = dataclasses.field(
        default_factory=lambda: {o: 0 for o in OUTCOMES}
    )
    ok_elapsed: List[float] = dataclasses.field(default_factory=list)
    max_observed_ms: Optional[float] = None
    max_completed_ms: Optional[float] = None
    #: Full-history side channel (§ Windowing) — updated from EVERY complete
    #: row physically read for this scan's generation set, regardless of any
    #: `since` cutoff. Mirrors the windowed fields exactly when no cutoff is
    #: applied (`Window.CURRENT_GENERATION` / `Window.FULL_HISTORY`).
    max_observed_ms_full_history: Optional[float] = None
    max_completed_ms_full_history: Optional[float] = None
    #: § Routed vs routeless — counted, never filtered on. Both cover ALL
    #: `kind: complete` rows within the window, any outcome.
    routed_count: int = 0
    routeless_count: int = 0
    routed_max_observed_ms: Optional[float] = None
    routeless_max_observed_ms: Optional[float] = None


def _shape_for(acc: _Accumulator, sorted_ok: List[float]) -> Optional[Shape]:
    """`sorted_ok` is `sorted(acc.ok_elapsed)`, computed once by the caller
    and shared with the `p50_ok` finalize step — see `scan_occupancy`."""
    ok_ms = acc.sum_ms["ok"]
    error_ms = acc.sum_ms["error"]
    timeout_ms = acc.sum_ms["timeout"]
    total_ms = ok_ms + error_ms + timeout_ms
    if total_ms <= 0.0:
        return None

    non_ok_share = (error_ms + timeout_ms) / total_ms
    if non_ok_share > CEILING_DOMINATED_NON_OK_SHARE_BAR:
        return Shape.CEILING_DOMINATED

    # A zero-`ok`-rows op never reaches here: `total_ms <= 0.0` (no rows of
    # any outcome) or `non_ok_share == 1.0` (only error/timeout rows) always
    # exceeds the 0.40 bar above — see module docstring § Shape.

    n = len(sorted_ok)
    top1_count = max(1, -(-n // 100))  # ceil(n / 100)
    top1_sum = sum(sorted_ok[n - top1_count :])
    top1_share = top1_sum / ok_ms if ok_ms > 0.0 else 0.0

    mean_ok = ok_ms / n
    p50_ok = _percentile_idx(sorted_ok, 0.50) or 0.0
    mean_over_p50 = (mean_ok / p50_ok) if p50_ok > 0.0 else float("inf")

    if top1_share > TAIL_DRIVEN_TOP1PCT_SHARE_BAR or mean_over_p50 >= TAIL_DRIVEN_MEAN_OVER_P50_BAR:
        return Shape.TAIL_DRIVEN

    return Shape.BROAD


@dataclasses.dataclass(frozen=True)
class OpOccupancy:
    """One op's occupancy figures over the scanned window. Every timing
    field is WALL CLOCK (module docstring § Every figure is WALL CLOCK) —
    field names are deliberately un-abbreviated so a caller cannot rename
    one to imply a cost this scan cannot support.

    `occupancy_ms` / `occupancy_secs` sum `ok`-outcome rows ONLY (AC6b).
    `max_observed_ms` covers ALL complete rows (any outcome);
    `max_completed_ms` covers `ok` + `error` only, excluding `timeout`
    (module docstring § Partition by outcome). `n_times_p50_ms` is the
    robust twin of `occupancy_ms`: `ok_count * p50_ok_ms` — the total the
    op WOULD have cost if every `ok` call took the median, computed over the
    same `ok` population `occupancy_ms` sums (staff-data-sci S4).

    `admitted_on` is the ONE ADMISSION RULE (AC10), imported from
    `coordinator_core.op_budget_suspension.admitted_on` and never restated in
    prose here — `[]`, `["max"]`, `["occupancy"]`, or `["max", "occupancy"]`,
    computed from this op's own `max_observed_ms` (or `0.0` when the op has
    no complete rows) and `occupancy_secs`."""

    op: str
    liveness: Liveness

    started_count: int
    complete_count: int

    ok_count: int
    error_count: int
    timeout_count: int

    occupancy_ms: float
    occupancy_secs: float

    mean_ok_ms: Optional[float]
    p50_ok_ms: Optional[float]
    n_times_p50_ms: Optional[float]

    max_observed_ms: Optional[float]
    max_completed_ms: Optional[float]

    #: Full-history twin of the two fields above — see module docstring
    #: § Windowing. Mirrors `max_observed_ms` / `max_completed_ms` exactly
    #: when the scan applied no `since` cutoff.
    max_observed_ms_full_history: Optional[float]
    max_completed_ms_full_history: Optional[float]

    #: § Routed vs routeless (correction, 2026-08-23) — visible, never
    #: silently dropped. `routed_max_observed_ms` / `routeless_max_observed_ms`
    #: both cover ALL outcomes within the window, same scope as
    #: `max_observed_ms` (which is their union and is unaffected by route).
    routed_count: int
    routeless_count: int
    routed_max_observed_ms: Optional[float]
    routeless_max_observed_ms: Optional[float]

    shape: Optional[Shape]

    admitted_on: List[str]


@dataclasses.dataclass(frozen=True)
class OccupancyStamp:
    """The provenance stamp every `OccupancyResult` carries — a figure
    without one is not reportable (task body). `window` is REQUIRED, never
    defaulted away by a caller re-serializing this result."""

    window: Window
    generation_paths: List[str]
    generation_byte_sizes: List[int]
    total_rows_read: int
    unparseable_rows: int
    read_time_secs: float
    liveness_poisoned_modules: Dict[str, str]

    #: The resolved wall-clock cutoff (§ Windowing, AC4b) — `None` unless
    #: `window is Window.WALL_CLOCK_CUTOFF`, in which case both are always
    #: populated together. A figure whose window is not stated is not
    #: reportable: these two fields ARE that statement for a cutoff scan.
    since_epoch: Optional[float] = None
    since_iso: Optional[str] = None

    #: Corpus-level view of § Routed vs routeless, over every in-window
    #: `kind: complete` row across every op — the visible surface for a
    #: filter that used to drop 75.1% of this corpus unseen.
    routed_complete_rows: int = 0
    routeless_complete_rows: int = 0
    routeless_share: float = 0.0


@dataclasses.dataclass(frozen=True)
class OccupancyResult:
    """The whole of one scan's output: the stamp (§ provenance) plus the
    per-op occupancy table. `ops` is a dict keyed by op name for O(1)
    caller lookups; iteration order matches first-seen order in the
    underlying scan."""

    stamp: OccupancyStamp
    ops: Dict[str, OpOccupancy]


def _generations_for_window(repo_root: Path, window: Window) -> List[Path]:
    generations = sink_generations(repo_root)
    if window is Window.CURRENT_GENERATION:
        return generations[:1]
    # FULL_HISTORY and WALL_CLOCK_CUTOFF both read every generation -- a
    # wall-clock cutoff cannot be resolved without reading every row of
    # every generation to find the ones inside it (module docstring
    # § Windowing).
    return generations


def scan_occupancy(
    repo_root: Path,
    *,
    window: Window = Window.CURRENT_GENERATION,
    since: Optional[float] = None,
    live_registry: Optional[frozenset] = None,
    poisoned_modules: Optional[Dict[str, str]] = None,
) -> OccupancyResult:
    """Stream every row of the windowed generation set, accumulate per-op
    occupancy, and return a stamped `OccupancyResult`.

    Streaming line-by-line, one `json.loads` per non-blank line — no
    generation is read into a single in-memory list of parsed rows (module
    docstring's spike-verdict cost is measured against this shape). `route`
    is NOT applied as an inclusion filter (module docstring § Routed vs
    routeless (correction)) — every row is accumulated into occupancy and
    reconciliation regardless of whether it carries a recognised `route`;
    `is_routed` is recorded per row only to populate `routed_count` /
    `routeless_count` and their `_max_observed_ms` twins.

    `since` (a unix epoch float) is the wall-clock cutoff (§ Windowing,
    AC4b) — REQUIRED when `window is Window.WALL_CLOCK_CUTOFF` (raises
    `ValueError` otherwise), ignored for the other two windows. A row whose
    `t_start` is `< since` is excluded from every windowed figure
    (`started_count`, `complete_count`, occupancy, windowed max) but still
    contributes to the full-history max side channel
    (`max_observed_ms_full_history` / `max_completed_ms_full_history`) and
    to `total_rows_read` / `unparseable_rows`, which count bytes-on-disk,
    not population membership.

    `live_registry` / `poisoned_modules` are injectable straight through to
    `classify_liveness` (see that function) so a test can pin a registry
    snapshot rather than paying a real eager-import pass.

    Never raises on a malformed line — counted in `unparseable_rows`
    instead. A missing or unreadable generation file contributes zero rows
    and zero bytes, silently (mirrors `op_latency.tail_entries`'s own
    never-raises contract over a sink other processes are appending to).
    """
    if window is Window.WALL_CLOCK_CUTOFF and since is None:
        raise ValueError(
            "since (a unix epoch float) is required for Window.WALL_CLOCK_CUTOFF "
            "-- a figure whose window is not stated is not reportable (AC4b)"
        )

    t0 = time.perf_counter()

    generations = _generations_for_window(repo_root, window)

    accumulators: Dict[str, _Accumulator] = {}
    generation_paths: List[str] = []
    generation_byte_sizes: List[int] = []
    total_rows_read = 0
    unparseable_rows = 0

    def _acc(op_name: str) -> _Accumulator:
        acc = accumulators.get(op_name)
        if acc is None:
            acc = _Accumulator(op=op_name)
            accumulators[op_name] = acc
        return acc

    for path in generations:
        try:
            byte_size = path.stat().st_size
        except OSError:
            byte_size = 0
        generation_paths.append(str(path))
        generation_byte_sizes.append(byte_size)

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for raw_line in fh:
                    line = raw_line.strip()
                    if not line:
                        continue
                    total_rows_read += 1
                    try:
                        entry = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        unparseable_rows += 1
                        continue
                    if not isinstance(entry, dict):
                        unparseable_rows += 1
                        continue

                    # § Routed vs routeless (correction, 2026-08-23): `route`
                    # is recorded per-op below, never used to exclude a row.
                    is_routed = entry.get("route") in EXECUTION_ROUTES

                    op_name = entry.get("op")
                    if not isinstance(op_name, str):
                        continue

                    kind = entry.get("kind") or "complete"
                    acc = _acc(op_name)

                    t_start = entry.get("t_start")
                    in_window = since is None or (
                        isinstance(t_start, (int, float)) and t_start >= since
                    )

                    # Full-history side channel (§ Windowing): every complete
                    # row physically read updates this, regardless of
                    # `in_window`, BEFORE the windowed-population filter
                    # below excludes it from everything else.
                    if kind == "complete":
                        outcome_fh = entry.get("outcome")
                        elapsed_fh = entry.get("elapsed_ms")
                        if outcome_fh in OUTCOMES and isinstance(elapsed_fh, (int, float)):
                            elapsed_fh = float(elapsed_fh)
                            if (
                                acc.max_observed_ms_full_history is None
                                or elapsed_fh > acc.max_observed_ms_full_history
                            ):
                                acc.max_observed_ms_full_history = elapsed_fh
                            if outcome_fh != "timeout":
                                if (
                                    acc.max_completed_ms_full_history is None
                                    or elapsed_fh > acc.max_completed_ms_full_history
                                ):
                                    acc.max_completed_ms_full_history = elapsed_fh

                    if not in_window:
                        continue

                    if kind == "started":
                        acc.started_count += 1
                        continue
                    if kind != "complete":
                        continue

                    acc.complete_count += 1
                    if is_routed:
                        acc.routed_count += 1
                    else:
                        acc.routeless_count += 1
                    outcome = entry.get("outcome")
                    if outcome not in OUTCOMES:
                        continue

                    elapsed = entry.get("elapsed_ms")
                    if not isinstance(elapsed, (int, float)):
                        continue
                    elapsed = float(elapsed)

                    acc.sum_ms[outcome] += elapsed
                    acc.count[outcome] += 1
                    if outcome == "ok":
                        acc.ok_elapsed.append(elapsed)

                    if acc.max_observed_ms is None or elapsed > acc.max_observed_ms:
                        acc.max_observed_ms = elapsed
                    if outcome != "timeout":
                        if acc.max_completed_ms is None or elapsed > acc.max_completed_ms:
                            acc.max_completed_ms = elapsed
                    if is_routed:
                        if (
                            acc.routed_max_observed_ms is None
                            or elapsed > acc.routed_max_observed_ms
                        ):
                            acc.routed_max_observed_ms = elapsed
                    else:
                        if (
                            acc.routeless_max_observed_ms is None
                            or elapsed > acc.routeless_max_observed_ms
                        ):
                            acc.routeless_max_observed_ms = elapsed
        except FileNotFoundError:
            continue
        except OSError:
            continue

    live_registry = (
        live_registry_op_names() if live_registry is None else live_registry
    )
    poisoned = _poisoned_modules() if poisoned_modules is None else poisoned_modules
    liveness_map = classify_liveness(
        accumulators.keys(), live_registry=live_registry, poisoned_modules=poisoned
    )

    ops: Dict[str, OpOccupancy] = {}
    for op_name, acc in accumulators.items():
        ok_count = acc.count["ok"]
        occupancy_ms = acc.sum_ms["ok"]
        mean_ok = (occupancy_ms / ok_count) if ok_count else None
        sorted_ok = sorted(acc.ok_elapsed)
        p50_ok = _percentile_idx(sorted_ok, 0.50) if sorted_ok else None
        n_times_p50 = (ok_count * p50_ok) if (p50_ok is not None) else None

        ops[op_name] = OpOccupancy(
            op=op_name,
            liveness=liveness_map[op_name],
            started_count=acc.started_count,
            complete_count=acc.complete_count,
            ok_count=ok_count,
            error_count=acc.count["error"],
            timeout_count=acc.count["timeout"],
            occupancy_ms=occupancy_ms,
            occupancy_secs=occupancy_ms / 1000.0,
            mean_ok_ms=mean_ok,
            p50_ok_ms=p50_ok,
            n_times_p50_ms=n_times_p50,
            max_observed_ms=acc.max_observed_ms,
            max_completed_ms=acc.max_completed_ms,
            max_observed_ms_full_history=acc.max_observed_ms_full_history,
            max_completed_ms_full_history=acc.max_completed_ms_full_history,
            routed_count=acc.routed_count,
            routeless_count=acc.routeless_count,
            routed_max_observed_ms=acc.routed_max_observed_ms,
            routeless_max_observed_ms=acc.routeless_max_observed_ms,
            shape=_shape_for(acc, sorted_ok),
            admitted_on=admitted_on(acc.max_observed_ms or 0.0, occupancy_ms / 1000.0),
        )

    routed_complete_rows = sum(acc.routed_count for acc in accumulators.values())
    routeless_complete_rows = sum(acc.routeless_count for acc in accumulators.values())
    total_complete_rows = routed_complete_rows + routeless_complete_rows
    routeless_share = (
        (routeless_complete_rows / total_complete_rows) if total_complete_rows else 0.0
    )

    since_iso = (
        datetime.datetime.fromtimestamp(since, tz=datetime.timezone.utc).isoformat()
        if since is not None
        else None
    )
    stamp = OccupancyStamp(
        window=window,
        generation_paths=generation_paths,
        generation_byte_sizes=generation_byte_sizes,
        total_rows_read=total_rows_read,
        unparseable_rows=unparseable_rows,
        read_time_secs=time.perf_counter() - t0,
        liveness_poisoned_modules=dict(poisoned),
        since_epoch=since,
        since_iso=since_iso,
        routed_complete_rows=routed_complete_rows,
        routeless_complete_rows=routeless_complete_rows,
        routeless_share=routeless_share,
    )

    return OccupancyResult(stamp=stamp, ops=ops)
