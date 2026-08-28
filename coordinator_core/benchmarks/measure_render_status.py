"""
coordinator_core.benchmarks.measure_render_status -- C10 projection
cold-start replay-cost measurement against the DR-215 per-op budget
(AC16, AC17).

Purpose: `sat-01`'s AC3 (`measure_read_events.py`) measured
`tracker_store.read_events`'s cold-start cost only. `tracker_projection`'s
fold (`current_state`/`render_status`, via `_fold_axis_states`) sits ON TOP
of that read and is a separate, unmeasured cost -- C5 (`tracker_transitions
.snapshot_axis_if_due`/`emit_snapshot_event`) justifies its compaction
snapshot as bounding that replay cost, but nothing before this module
measured it. This module produces the two numbers AC16/AC17 require:

  1. **Uncompacted.** `render_status` cold-started (interpreter-spawn-to-
     exit, via `_render_status_probe.py`, exactly as `measure_read_events`
     times `_read_events_probe.py`) over a synthetic log at a STATED
     (events_per_shard, shard_count) pair, checked against the resolved
     DR-215 budget, plus the extrapolated total-event count at which that
     budget would breach (same secant-extrapolation shape as
     `measure_read_events._extrapolate_breach_total_events`).
  2. **Compacted.** The SAME log, except the benchmark item's own axis
     history is folded into one `kind: "snapshot"` event (C5's
     `build_snapshot_event` shape) covering all but a short unsnapshotted
     tail -- re-measured, and the delta reported.

**Read this before interpreting the compacted number (AC17).** A
near-zero delta is a VALID, PREDICTED result, not a tuning failure. A
snapshot bounds per-item FOLD ARITHMETIC (fewer `to_state` assignments
inside `_fold_axis_states`'s loop) -- it does NOT bound log size or read
I/O: `tracker_store.read_events` unconditionally `read_text()`s and
`json.loads()`s every line of every shard before the fold's content-bound
skip set can apply to a single event (see
`tracker_transitions.emit_snapshot_event`'s own docstring, which names
this module as the one that measures that cost). On a spawn-per-call
engine, that parse/IO cost dominates a cold start; this module's
background "noise" events (which carry no `axis` field and are skipped in
one dict-membership check) make that dominance the majority of every
measured sample by construction, matching a real shard population where
one item's own axis history is a small fraction of the total log. Do not
iterate on the fixture shape or the projection implementation to
manufacture a bigger compacted-vs-uncompacted delta; report the number
measured.

**Load caveat.** This machine routinely runs 50-70 concurrent LLM
sessions; treat a slow sample as a loaded box before a regression. See
`run_c10_measurement`'s `n`/`warmup` for the sampling knobs; the module
entry point takes the median of `DEFAULT_N` post-warmup samples per point,
not a single draw.

Never points at the real `state/` tree -- every fixture is materialized
under a fresh `tempfile.mkdtemp` root and torn down in a `finally`,
exactly as `measure_read_events.materialize_fixture` does.

Spec backlink: pln-sat-03-event-sourced-completio-c270a1
§ Tasks C10 (AC16, AC17).
"""

from __future__ import annotations

import json
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import List, Optional, Union

from coordinator_core.benchmarks import budget as budget_mod
from coordinator_core.benchmarks import declare_benchmark_origin
from coordinator_core.benchmarks.measure_read_events import (
    _band_ms,
    _extrapolate_breach_total_events,
    _is_noise_dominated,
    ALREADY_BREACHED_SENTINEL,
)
from coordinator_core.benchmarks.timer import SUBPROCESS_CREATIONFLAGS, SUBPROCESS_TIMEOUT_S
from coordinator_core.tracker_store import EVENTS_DIR_RELPATH

DEFAULT_N = 10
"""Default timed-sample count per measured point (post warm-up) -- matches
`measure_read_events.DEFAULT_N` so the two measurements are drawn under the
same sampling discipline."""

DEFAULT_WARMUP = 2
"""Default warm-up spawns discarded before the timed sample window."""

