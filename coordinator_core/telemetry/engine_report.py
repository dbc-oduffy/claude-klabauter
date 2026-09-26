"""
coordinator_core.telemetry.engine_report — supported read surface over the
rotation-aware op-latency sink.

Purpose: promotes the rotation-spanning read mechanism `cost_census.py`
already built privately (`_sink_paths`/`_percentile`) to a module other
callers can import directly, without reaching into `cost_census`'s
private helpers or re-deriving the rotation walk. Built for
`docs/plans/2026-08-19-warm-engine-gets-an-honest-instrument.md` C1.

Reuse, not re-founding: `iter_sink_entries` below reads across every
rotated generation via `op_latency.sink_generations` — promoted (same
plan, same chunk) to `op_latency.py` beside the `_sink_path` it wraps,
specifically so both `cost_census.py` and this module import DOWN into
`op_latency`, never the other way, with no import cycle in either
direction at any import scope (see that function's docstring for why the
promote-vs-relocate call landed there and not here).

Negative-spec:
    - Not a registered IPC op. Plain module-level functions, matching
      `op_latency.pairing_summary`'s reasoning for declining op
      registration (docs/plans/2026-08-08-make-a-vanished-invocation-visible.md
      C2, "Out of scope") — op registration drags in a
      classification/authz surface out of proportion to a reader.
    - `per_op_latency` is NOT limited to `cost_census.HOT_PATH_OPS`. That
      tuple is four ops picked for census comparability; this surface's
      per-op breakdown covers every op present in the corpus.
    - Does not add p95/p99 to `cost_census._summarize_op` itself, and does
      not otherwise change that module's own output shape.

`route_distribution` and `warm_share_since` (C2, same plan) apply the
DR-328 rule: an unstamped row (`route is None`) is UNOBSERVABLE, NOT COLD,
and never contributes to a routed count or a warm-share denominator.

`process_fanout` and `outcome_split` (C4, same plan) round out the picture:
process fanout is the direct measure of "are we paying cold start per
call"; `outcome_split` reports any `outcome` value outside the known
`ok`/`error`/`timeout` set under `"other"` rather than dropping it.
`engine_telemetry_report` composes every section above plus
`op_latency.pairing_summary`'s output into one aggregator, reachable from
this same surface rather than beside it.

Spec backlink: docs/plans/2026-08-19-warm-engine-gets-an-honest-instrument.md C1, C2, C4
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional

from coordinator_core.telemetry.cost_census import MAX_ROWS_SCANNED
from coordinator_core.telemetry.op_latency import pairing_summary, sink_generations

_PERCENTILE_FRACTIONS = (0.50, 0.90, 0.95, 0.99)


def _percentile(sorted_vals: List[float], pct: float) -> Optional[float]:
    """Index-based percentile, no interpolation. `pct` is a FRACTION
    (0.50, not 50) — `_percentile(vals, 95)` silently returns the max.
    Promoted verbatim from `cost_census._percentile`."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = min(len(sorted_vals) - 1, int(round(pct * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


# `MAX_ROWS_SCANNED` from `cost_census`, so `cost_census` delegating back


def iter_sink_entries(
    *,
    repo_root: Optional[Path] = None,
    sink_paths: Optional[List[Path]] = None,
    since: Optional[float] = None,
    max_rows: int = MAX_ROWS_SCANNED,
) -> Iterator[dict]:
    """Yield parsed op-latency rows across ALL rotated generations,
    OLDEST-FIRST — the reverse of `sink_generations`'s newest-first walk,
    reversed explicitly here rather than assumed.

    Either `sink_paths` (used directly, still consumed oldest-file-first
    via the same reversal) or `repo_root` (resolved via
    `op_latency.sink_generations`) must be given; `sink_paths` wins if
    both are given.

    `since`, if given, drops rows whose `t_start` is older than it
    (rows lacking a numeric `t_start` are kept — a reader cannot safely
    call them "too old"). `max_rows` bounds total lines READ across all
    generations combined (matching `cost_census.MAX_ROWS_SCANNED`'s
    ratchet, not a target) — once hit, this stops reading, even mid-file.

    Never raises: this reads files several live processes are actively
    appending to. A malformed line, an unreadable file, or a missing
    sink is skipped, not fatal.
    """
    if sink_paths is None:
        if repo_root is None:
            return
        sink_paths = sink_generations(repo_root)

    rows_read = 0
    # truncate away from it. Unreachable today at MAX_ROWS_SCANNED =
    for path in reversed(list(sink_paths)):
        if rows_read >= max_rows:
            break
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for raw_line in fh:
                    if rows_read >= max_rows:
                        break
                    rows_read += 1
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if not isinstance(entry, dict):
                        continue
                    if since is not None:
                        t_start = entry.get("t_start")
                        if isinstance(t_start, (int, float)) and t_start < since:
                            continue
                    yield entry
        except OSError:
            continue


