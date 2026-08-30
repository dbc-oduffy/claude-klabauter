"""coordinator_core.telemetry.op_adjudication — the ONE adjudication query.

Purpose: `coordinator_core/op_budget_suspension.py` § `SUSPENDED_OPS` records
kill/suspend/gravestone dispositions against DR-344's brightline (500ms kill,
200ms per-process fix), and every figure it cites must trace to a single,
reproducible measurement rather than a hand-derived number nobody can rerun.
This module is that measurement: one pass over every `kind: "process_time"`
row across all `.git/coordinator-sessions/logs/op-latency*.jsonl` shards,
emitting per-op-per-route p95/n/window/confidence — and nothing else feeds a
brightline verdict. → state/handoffs/2026-08-29-readjudicate-the-op-budget-index.md
   state/dispatch-briefs/2026-08-29-a-zero-is-under-one-tick-not-unmeasured/C2.md

Negative-spec (hard-won, from the C2 brief's own citations):
    - A figure without a confidence label is never emitted. Every bucket this
      module produces carries exactly one of `CONFIDENCE_EXACT` (`spawns ==
      0` — a `process_time` sample taken with no child process in flight, so
      the CPU delta is wholly this op's own), `CONFIDENCE_FLOOR` (`spawns >
      0` — a spawned child's own CPU is excluded from `process_ms` by
      construction of the two universal per-process-time lessons this module
      trusts rather than re-derives, so the figure is a FLOOR on the op's
      real cost, never the whole of it), or `CONFIDENCE_SPAWNS_UNKNOWN` (the
      row carries no `spawns` key at all — structurally the case for every
      warm-route row today, since `record_op_process_time`'s own docstring
      states a missing `spawns` key means "not counted here", never `0`).
      These three are mutually exclusive by construction (`_confidence`),
      never blended into one figure: a bucket mixing an EXACT row and a
      SPAWNS-UNKNOWN row would report a percentile no single label can
      honestly describe, so rows are bucketed BY confidence before any
      percentile is taken, not after.
    - `benchmark` and `test` origins are excluded unconditionally, in every
      arm, with no flag to re-include them — see
      `coordinator_core.telemetry.op_latency.invocation_origin`'s own
      motivating case (10,832 non-production `ping` completions) for why an
      op's own census must not be convicted on its harness's traffic. The
      `test`-origin population this module was built against (6,643 rows) is
      `coordinator_core.tests.test_op_suspension_ratchet`'s own sweep
      artifact — named here because the origin baton that preceded this
      module could not name it, and an unnamed exclusion invites a future
      reader to second-guess it back in.
    - Null-origin rows are NEVER silently dropped — they are pre-stamping
      historical traffic (measured bounded 2026-08-21 → 2026-08-25, emitted
      by `hook_batch`/`pool_worker`/`one_shot_cli`/`accept_thread` before the
      `origin` field existed) and two ops in the current backlog
      (`roadmap.serve`, `review_trail.write`) are MOSTLY this population —
      excluding it would read a heavily-called op as thin. Every emitted
      bucket carries `null_origin_rows`, a plain count, so a reader always
      knows how much of a figure rests on unstamped rows rather than
      discovering it by cross-referencing a second source.
    - The `1.0` fixture rows are excluded BY OP NAME
      (`EXCLUDED_FIXTURE_OPS = {"ping", "meter.selftest"}`), not by value —
      filtering on `process_ms == 1.0` would also catch a genuine 1.0ms
      production sample, which this op's own granularity-invariant test must
      never assert against (a fixture value indistinguishable from a real
      one is not a fixture worth excluding by value).
    - `n < MIN_N` (30) reports `verdict: "unadjudicated"` — the p95 is still
      computed and returned (a reader may want to see it), but it is never
      offered as a verdict feeding a kill/suspend/reinstate decision. This
      mirrors `coordinator_core.telemetry.op_latency.breach_summary`'s own
      `TREND_MIN_ATTEMPTS_PER_HALF` discipline: an under-powered sample
      reports its insufficiency, never a false pass or false conviction.
    - Windows are absolute `t_start` epoch-second bounds, stated on every
      call, never "last 24h" computed relative to run time — a caller wanting
      a trailing-24h arm passes `window_start=now - 86400` itself and the
      returned bucket states the bound it actually used. Two arms taken
      against the SAME op can disagree sharply in both directions (measured:
      `records.query` 312.5ms all-time n=831 vs 859.4ms in a 24h arm;
      `ceremony.commit` 363 rows all-time and zero in a 24h arm because the
      op was deleted) — a verdict citing "the last day" without the bound
      that produced it cannot be checked against a second run.
    - The two-route rule (`op_verdicts`): an op observed on more than one
      execution route is adjudicated on the WORSE (higher-p95) route's
      figure, with every route's figure retained in the output so neither is
      silently dropped. A `CONFIDENCE_SPAWNS_UNKNOWN` figure NEVER convicts
      on its own — `op_verdicts` only considers `EXACT`/`FLOOR` buckets for
      the verdict figure; an op with only spawns-unknown data (however large
      its n) reports `verdict: "insufficient_confidence"`, never a numeric
      conviction, because a floor with no spawn accounting cannot be told
      apart from a genuinely cheap op that merely lacks the counter.
    - Bounded by shard mtime, not by parsing every shard and discarding: a
      requested `window_start` skips any shard whose own mtime predates it
      (a rotated generation's mtime is its last write, so it cannot contain a
      row younger than that) — see `candidate_shards`. This is a NECESSARY
      condition only (a candidate shard may still hold rows outside the
      window, filtered per-row afterward), never a sufficient one.

Budget (brightline: DR-344, 500ms kill / project CLAUDE.md § The
brightline): a naive read of all five live shards with a `kind ==
"process_time"` substring-adjacent prefilter measured 343.8ms process time
over ~116,000 rows / ~128MB on this box, this pass — ~150ms of headroom
under the 500ms bar. `_MEASURED_ROWS_PER_SECOND` states what that implies
(~337k rows/sec); `_GATE_BREAK_ROW_COUNT` is the row count at which a
linear projection of that same rate would consume the full 500ms bar on a
sink growing ~17MB/day — stated so a future caller can tell, from the
sink's own row count, how much of that headroom remains, rather than
re-deriving it from a fresh timing run every time the question comes up.
This module does not re-measure itself; the figures above are recorded, not
computed live.

Spec backlink: state/dispatch-briefs/2026-08-29-a-zero-is-under-one-tick-not-unmeasured/C2.md
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from coordinator_core.telemetry.op_latency import (
    BENCHMARK,
    EXECUTION_ROUTES,
    TEST,
    sink_generations,
)

__all__ = [
    "CONFIDENCE_EXACT",
    "CONFIDENCE_FLOOR",
    "CONFIDENCE_SPAWNS_UNKNOWN",
    "MIN_N",
    "EXCLUDED_ORIGINS",
    "EXCLUDED_FIXTURE_OPS",
    "UNROUTED",
    "MEASURED_PROCESS_TIME_MS",
    "MEASURED_ROW_COUNT",
    "_MEASURED_ROWS_PER_SECOND",
    "_GATE_BREAK_ROW_COUNT",
    "candidate_shards",
    "adjudicate",
    "op_verdicts",
]

#: EXACT: `spawns == 0` -- the sample carries no child-process CPU at all, so
#: `process_ms` is this op's own cost in full.
CONFIDENCE_EXACT = "EXACT"

#: FLOOR: `spawns > 0` -- a spawned child's own CPU is excluded from
#: `process_ms` by construction (the two universal per-process-time lessons
#: this module trusts and does not re-derive), so the figure is a lower
#: bound on the op's real cost, never the whole of it.
CONFIDENCE_FLOOR = "FLOOR"

#: SPAWNS-UNKNOWN: the row carries no `spawns` key -- structurally the case
#: for the warm route today (see `coordinator_core.ipc.record_op_process_time`
#: docstring: an absent `spawns` key means "not counted here", never `0`).
CONFIDENCE_SPAWNS_UNKNOWN = "SPAWNS-UNKNOWN"

#: Minimum row count a bucket must hold before it is offered as a verdict
#: rather than merely a figure. Mirrors
#: `coordinator_core.telemetry.op_latency.TREND_MIN_ATTEMPTS_PER_HALF`'s own
#: under-powered-sample discipline.
MIN_N = 30

#: Origins excluded unconditionally, in every arm -- see module docstring.
EXCLUDED_ORIGINS = frozenset({BENCHMARK, TEST})

#: Ops excluded BY NAME -- the `1.0` granularity fixture rows (38 `ping`, 2
#: `meter.selftest`). Excluded by name, never by value, so a genuine 1.0ms
#: production sample of a different op is never caught by this filter.
EXCLUDED_FIXTURE_OPS = frozenset({"ping", "meter.selftest"})

#: Route label for a row whose `route` field is missing or not one of
#: `EXECUTION_ROUTES` -- kept as its own bucket rather than dropped or
#: merged into a real route, so an unrouted population is visible rather
#: than silently absorbed.
UNROUTED = "unrouted"

#: Measured 2026-08-29, this box, this pass -- see module docstring Budget
#: section. Recorded, not re-derived at import or call time.
MEASURED_PROCESS_TIME_MS: float = 343.8
MEASURED_ROW_COUNT: int = 116_000

#: What the measured figures above imply, stated once so a caller does not
#: have to redo the division: ~337k rows/sec on this box, this pass.
_MEASURED_ROWS_PER_SECOND: float = MEASURED_ROW_COUNT / (MEASURED_PROCESS_TIME_MS / 1000.0)

#: DR-344's kill bar, restated here rather than imported, for the same
#: reason `coordinator_core.op_census.timing.PROCESS_TIME_BAR_MS` mirrors it
#: rather than importing it: this module must not drag in that module's own
#: import surface merely to read one float.
_BRIGHTLINE_MS: float = 500.0

#: Row count at which a LINEAR projection of the measured rate above would
#: consume the full 500ms brightline -- i.e. the point at which this
#: module's own read stops fitting inside the bar it exists to police. Not a
#: live measurement: a projection from the recorded figures above, stated so
#: a caller can compare it against the sink's current row count without
#: re-running a timing probe.
_GATE_BREAK_ROW_COUNT: int = int(MEASURED_ROW_COUNT * (_BRIGHTLINE_MS / MEASURED_PROCESS_TIME_MS))


def _confidence(spawns: Optional[int]) -> str:
    """Confidence label for one row's `spawns` value -- see module docstring."""
    if spawns is None:
        return CONFIDENCE_SPAWNS_UNKNOWN
    if spawns == 0:
        return CONFIDENCE_EXACT
    return CONFIDENCE_FLOOR