BASELINE_EVENTS_PER_SHARD = 500
"""Background 'noise' events per shard -- same figure as
`measure_read_events.BASELINE_EVENTS_PER_SHARD`, so this module's
uncompacted baseline point is directly comparable to sat-01's AC3
baseline."""

BASELINE_SHARD_COUNT = 3
"""Matches `measure_read_events.BASELINE_SHARD_COUNT` (three shards already
exist on disk today, per the plan)."""

GROWTH_EVENTS_PER_SHARD = 5000
"""Second (larger) measurement point at the SAME shard count, used only to
derive the events-axis growth slope for the AC16 breach extrapolation --
mirrors `measure_read_events.GROWTH_EVENTS_PER_SHARD`."""

TARGET_ITEM_ID = "bench-item-0001"
"""The single item whose status this module renders on every probe call."""

TARGET_AXIS = "manual_close"
"""The axis folded for `TARGET_ITEM_ID` -- an arbitrary member of
`tracker_transitions.TRANSITION_AXES`; the fold cost of the specific code
path this module exercises (`render_status`/`_fold_axis_states`'s raw
iteration) does not depend on which axis is chosen. Narrower than a claim
about `tracker_transitions.py` cost in general -- this benchmark never
calls `reopen_cascade` or exercises axis-specific `_ASSERTED_TO_STATE`/
`_RETRACT_TO_STATE` logic (Review: coordinator:code-reviewer P3)."""

TARGET_ITEM_EVENT_COUNT = 300
"""Transition events minted for `TARGET_ITEM_ID` on `TARGET_AXIS` in the
UNCOMPACTED fixture -- a realistic-sized single-item history, small
relative to `BASELINE_EVENTS_PER_SHARD * BASELINE_SHARD_COUNT` background
events so the fixture's background-to-target ratio matches a real shard
population."""

SNAPSHOT_TAIL_COUNT = 5
"""Events left UNSNAPSHOTTED after the target item's history is folded in
the COMPACTED fixture -- mirrors `tracker_transitions
._events_for_axis_since_last_snapshot`'s 'events since last snapshot' tail
shape: a snapshot covers everything up to itself, never the live tail."""


def _make_noise_event(idx: int, shard: int) -> dict:
    """One synthetic background event carrying NO `axis` field -- skipped
    by `tracker_projection._fold_axis_states`'s `if axis not in states:
    continue` check in one dict-membership test, exactly the shape a real
    non-transition tracker event (e.g. an `item_project_added` membership
    edge) would take through that loop. Mirrors
    `measure_read_events._make_event`'s shape (same field set) so the two
    modules' background-event cost is comparable."""
    return {
        "id": f"evt-noise-shard{shard}-{idx:07d}-{uuid.uuid4().hex[:8]}",
        "observed_at": f"2026-07-28T00:00:{idx % 60:02d}.{idx % 1000:03d}Z",
        "applied_at": f"2026-07-28T00:01:{idx % 60:02d}.{idx % 1000:03d}Z",
        "machine": f"fixture-machine-{shard}",
        "sequence": idx + 1,
        "logical_clock": {"wall_ms": idx, "counter": 0},
        "kind": "fixture.noop",
    }


def _make_transition_event(idx: int, *, item_id: str, axis: str, applied_hour: int) -> dict:
    """One synthetic transition event on `(item_id, axis)`, field-shaped
    like `tracker_transitions._emit`'s stored output (see that module's
    docstring "Event fields (binding, closed list)") -- alternates
    `to_state` between "open"/"closed"-style values so a real fold does
    real per-event `to_state` assignment work, not a no-op comparison."""
    to_state = "closed" if idx % 2 == 0 else "reopened"
    return {
        "id": f"evt-transition-{item_id}-{axis}-{idx:07d}",
        "item_id": item_id,
        "axis": axis,
        "from_state": None if idx == 0 else "reopened",
        "to_state": to_state,
        "actor": "bench-fixture",
        "evidence": None,
        "tier": "direct",
        "source_observation_id": None,
        "observed_at": f"2026-07-28T{applied_hour % 24:02d}:00:{idx % 60:02d}.{idx % 1000:03d}Z",
        "applied_at": f"2026-07-28T{applied_hour % 24:02d}:00:{idx % 60:02d}.{idx % 1000:03d}Z",
        "machine": "fixture-machine-0",
        "sequence": idx + 1,
        "logical_clock": {"wall_ms": idx, "counter": 0},
        "schema_version": 1,
    }