def _percentiles_for(vals: List[float]) -> dict:
    vals = sorted(vals)
    return {
        "n": len(vals),
        "p50_ms": _percentile(vals, 0.50),
        "p90_ms": _percentile(vals, 0.90),
        "p95_ms": _percentile(vals, 0.95),
        "p99_ms": _percentile(vals, 0.99),
        "max_ms": vals[-1] if vals else None,
    }


def latency_percentiles(entries: Iterable[dict]) -> dict:
    vals: List[float] = []
    for entry in entries:
        if (entry.get("kind") or "complete") != "complete":
            continue
        elapsed_ms = entry.get("elapsed_ms")
        if isinstance(elapsed_ms, (int, float)):
            vals.append(float(elapsed_ms))
    return _percentiles_for(vals)


_COVERAGE_FLOOR = 0.05


def route_distribution(
    entries: Iterable[dict],
    *,
    coverage_floor: float = _COVERAGE_FLOOR,
    min_complete_rows: int = 0,
) -> dict:
    """Route distribution over `entries`, refusing a verdict coverage can't
    support (DR-328).

    THE CENTRAL RULE: an unstamped row (`route is None`) is UNOBSERVABLE,
    NOT COLD. It counts toward `complete` and `unstamped`, never toward
    `by_route`, and never into the `warm_share_of_routed` denominator —
    `execution_route()` defaults every CURRENT writer to `in_process`, but
    `route` is a recent field the vast majority of the live corpus predates,
    so an unstamped row says nothing about which route it took.

    Only rows treated as "complete" (an absent `kind` is `"complete"`, per
    `op_latency`'s backward-reading rule) contribute to any count here.

    `min_complete_rows` (default `0`, load-bearing — every existing caller
    keeps today's behaviour exactly; the policy choice of what floor to set
    lives in the instrument, not here) is an absolute row-count minimum
    checked BEFORE the coverage floor: "too few rows to judge" subsumes
    "coverage over too few rows", and reporting the ratio reason on a
    tiny window is the less honest of the two. When `complete <
    min_complete_rows`, `verdict` is `"unknown"` with a `verdict_reason`
    DISTINCT from the coverage-floor refusal, naming the row count and the
    minimum, so an operator reading the envelope can tell which refusal
    fired.

    Otherwise, when `coverage` (routed / complete) is below
    `coverage_floor`, `warm_share_of_routed` is still reported, but
    `verdict` is `"unknown"` with the coverage figure named in
    `verdict_reason` — a share computed over thin coverage is a function
    of log age, not of routing.

    `"degraded"` is RESERVED for a future share-threshold check and is
    never emitted by this function.
    """
    complete = 0
    unstamped = 0
    by_route: Dict[str, int] = {}
    for entry in entries:
        if (entry.get("kind") or "complete") != "complete":
            continue
        complete += 1
        route = entry.get("route")
        if route is None:
            unstamped += 1
            continue
        by_route[route] = by_route.get(route, 0) + 1

    routed = sum(by_route.values())
    coverage = (routed / complete) if complete else 0.0

    warm_share_of_routed: Optional[float]
    if routed:
        warm_share_of_routed = by_route.get("warm_server", 0) / routed
    else:
        warm_share_of_routed = None

    if complete < min_complete_rows:
        verdict = "unknown"
        verdict_reason = (
            f"only {complete} complete row(s), below the "
            f"min_complete_rows={min_complete_rows} minimum — too few rows "
            "to judge, regardless of coverage"
        )
    elif coverage < coverage_floor:
        verdict = "unknown"
        verdict_reason = (
            f"coverage {coverage:.4%} ({routed}/{complete}) is below the "
            f"{coverage_floor:.4%} floor — a warm share over this little "
            "coverage is a function of log age, not of routing (DR-328)"
        )
    else:
        verdict = "ok"
        verdict_reason = (
            f"coverage {coverage:.4%} ({routed}/{complete}) meets the "
            f"{coverage_floor:.4%} floor"
        )

    return {
        "complete": complete,
        "routed": routed,
        "unstamped": unstamped,
        "coverage": coverage,
        "by_route": by_route,
        "warm_share_of_routed": warm_share_of_routed,
        "verdict": verdict,
        "verdict_reason": verdict_reason,
    }