def _percentile(sorted_vals: List[float], fraction: float) -> Optional[float]:
    """Index-based percentile over a pre-sorted list, no interpolation.

    Same index rule as `coordinator_core.telemetry.op_latency._percentile_idx`
    -- restated rather than imported, since that name is a private helper of
    its own module and this module's own contract (one figure, one label)
    does not depend on sharing its object identity.
    """
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = min(len(sorted_vals) - 1, int(round(fraction * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


def candidate_shards(repo_root: Path, *, window_start: Optional[float] = None) -> List[Path]:
    """Shards from `sink_generations` that CANNOT be excluded by `window_start`.

    A rotated generation's mtime is its last write -- a shard whose mtime
    predates `window_start` cannot hold a row at or after that bound, so it
    is skipped without ever being opened. This is a NECESSARY filter only: a
    kept shard may still hold rows outside the requested window, filtered
    per-row by `_iter_process_time_rows` afterward. `window_start=None`
    (the all-time arm) returns every generation unfiltered.
    """
    paths = sink_generations(repo_root)
    if window_start is None:
        return paths
    kept = []
    for path in paths:
        try:
            if path.stat().st_mtime >= window_start:
                kept.append(path)
        except OSError:
            continue
    return kept


def _iter_process_time_rows(
    paths: Iterable[Path],
    *,
    window_start: Optional[float],
    window_end: Optional[float],
):
    """Yield every in-window, non-excluded `kind: "process_time"` row.

    Never raises: an unreadable shard, a torn line, or a non-dict row is
    skipped rather than failing the whole read -- this is a reader over a
    sink several live processes may still be appending to.
    """
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for raw_line in fh:
                    line = raw_line.strip()
                    if not line:
                        continue
                    # Substring prefilter (module docstring's Budget section):
                    # every row this module wants carries the literal value
                    # `"process_time"` somewhere in its `kind` field.
                    # Deliberately checking the VALUE alone, not
                    # `"kind":"process_time"` with a fixed separator --
                    # `_write_entry`'s writer (op_latency.py) uses
                    # `separators=(",", ":")` with no space today, but a
                    # prefilter pinned to that exact byte sequence would
                    # silently stop filtering (falling back to parsing every
                    # line, correctly but slowly) the moment any writer's
                    # `json.dumps` spacing differs -- a fixture, a future
                    # writer, or a hand-written test row among them. Skipping
                    # `json.loads` for the majority of rows that cannot
                    # possibly be `process_time` is what the measured 343.8ms
                    # budget figure (module docstring) assumes; parsing every
                    # line unconditionally is measurably slower on this box.
                    if '"process_time"' not in line:
                        continue
                    try:
                        entry = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if not isinstance(entry, dict):
                        continue
                    if entry.get("kind") != "process_time":
                        continue

                    t_start = entry.get("t_start")
                    if not isinstance(t_start, (int, float)):
                        continue
                    if window_start is not None and t_start < window_start:
                        continue
                    if window_end is not None and t_start > window_end:
                        continue

                    op = entry.get("op")
                    if not isinstance(op, str) or op in EXCLUDED_FIXTURE_OPS:
                        continue

                    if entry.get("origin") in EXCLUDED_ORIGINS:
                        continue

                    process_ms = entry.get("process_ms")
                    if not isinstance(process_ms, (int, float)):
                        continue

                    yield entry
        except (FileNotFoundError, OSError):
            continue


def adjudicate(
    repo_root: Optional[Path] = None,
    *,
    sink_paths: Optional[Iterable[Path]] = None,
    window_start: Optional[float] = None,
    window_end: Optional[float] = None,
    min_n: int = MIN_N,
) -> List[dict]:
    """One pass over `kind: "process_time"` rows, bucketed per op/route/confidence.

    Either `sink_paths` (read directly, exactly those files -- what a test
    or a caller with its own shard list wants) or `repo_root` (resolved via
    `candidate_shards`, honouring the mtime-bound skip) must be given;
    `sink_paths` wins if both are given.

    Returns a list of dicts, one per `(op, route, confidence)` bucket that
    produced at least one row:
        {"op": str, "route": str, "confidence": str,
         "p95_ms": float, "n": int, "zero_rows": int,
         "null_origin_rows": int,
         "t_start_min": float, "t_start_max": float,
         "verdict": "adjudicated"|"unadjudicated"}

    `zero_rows` counts rows in the bucket whose `process_ms == 0` -- a
    near-instant sample is a real fact worth surfacing, not folded silently
    into the percentile. `null_origin_rows` counts rows in the bucket whose
    `origin` key is entirely absent (module docstring's pre-stamping
    population) -- never dropped, always counted, so a reader can tell how
    much of a figure rests on unstamped traffic. `verdict` is
    `"unadjudicated"` whenever `n < min_n` -- the p95 is still returned, but
    is not to be read as a conviction (see module docstring).

    A route absent from `EXECUTION_ROUTES` (or missing) buckets under
    `UNROUTED` rather than a real route name or `None` -- see that
    constant's docstring.

    Never raises: an unreadable shard or malformed row is skipped (see
    `_iter_process_time_rows`); a bucket with no rows never exists.
    """
    if sink_paths is not None:
        paths = list(sink_paths)
    elif repo_root is not None:
        paths = candidate_shards(repo_root, window_start=window_start)
    else:
        raise ValueError("adjudicate requires either repo_root or sink_paths")

    buckets: Dict[tuple, dict] = {}

    for entry in _iter_process_time_rows(paths, window_start=window_start, window_end=window_end):
        op = entry["op"]
        route = entry.get("route")
        if route not in EXECUTION_ROUTES:
            route = UNROUTED
        spawns = entry.get("spawns")
        confidence = _confidence(spawns)

        key = (op, route, confidence)
        bucket = buckets.get(key)
        if bucket is None:
            bucket = {
                "values": [],
                "zero_rows": 0,
                "null_origin_rows": 0,
                "t_start_min": None,
                "t_start_max": None,
            }
            buckets[key] = bucket

        process_ms = float(entry["process_ms"])
        bucket["values"].append(process_ms)
        if process_ms == 0.0:
            bucket["zero_rows"] += 1
        if entry.get("origin") is None:
            bucket["null_origin_rows"] += 1

        t_start = float(entry["t_start"])
        if bucket["t_start_min"] is None or t_start < bucket["t_start_min"]:
            bucket["t_start_min"] = t_start
        if bucket["t_start_max"] is None or t_start > bucket["t_start_max"]:
            bucket["t_start_max"] = t_start

    figures: List[dict] = []
    for (op, route, confidence), bucket in buckets.items():
        values = sorted(bucket["values"])
        n = len(values)
        figures.append(
            {
                "op": op,
                "route": route,
                "confidence": confidence,
                "p95_ms": _percentile(values, 0.95),
                "n": n,
                "zero_rows": bucket["zero_rows"],
                "null_origin_rows": bucket["null_origin_rows"],
                "t_start_min": bucket["t_start_min"],
                "t_start_max": bucket["t_start_max"],
                "verdict": "adjudicated" if n >= min_n else "unadjudicated",
            }
        )

    figures.sort(key=lambda f: (f["op"], f["route"], f["confidence"]))
    return figures


#: Confidence labels that may CONVICT an op in `op_verdicts` -- deliberately
#: excludes `CONFIDENCE_SPAWNS_UNKNOWN`, per module docstring's two-route
#: rule ("a SPAWNS-UNKNOWN figure never convicts on its own").
_CONVICTING_CONFIDENCES = frozenset({CONFIDENCE_EXACT, CONFIDENCE_FLOOR})


def op_verdicts(figures: Iterable[dict]) -> Dict[str, dict]:
    """Per-op verdict from `adjudicate`'s figures, applying the two-route rule.

    An op observed on more than one route is convicted on the WORSE
    (higher-`p95_ms`) route among its `adjudicated`, `EXACT`/`FLOOR`
    figures -- every figure for the op (every route, every confidence) is
    retained under `"figures"` so neither route is silently dropped, and the
    convicting route/confidence is named explicitly so a reader never has to
    re-derive which one produced the verdict.

    An op with no `adjudicated` `EXACT`/`FLOOR` figure -- because it has
    only `SPAWNS-UNKNOWN` data, or only `unadjudicated` (`n < min_n`) data,
    or both -- reports `verdict: "insufficient_confidence"` rather than a
    numeric conviction, however large its `SPAWNS-UNKNOWN` `n` is. This is
    the module docstring's "a SPAWNS-UNKNOWN figure never convicts on its
    own" made concrete.

    Return shape, keyed by op:
        {op: {"op": str, "verdict": "adjudicated"|"insufficient_confidence",
              "worst_route": str|None, "worst_confidence": str|None,
              "p95_ms": float|None, "n": int|None,
              "routes_considered": [str, ...], "figures": [dict, ...]}}
    """
    by_op: Dict[str, List[dict]] = {}
    for fig in figures:
        by_op.setdefault(fig["op"], []).append(fig)

    result: Dict[str, dict] = {}
    for op, figs in by_op.items():
        convicting = [
            f
            for f in figs
            if f["confidence"] in _CONVICTING_CONFIDENCES and f["verdict"] == "adjudicated"
        ]
        if not convicting:
            result[op] = {
                "op": op,
                "verdict": "insufficient_confidence",
                "worst_route": None,
                "worst_confidence": None,
                "p95_ms": None,
                "n": None,
                "routes_considered": [],
                "figures": figs,
            }
            continue

        worst = max(convicting, key=lambda f: f["p95_ms"])
        result[op] = {
            "op": op,
            "verdict": "adjudicated",
            "worst_route": worst["route"],
            "worst_confidence": worst["confidence"],
            "p95_ms": worst["p95_ms"],
            "n": worst["n"],
            "routes_considered": sorted({f["route"] for f in convicting}),
            "figures": figs,
        }
    return result