def _make_snapshot_event(
    *, item_id: str, axis: str, folded_event_ids: list, folded_to_state: str
) -> dict:
    """One synthetic `kind: "snapshot"` event, field-shaped like
    `tracker_transitions.build_snapshot_event`'s stored output (C5) --
    `folded_event_ids` is the exact-identity skip set
    `tracker_projection._fold_axis_states` consumes; `as_of_sequence`/
    `as_of_applied_at` are provenance-only fields that fold never reads
    (see that module's docstring), stamped here only for shape fidelity."""
    return {
        "id": f"evt-snapshot-{item_id}-{axis}",
        "item_id": item_id,
        "axis": axis,
        "kind": "snapshot",
        "folded_event_ids": list(folded_event_ids),
        "as_of_sequence": len(folded_event_ids),
        "as_of_applied_at": "2026-07-28T01:00:00.000Z",
        "folded_to_state": folded_to_state,
        "observed_at": "2026-07-28T01:00:00.000Z",
        "applied_at": "2026-07-28T01:00:00.000Z",
        "folded_at": "2026-07-28T01:00:00.000Z",
        "schema_version": 1,
        "machine": "fixture-machine-0",
        "sequence": 0,
        "logical_clock": {"wall_ms": 0, "counter": 0},
    }


def materialize_fixture(
    root: Path,
    *,
    events_per_shard: int,
    shard_count: int,
    compacted: bool,
    item_id: str = TARGET_ITEM_ID,
    axis: str = TARGET_AXIS,
    target_event_count: int = TARGET_ITEM_EVENT_COUNT,
) -> int:
    """Write `shard_count` shard files of `events_per_shard` synthetic
    background events each, plus `item_id`'s `axis` transition history, to
    `root/EVENTS_DIR_RELPATH` -- matching the real shard glob
    (`events.*.jsonl`) `read_events` scans.

    UNCOMPACTED (`compacted=False`): all `target_event_count` transition
    events for `(item_id, axis)` are written raw into shard 0.

    COMPACTED (`compacted=True`): the first `target_event_count -
    SNAPSHOT_TAIL_COUNT` of those events are folded into ONE `kind:
    "snapshot"` event (C5's shape) and the remaining `SNAPSHOT_TAIL_COUNT`
    are left raw -- mirrors `snapshot_axis_if_due`'s "events since last
    snapshot" tail (fold-on-close, not a truncating rewrite).

    Returns the total event count actually written (AC16 requires the
    record count be stated, not merely the background events_per_shard *
    shard_count figure -- the target item's own history and, in the
    compacted case, the one snapshot event both count).
    """
    shard_dir = root / EVENTS_DIR_RELPATH
    shard_dir.mkdir(parents=True, exist_ok=True)

    total_written = 0
    for shard in range(shard_count):
        lines = [
            json.dumps(_make_noise_event(i, shard), sort_keys=True)
            for i in range(events_per_shard)
        ]
        total_written += events_per_shard

        if shard == 0:
            transitions = [
                _make_transition_event(i, item_id=item_id, axis=axis, applied_hour=1)
                for i in range(target_event_count)
            ]
            if compacted:
                folded = transitions[: target_event_count - SNAPSHOT_TAIL_COUNT]
                tail = transitions[target_event_count - SNAPSHOT_TAIL_COUNT :]
                folded_to_state = folded[-1]["to_state"] if folded else None
                snapshot = _make_snapshot_event(
                    item_id=item_id,
                    axis=axis,
                    folded_event_ids=[e["id"] for e in folded],
                    folded_to_state=folded_to_state,
                )
                shard_events = [snapshot] + tail
                total_written += 1 + len(tail)
            else:
                shard_events = transitions
                total_written += len(transitions)
            lines.extend(json.dumps(e, sort_keys=True) for e in shard_events)

        shard_path = shard_dir / f"events.fixture-machine-{shard}.jsonl"
        shard_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    return total_written