def warm_share_since(
    entries: Iterable[dict], *, since_ts: float, bucket_secs: int = 1800
) -> Dict[int, dict]:
    """Warm share of ROUTED rows, bucketed per `bucket_secs`-second interval
    since `since_ts` (typically a publish stamp) — the shape that shows a
    fix working over time, as opposed to `route_distribution`'s single
    aggregate figure.

    Bucket keys are `int((t_start - since_ts) // bucket_secs)`, so bucket
    `0` is `[since_ts, since_ts + bucket_secs)`. Rows before `since_ts`,
    rows lacking a numeric `t_start`, non-"complete" rows, and unstamped
    (`route is None`) rows are all excluded — same UNOBSERVABLE-NOT-COLD
    rule as `route_distribution`: an unstamped row contributes to neither
    a bucket's `routed` count nor its `warm` count.

    Each bucket value is `{"routed": int, "warm": int, "warm_share": float}`.
    """
    buckets: Dict[int, Dict[str, int]] = {}
    for entry in entries:
        if (entry.get("kind") or "complete") != "complete":
            continue
        route = entry.get("route")
        if route is None:
            continue
        t_start = entry.get("t_start")
        if not isinstance(t_start, (int, float)):
            continue
        if t_start < since_ts:
            continue
        bucket_idx = int((t_start - since_ts) // bucket_secs)
        bucket = buckets.setdefault(bucket_idx, {"routed": 0, "warm": 0})
        bucket["routed"] += 1
        if route == "warm_server":
            bucket["warm"] += 1

    return {
        idx: {
            "routed": b["routed"],
            "warm": b["warm"],
            "warm_share": (b["warm"] / b["routed"]) if b["routed"] else None,
        }
        for idx, b in sorted(buckets.items())
    }


_KNOWN_OUTCOMES = ("ok", "error", "timeout")


def process_fanout(entries: Iterable[dict]) -> dict:
    pids: set = set()
    op_pids: Dict[str, set] = {}
    op_counts: Dict[str, int] = {}
    ops = 0
    for entry in entries:
        if (entry.get("kind") or "complete") != "complete":
            continue
        pid = entry.get("pid")
        if pid is None:
            continue
        ops += 1
        pids.add(pid)
        op = entry.get("op")
        if op is None:
            continue
        op_counts[op] = op_counts.get(op, 0) + 1
        op_pids.setdefault(op, set()).add(pid)

    by_op = {
        op: {
            "ops": op_counts[op],
            "distinct_pids": len(op_pids[op]),
            "ops_per_pid": (op_counts[op] / len(op_pids[op])) if op_pids[op] else 0.0,
        }
        for op in op_counts
    }

    return {
        "ops": ops,
        "distinct_pids": len(pids),
        "ops_per_pid": (ops / len(pids)) if pids else 0.0,
        "by_op": by_op,
    }


def outcome_split(entries: Iterable[dict]) -> dict:
    """Counts of `ok`/`error`/`timeout` (per `_KNOWN_OUTCOMES`) overall and
    per-`op`, off the `outcome` field. An `outcome` value outside the known
    set lands under a separate `"other"` bucket rather than being dropped —
    an unrecognised outcome is a finding, not noise.

    Only rows treated as "complete" (an absent `kind` is `"complete"`, per
    `op_latency`'s backward-reading rule) contribute.
    """
    overall = {bucket: 0 for bucket in (*_KNOWN_OUTCOMES, "other")}
    by_op: Dict[str, Dict[str, int]] = {}
    for entry in entries:
        if (entry.get("kind") or "complete") != "complete":
            continue
        outcome = entry.get("outcome")
        bucket = outcome if outcome in _KNOWN_OUTCOMES else "other"
        overall[bucket] += 1
        op = entry.get("op")
        if op is None:
            continue
        op_bucket = by_op.setdefault(op, {b: 0 for b in (*_KNOWN_OUTCOMES, "other")})
        op_bucket[bucket] += 1

    return {"overall": overall, "by_op": by_op}


def engine_telemetry_report(*, repo_root: Path, since: Optional[float] = None) -> dict:
    entries = list(iter_sink_entries(repo_root=repo_root, since=since))
    return {
        "latency": latency_percentiles(entries),
        "per_op_latency": per_op_latency(entries),
        "route_distribution": route_distribution(entries),
        "process_fanout": process_fanout(entries),
        "outcome_split": outcome_split(entries),
        "pairing_summary": pairing_summary(repo_root=repo_root),
    }


def per_op_latency(entries: Iterable[dict]) -> Dict[str, dict]:
    """Same shape as `latency_percentiles`, keyed by `op`, for EVERY op
    present in `entries` — deliberately NOT limited to
    `cost_census.HOT_PATH_OPS` (see module docstring negative-spec)."""
    by_op: Dict[str, List[float]] = {}
    for entry in entries:
        if (entry.get("kind") or "complete") != "complete":
            continue
        op = entry.get("op")
        elapsed_ms = entry.get("elapsed_ms")
        if op is None or not isinstance(elapsed_ms, (int, float)):
            continue
        by_op.setdefault(op, []).append(float(elapsed_ms))
    return {op: _percentiles_for(vals) for op, vals in by_op.items()}
