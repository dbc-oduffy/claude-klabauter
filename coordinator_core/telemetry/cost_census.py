"""
coordinator_core.telemetry.cost_census — recurring, comparable cost snapshot.

Purpose: closes acceptance criterion 5 of
state/handoffs/2026-08-15-kill-it-if-it-cannot-pay-for-itself.md. Every cost
measurement the 2026-08-15/16 kill wing relied on was produced by someone
going looking AFTER a problem — the wing's own baton names that as the
failure pattern, and it cost real accuracy: the fleet spawn census lost ~30%
of its accuracy in a single day to the campaign's own sessions, three
attributions of the same incident were confidently wrong, and kill-ledger
entry K-004 sat undecidable for want of any instrumentation at all. A single
measurement is not a series and cannot show regression — that is the bar
this module exists to clear: run on a cadence, produce a NUMBER COMPARABLE
ACROSS RUNS, append it to a durable series (never overwrite), so a
regression is visible against prior runs rather than each run being a fresh,
un-comparable snapshot.

What is measured, and why (the dropped axes are dropped deliberately, not
by omission):
    - Per-op wall-clock (n/p50/p90/max elapsed_ms) for a NAMED set of
      hot-path ops (`_HOT_PATH_OPS` below) — read straight out of
      `coordinator_core.telemetry.op_latency`'s existing sink. This is the
      one axis the baton names that is ALREADY instrumented and durable:
      "per-invocation cost of anything on the commit/session hot path."
    - Vanished-invocation rate (`op_latency.pairing_summary`) — a free
      reuse of an existing reader; a rising unpaired-started rate is a
      leading indicator of the same fleet-degradation shape the 2026-08-15
      audit reconstructed after the fact, catchable BEFORE the fact here.
    - DROPPED: subprocess-spawn-count amplification per composed op. The
      baton names this as a candidate axis ("the amplification factor of
      composed ops"), but no existing durable counter measures it —
      `coordinator_core.composition_budget.CompositionBudget` counts
      invocations only in-memory, per-process, and is wired to a durable
      sink (`on_count`) by no caller today (see that module's own
      docstring, "RESERVED FOR THE PM -- NOT DECIDED HERE"). Measuring it
      here would mean either building a fresh probe (exactly the expensive,
      stale-by-construction pattern this module exists to replace — see
      module docstring "Prefer reading telemetry that already exists") or
      wiring `on_count` project-wide, which is separate build work this
      dispatch was not asked to do. Left as a named gap, not silently
      absent.
    - DROPPED: host-resource sampling (CPU/mem/disk pressure). A sibling
      executor is concurrently building a host-resource sampler reusing
      `coordinator_core.telemetry.log_rotation` (dispatch brief,
      2026-08-16) — duplicating that here would be redundant work on a
      shared-tree dispatch and risks a merge collision on the exact module
      this one is told not to touch. Once that sampler lands, its sink is
      the natural THIRD axis for a future revision of this census; not
      built here.

Cadence: daily, not continuous and not per-invocation. Justified against
the 50-70-concurrent-session load norm (docs/wiki/machine-load-norm.md):
    - Cost floor: `op-latency.jsonl` grows ~7.3 MB/day at that load
      (log_rotation.py's own measured constant) and rotates at 25 MiB /
      ~3-4 days, 4 generations kept (~100 MiB worst case). This module
      NEVER scans more than `LOOKBACK_SECS` (default 24h) of rows
      regardless of how much history is on disk — see `_iter_recent_rows`
      — so its own cost is bounded by one day of traffic, not by total
      corpus size, and does not grow as the corpus grows.
    - Why daily and not hourly: the 2026-08-15 incident lost 30% of the
      fleet census's accuracy in a SINGLE DAY — daily is the coarsest
      cadence that still catches a regression before it compounds across a
      full workweek, and no finer cadence is justified without evidence a
      day-scale regression is missed at day granularity.
    - Why daily and not weekly: `state/code-stats-history.md` (the cited
      precedent) is workweek-scoped because code volume moves slowly;
      op-latency reflects LIVE session behavior on a shared, 50-70-session
      box and can regress inside a single day, as the audit that
      motivated this module demonstrates directly.
    - Self-bounded: `run_census()` records its OWN elapsed wall-clock in
      the emitted row (`census_elapsed_ms`) and never reads more than
      `MAX_ROWS_SCANNED` (a ratchet, not a target — see that constant) rows
      total across the sink + its rotated generations, truncating (and
      saying so via `truncated: true`) rather than reading unboundedly.
      NO subprocess spawn anywhere in this module — the read side is
      pure-Python file I/O, matching CLAUDE.md's "content search / file
      location in-process" mandate and this module's own cheapness
      requirement.

Where the series lives: `state/cost-census.jsonl`, one JSON line per run,
newest appended last — the same flat-JSONL-with-date convention already
established by `state/backlog-snapshots.*.jsonl` (reused deliberately,
per the brief's "reuse a convention rather than inventing one"), not a
Markdown table like `state/code-stats-history.md` (that shape suits a
human-authored weekly narrative; this is a machine-appended series a
future script needs to diff programmatically).

Negative-spec:
    - Never spawns a subprocess. Never re-derives by probing live state —
      every number here is a read of op_latency's existing sink.
    - Never truncates or rewrites `state/cost-census.jsonl` — strictly
      append-only, one line per `run_census()` call, mirroring
      `atomic_append`'s discipline elsewhere in this package.
    - Never raises out of `run_census()` on a missing/unreadable sink —
      an absent op-latency.jsonl (e.g. a fresh install) produces a row
      with `n=0` for every op, not an exception; this module's own
      failure must never block whatever cadence invokes it.

Spec backlink: state/handoffs/2026-08-15-kill-it-if-it-cannot-pay-for-itself.md AC5
               docs/wiki/cost-budgets-and-the-kill-disposition.md
               docs/wiki/machine-load-norm.md
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

#: Named hot-path ops this census tracks (baton candidate axis: "per-
#: invocation cost of anything on the commit/session hot path"). A fixed,
#: reviewable list rather than "every op seen" so the series stays
#: comparable run over run even as new ops are added elsewhere — an op
#: NOT in this list is simply not part of the series yet, not silently
#: dropped from view (op-latency.jsonl itself still has every row).
#: `review_brightline_gate.from_handoff` (the K-004 C1 instrument) was
#: REMOVED from this tuple on 2026-08-19: the op it measured is gone
#: (state/kill-ledger.md K-007) and can never emit another row, so keeping
#: it would report a permanent zero as if it were a measurement. Its 117
#: historical rows remain in op-latency.jsonl and are quoted in K-007.
HOT_PATH_OPS: tuple = (
    "coverage.gate",
    "handoff.stamp",
    "session.claim_plan",
    "review_trail.write",
)

#: Ratchet, not a target: this module must never read an unbounded number
#: of lines even if `LOOKBACK_SECS` and rotation both fail to bound it
#: (e.g. a corrupted/unrotated sink). 2,000,000 is ~9x the 222,572-row
#: single-sink scale the 2026-08-15 fleet-degradation audit reconstructed
#: from — comfortably above any one day's traffic, comfortably below
#: "unbounded."
MAX_ROWS_SCANNED = 2_000_000

#: Default lookback window for "recent" rows — see module docstring's
#: cadence section for why 24h (daily) is the chosen granularity.
LOOKBACK_SECS_DEFAULT = 24 * 60 * 60


def _sink_paths(repo_root: Path) -> List[Path]:
    """The op-latency sink plus its rotated generations, newest first.

    Thin call-through to `op_latency.sink_generations` (promoted there
    2026-08-19, plan `2026-08-19-warm-engine-gets-an-honest-instrument`
    C1 — the rotation-aware resolver's supported home, chosen to avoid an
    import cycle once C3 makes `op_latency.pairing_summary` consume the
    same resolver). Kept as a wrapper, not inlined at call sites, so this
    module's existing (newest-first) output shape and every existing
    caller are unchanged."""
    from coordinator_core.telemetry.op_latency import sink_generations

    return sink_generations(repo_root)


def _percentile(sorted_vals: List[float], pct: float) -> Optional[float]:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = min(len(sorted_vals) - 1, int(round(pct * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


def _summarize_op(elapsed_ms_by_op: Dict[str, List[float]]) -> Dict[str, dict]:
    summary: Dict[str, dict] = {}
    for op in HOT_PATH_OPS:
        vals = sorted(elapsed_ms_by_op.get(op, []))
        summary[op] = {
            "n": len(vals),
            "p50_ms": _percentile(vals, 0.50),
            "p90_ms": _percentile(vals, 0.90),
            "max_ms": vals[-1] if vals else None,
        }
    return summary


def _series_path(repo_root: Path) -> Path:
    return repo_root / "state" / "cost-census.jsonl"


def run_census(
    *,
    repo_root: Path,
    now: Optional[float] = None,
    lookback_secs: float = LOOKBACK_SECS_DEFAULT,
    max_rows: int = MAX_ROWS_SCANNED,
    write: bool = True,
) -> dict:
    """Compute one census row and (if `write`) append it to
    `state/cost-census.jsonl`. Returns the row unconditionally so a caller
    (test, or an interactive run) can inspect it without re-reading disk.

    Never raises — a missing/unreadable sink degrades to `n=0` rows and an
    empty pairing summary, per module docstring negative-spec.
    """
    if now is None:
        now = time.time()

    census_perf_start = time.perf_counter()

    sink_paths = _sink_paths(repo_root)
    elapsed_ms_by_op: Dict[str, List[float]] = {op: [] for op in HOT_PATH_OPS}
    truncated = False
    rows_seen = 0

    cutoff = now - lookback_secs
    rows_read = 0
    for path in sink_paths:
        if rows_read >= max_rows:
            truncated = True
            break
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for raw_line in fh:
                    if rows_read >= max_rows:
                        truncated = True
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
                    if (entry.get("kind") or "complete") != "complete":
                        continue
                    t_start = entry.get("t_start")
                    if isinstance(t_start, (int, float)) and t_start < cutoff:
                        continue
                    op = entry.get("op")
                    elapsed_ms = entry.get("elapsed_ms")
                    if op in elapsed_ms_by_op and isinstance(elapsed_ms, (int, float)):
                        elapsed_ms_by_op[op].append(float(elapsed_ms))
                        rows_seen += 1
        except OSError:
            continue

    try:
        from coordinator_core.telemetry.op_latency import pairing_summary

        pairing = pairing_summary(repo_root=repo_root, now=now)
    except Exception:
        pairing = {
            "total": 0, "paired": 0, "unpaired_started": 0,
            "unpaired_rate": 0.0, "in_flight": 0,
            "malformed_lines_skipped": 0,
        }

    census_elapsed_ms = (time.perf_counter() - census_perf_start) * 1000.0

    row = {
        "date": time.strftime("%Y-%m-%d", time.gmtime(now)),
        "t_run": now,
        "lookback_secs": lookback_secs,
        "hot_path_ops": _summarize_op(elapsed_ms_by_op),
        "rows_matched": rows_seen,
        "rows_scanned": rows_read,
        "truncated": truncated,
        "vanished_invocation": pairing,
        "census_elapsed_ms": census_elapsed_ms,
    }

    if write:
        _append_row(_series_path(repo_root), row)

    return row


def _append_row(series_path: Path, row: dict) -> None:
    """Append-only write of one JSON line — never truncates/rewrites the
    file. Best-effort: a write failure here must not raise past this
    module's own negative-spec (never blocks whatever cadence calls it)."""
    try:
        os.makedirs(series_path.parent, exist_ok=True)
        line = json.dumps(row, separators=(",", ":")) + "\n"
        with open(series_path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line)
    except OSError:
        pass


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entrypoint — `python -m coordinator_core.telemetry.cost_census`.
    Resolves repo_root from cwd via `git rev-parse --show-toplevel`-free
    lookup (walks up for a `.git` marker) so this can be invoked from
    anywhere under the repo without a subprocess spawn."""
    repo_root = _find_repo_root(Path.cwd())
    if repo_root is None:
        print("cost_census: could not resolve repo root (no .git found)")
        return 1
    row = run_census(repo_root=repo_root)
    print(json.dumps(row, indent=2, default=str))
    return 0


def _find_repo_root(start: Path) -> Optional[Path]:
    current = start.resolve()
    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists():
            return candidate
    return None


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv[1:]))