def _time_bare_import(module_name: str) -> float:
    """Time one cold subprocess spawn that does nothing but `import
    <module_name>` -- process-spawn to exit, same timing shape as
    `_time_probe`. Isolates import cost from fold cost for the C10 AC16
    import-chain attribution (Review: coordinator:code-reviewer P2 --
    `PHASE-0-MEASUREMENTS.md` cited a ~333ms/~79ms bare-import split with
    no script on disk producing it; this is that script)."""
    argv = [sys.executable, "-c", f"import {module_name}"]
    start = time.perf_counter()
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_S,
        creationflags=SUBPROCESS_CREATIONFLAGS,
    )
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    if completed.returncode != 0:
        raise RuntimeError(
            f"measure_render_status: bare-import probe failed for "
            f"{module_name!r} rc={completed.returncode} stderr={completed.stderr!r}"
        )
    return elapsed_ms


def measure_bare_import(module_name: str, *, n: int = DEFAULT_N, warmup: int = DEFAULT_WARMUP) -> dict:
    """Draw `warmup` discarded + `n` timed cold-start samples of a bare
    `import <module_name>` subprocess and return reduced statistics --
    same reduction shape as `measure`, so the two are directly comparable.
    """
    for _ in range(warmup):
        _time_bare_import(module_name)
    samples_ms: List[float] = [_time_bare_import(module_name) for _ in range(n)]
    return {
        "module": module_name,
        "sample_count": n,
        "min_ms": min(samples_ms),
        "mean_ms": statistics.mean(samples_ms),
        "median_ms": statistics.median(samples_ms),
        "stdev_ms": statistics.stdev(samples_ms) if len(samples_ms) > 1 else 0.0,
        "samples_ms": samples_ms,
    }


def run_import_isolation_measurement(*, n: int = DEFAULT_N, warmup: int = DEFAULT_WARMUP) -> dict:
    """C10 AC16 import-chain attribution: bare-import cost of
    `tracker_projection` vs `tracker_store`, isolated from fold/read cost.
    Produces the numbers `PHASE-0-MEASUREMENTS.md`'s C10 section cites --
    prior to this function existing, no script on disk reproduced them
    (Review: coordinator:code-reviewer P2)."""
    return {
        "tracker_projection": measure_bare_import(
            "coordinator_core.tracker_projection", n=n, warmup=warmup
        ),
        "tracker_store": measure_bare_import(
            "coordinator_core.tracker_store", n=n, warmup=warmup
        ),
    }


def _time_probe(repo_root: Path, item_id: str) -> float:
    """Time one cold spawn of `_render_status_probe` against `repo_root`,
    from process-spawn to exit -- same shape as
    `measure_read_events._time_probe`. Raises `RuntimeError` on a non-zero
    exit; a probe failure is never silently dropped as a sample."""
    argv = [
        sys.executable,
        "-m",
        "coordinator_core.benchmarks._render_status_probe",
        "--repo",
        str(repo_root),
        "--item-id",
        item_id,
    ]
    start = time.perf_counter()
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_S,
        creationflags=SUBPROCESS_CREATIONFLAGS,
    )
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    if completed.returncode != 0:
        raise RuntimeError(
            f"measure_render_status: probe failed rc={completed.returncode} "
            f"stderr={completed.stderr!r}"
        )
    return elapsed_ms


def measure(
    events_per_shard: int,
    shard_count: int,
    *,
    compacted: bool,
    n: int = DEFAULT_N,
    warmup: int = DEFAULT_WARMUP,
) -> dict:
    """Materialize one fixture at (events_per_shard, shard_count),
    compacted or not, draw `warmup` discarded + `n` timed cold-start
    samples of `render_status` against it, and return the reduced
    statistics. Fixture is always materialized under a fresh temp root and
    removed in `finally` -- never points at the real repo `state/` tree."""
    tmp_root = Path(tempfile.mkdtemp(prefix="sat03-render-status-bench-"))
    try:
        total_events = materialize_fixture(
            tmp_root,
            events_per_shard=events_per_shard,
            shard_count=shard_count,
            compacted=compacted,
        )

        for _ in range(warmup):
            _time_probe(tmp_root, TARGET_ITEM_ID)

        samples_ms: List[float] = [_time_probe(tmp_root, TARGET_ITEM_ID) for _ in range(n)]
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    return {
        "events_per_shard": events_per_shard,
        "shard_count": shard_count,
        "compacted": compacted,
        "total_events": total_events,
        "sample_count": n,
        "min_ms": min(samples_ms),
        "mean_ms": statistics.mean(samples_ms),
        "median_ms": statistics.median(samples_ms),
        "stdev_ms": statistics.stdev(samples_ms) if len(samples_ms) > 1 else 0.0,
        "samples_ms": samples_ms,
    }


def run_c10_measurement(*, n: int = DEFAULT_N, warmup: int = DEFAULT_WARMUP) -> dict:
    """Run the full C10 measurement (AC16, AC17): an uncompacted baseline
    point, an uncompacted events-axis growth point (same shard count, for
    the breach extrapolation), and the compacted point at the baseline
    (events_per_shard, shard_count). Returns a dict carrying every AC16/
    AC17 number plus the raw measured points for audit.

    Resolves the DR-215 budget the same way `measure_read_events` does --
    via `budget.resolve_budget`, tier default (`tracker.render_status` has
    no per-op override in `budget-manifest.json`, matching
    `tracker.read_events`'s own unlisted status there).
    """
    manifest = budget_mod.load_manifest()
    budget = budget_mod.resolve_budget("tracker.render_status", "COMPUTE_ONLY", manifest)
    target_ms = budget["target_ms"]
    band_ms = _band_ms(target_ms, budget["tolerance"])

    uncompacted_baseline = measure(
        BASELINE_EVENTS_PER_SHARD, BASELINE_SHARD_COUNT, compacted=False, n=n, warmup=warmup
    )
    uncompacted_growth = measure(
        GROWTH_EVENTS_PER_SHARD, BASELINE_SHARD_COUNT, compacted=False, n=n, warmup=warmup
    )
    compacted_baseline = measure(
        BASELINE_EVENTS_PER_SHARD, BASELINE_SHARD_COUNT, compacted=True, n=n, warmup=warmup
    )

    breach_total_events = _extrapolate_breach_total_events(
        uncompacted_baseline, uncompacted_growth, band_ms
    )
    noise_check = _is_noise_dominated(uncompacted_baseline, uncompacted_growth)

    compaction_delta_ms = compacted_baseline["min_ms"] - uncompacted_baseline["min_ms"]
    compaction_noise_floor_ms = 2 * (
        uncompacted_baseline["stdev_ms"] + compacted_baseline["stdev_ms"]
    )

    return {
        "budget_op": "tracker.render_status",
        "budget_target_ms": target_ms,
        "budget_tolerance": budget["tolerance"],
        "budget_band_ms": band_ms,
        "uncompacted_baseline": uncompacted_baseline,
        "uncompacted_growth": uncompacted_growth,
        "compacted_baseline": compacted_baseline,
        "uncompacted_within_budget": uncompacted_baseline["min_ms"] <= band_ms,
        "extrapolated_breach_total_events_at_shard_count": {
            "shard_count": BASELINE_SHARD_COUNT,
            "total_events": breach_total_events,
            "noise_dominated": noise_check["noise_dominated"],
            "noise_floor_ms": noise_check["noise_floor_ms"],
        },
        "compaction_delta_ms": compaction_delta_ms,
        "compaction_delta_noise_dominated": abs(compaction_delta_ms) < compaction_noise_floor_ms,
        "compaction_delta_noise_floor_ms": compaction_noise_floor_ms,
    }


if __name__ == "__main__":  # pragma: no cover
    declare_benchmark_origin()
    result = run_c10_measurement()
    result["import_isolation"] = run_import_isolation_measurement()
    print(json.dumps(result, indent=2, default=str))
