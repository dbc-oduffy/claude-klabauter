"""
coordinator_core.benchmarks.measure_read_events -- C5 cold-start read_events
measurement against the DR-215 per-op budget (AC3).

Purpose: measures `coordinator_core.tracker_store.read_events`'s cold-start
(interpreter-spawn-to-exit) latency across ALL shards under a synthetic
fixture tree, and produces the numbers AC3 requires:

  0. A 0-event/BASELINE_SHARD_COUNT control point, isolating
     interpreter-cold-start/import-chain cost from `read_events`'s own
     per-event work -- reproducible from this module, not hand-run and
     transcribed into a commit message.
  1. A measured figure at a STATED (events_per_shard, shard_count) pair.
  2. The extrapolated total-event count at which that figure would breach the
     DR-215 per-op budget, at the SAME stated shard count -- linear
     extrapolation from two measured points (see `_extrapolate_breach`),
     flagged as noise-dominated when the delta is not large relative to the
     combined sample stdev, and returning an explicit sentinel rather than a
     negative number when the baseline itself already exceeds the budget band.
  3. The measured delta from adding one additional shard, holding
     events_per_shard fixed.

Why two independent growth axes are measured, not one: `read_events` globs
every shard and merge-sorts the union (see tracker_store.py's module
docstring), so its cost grows with BOTH total event count and shard count
independently -- a shard-count-pinned breach figure alone cannot price
compaction against fleet growth (more shards over time) without re-measuring.
See docs/plans/2026-07-28-sat-01-sovereign-tracker-substrate.md § C5 / AC3.

Uses the EXISTING harness building blocks rather than a parallel measurement
path: `coordinator_core.benchmarks.budget.resolve_budget` resolves the
DR-215 COMPUTE_ONLY tier budget from `budget-manifest.json` (`read_events`
has no per-op override, so it resolves via the tier default -- see
`_read_events_probe.py`'s docstring for why it is not a registered op in the
first place), and the subprocess-timing shape mirrors
`coordinator_core.benchmarks.timer.time_invocation` (same
`SUBPROCESS_CREATIONFLAGS`/`SUBPROCESS_TIMEOUT_S` constants, same
spawn-to-exit `perf_counter` wrapper) even though there is no `invoke <op>`
to spawn -- `_read_events_probe.py` stands in for it.

Never point this at the real `state/` tree -- every fixture is materialized
under a fresh `tempfile.mkdtemp` root and torn down in a `finally`.

Spec backlink: pln-sat-01-sovereign-tracker-subst-a66742 § C5 (AC3).
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
from coordinator_core.benchmarks.timer import SUBPROCESS_CREATIONFLAGS, SUBPROCESS_TIMEOUT_S
from coordinator_core.tracker_store import EVENTS_DIR_RELPATH

DEFAULT_N = 10
"""Default timed-sample count per measured point (post warm-up)."""

DEFAULT_WARMUP = 2
"""Default warm-up spawns discarded before the timed sample window."""

BASELINE_EVENTS_PER_SHARD = 500
"""'Stated record count' half of AC3's first number's (events, shards) pair."""

BASELINE_SHARD_COUNT = 3
"""Matches 'three goals-log shards already exist on disk today' (plan § C5)."""

GROWTH_EVENTS_PER_SHARD = 5000
"""Second (larger) measurement point at the SAME shard count, used only to
derive the events-axis growth slope for the AC3-2 extrapolation -- 10x the
baseline events-per-shard is enough range to separate the growth signal from
per-sample scheduler noise without a multi-minute fixture-materialization
cost."""


def _make_event(idx: int, shard: int) -> dict:
    """One synthetic `applied_at`-populated event, shaped like a real
    tracker_store record (see tracker_store.py's ordering-contract docstring
    for the fields `read_events` reads: `applied_at`, `observed_at`, `id`).
    Values are deterministic in *idx*/*shard* except the `id` suffix, which is
    randomized so no two synthetic events across shards or measurement points
    collide."""
    return {
        "id": f"evt-shard{shard}-{idx:07d}-{uuid.uuid4().hex[:8]}",
        "observed_at": f"2026-07-28T00:00:{idx % 60:02d}.{idx % 1000:03d}Z",
        "applied_at": f"2026-07-28T00:01:{idx % 60:02d}.{idx % 1000:03d}Z",
        "machine": f"fixture-machine-{shard}",
        "sequence": idx + 1,
        "logical_clock": {"wall_ms": idx, "counter": 0},
        "kind": "fixture.noop",
    }


def materialize_fixture(root: Path, *, events_per_shard: int, shard_count: int) -> None:
    """Write `shard_count` shard files of `events_per_shard` synthetic
    events each under `root/EVENTS_DIR_RELPATH`, matching the real shard
    glob (`events.*.jsonl`) `read_events` scans."""
    shard_dir = root / EVENTS_DIR_RELPATH
    shard_dir.mkdir(parents=True, exist_ok=True)
    for shard in range(shard_count):
        lines = [
            json.dumps(_make_event(i, shard), sort_keys=True) for i in range(events_per_shard)
        ]
        shard_path = shard_dir / f"events.fixture-machine-{shard}.jsonl"
        shard_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def _time_probe(repo_root: Path) -> float:
    """Time one cold spawn of `_read_events_probe` against `repo_root`,
    from process-spawn to exit -- the same spawn-to-exit shape
    `timer.time_invocation` uses for real op invocations. Raises
    `RuntimeError` on a non-zero exit; a probe failure is never silently
    dropped as a sample."""
    argv = [
        sys.executable,
        "-m",
        "coordinator_core.benchmarks._read_events_probe",
        "--repo",
        str(repo_root),
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
            f"measure_read_events: probe failed rc={completed.returncode} "
            f"stderr={completed.stderr!r}"
        )
    return elapsed_ms


def measure(
    events_per_shard: int,
    shard_count: int,
    *,
    n: int = DEFAULT_N,
    warmup: int = DEFAULT_WARMUP,
) -> dict:
    """Materialize one fixture at (events_per_shard, shard_count), draw
    `warmup` discarded + `n` timed cold-start samples of `read_events`
    against it, and return the reduced statistics. Fixture is always
    materialized under a fresh temp root and removed in `finally` -- never
    points at the real repo `state/` tree."""
    tmp_root = Path(tempfile.mkdtemp(prefix="sat01-read-events-bench-"))
    try:
        materialize_fixture(tmp_root, events_per_shard=events_per_shard, shard_count=shard_count)

        for _ in range(warmup):
            _time_probe(tmp_root)

        samples_ms: List[float] = [_time_probe(tmp_root) for _ in range(n)]
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    return {
        "events_per_shard": events_per_shard,
        "shard_count": shard_count,
        "total_events": events_per_shard * shard_count,
        "sample_count": n,
        "min_ms": min(samples_ms),
        "mean_ms": statistics.mean(samples_ms),
        "stdev_ms": statistics.stdev(samples_ms) if len(samples_ms) > 1 else 0.0,
        "samples_ms": samples_ms,
    }


def _band_ms(target_ms: float, tolerance: dict) -> float:
    """Same band formula as `gate.evaluate` (relative -> target*(1+value),
    absolute -> target+value) -- duplicated here (not imported) because
    `gate.py` is deliberately a zero-I/O leaf over an already-resolved
    ConformanceRecord shape, not a general tolerance-math helper."""
    if tolerance["kind"] == "relative":
        return target_ms * (1 + tolerance["value"])
    if tolerance["kind"] == "absolute":
        return target_ms + tolerance["value"]
    raise ValueError(f"unknown tolerance.kind: {tolerance['kind']!r}")


ALREADY_BREACHED_SENTINEL = "already breached at baseline"
"""Returned by `_extrapolate_breach_total_events` in place of a total-event
count when the baseline point itself already exceeds `band_ms` -- a
downstream consumer must not have to infer this from a negative number."""

NOISE_STDEV_MULTIPLE = 2
"""`delta_ms` must exceed this many combined stdevs before the extrapolated
slope is treated as a real signal rather than sample noise (Finding 3)."""


def _extrapolate_breach_total_events(
    baseline: dict, growth_point: dict, band_ms: float
) -> Union[float, str, None]:
    """Linear extrapolation (secant through the two measured `min_ms` points,
    both at the SAME shard count) for the total-event count at which
    `read_events`'s `min_ms` would cross `band_ms`. Returns `None` if the
    slope is non-positive (no measurable growth over the sampled range --
    extrapolation would be meaningless, not a false 'never breaches').
    Returns `ALREADY_BREACHED_SENTINEL` if the baseline point already
    exceeds `band_ms` -- the formula below is only valid for a breach point
    that hasn't happened yet; falling through would emit a negative,
    already-in-the-past 'breach point' exactly when the baseline is already
    over budget."""
    # Review: code-reviewer -- baseline["min_ms"] > band_ms made
    # (band_ms - baseline["min_ms"]) negative before any slope was applied,
    # so a small positive slope produced a negative total-event count. This
    # branch makes the already-breached case an explicit, unambiguous
    # sentinel instead of a number a downstream reader has to interpret.
    if baseline["min_ms"] > band_ms:
        return ALREADY_BREACHED_SENTINEL
    delta_events = growth_point["total_events"] - baseline["total_events"]
    delta_ms = growth_point["min_ms"] - baseline["min_ms"]
    if delta_events <= 0:
        raise ValueError("_extrapolate_breach_total_events: non-positive event delta")
    slope_ms_per_event = delta_ms / delta_events
    if slope_ms_per_event <= 0:
        return None
    return baseline["total_events"] + (band_ms - baseline["min_ms"]) / slope_ms_per_event


def _is_noise_dominated(baseline: dict, growth_point: dict) -> dict:
    """Compare the measured `min_ms` delta between `baseline` and
    `growth_point` against `NOISE_STDEV_MULTIPLE` times their combined
    `stdev_ms` (Finding 3): a positive-but-noise-driven slope should not be
    reported with the same confidence as a real signal. Returns a small dict
    (not a bare bool) so the comparison values travel with the flag."""
    delta_ms = growth_point["min_ms"] - baseline["min_ms"]
    noise_floor_ms = NOISE_STDEV_MULTIPLE * (baseline["stdev_ms"] + growth_point["stdev_ms"])
    return {
        "noise_dominated": delta_ms < noise_floor_ms,
        "delta_ms": delta_ms,
        "noise_floor_ms": noise_floor_ms,
    }


def run_ac3_measurement(*, n: int = DEFAULT_N, warmup: int = DEFAULT_WARMUP) -> dict:
    """Run the full AC3 measurement: a 0-event/BASELINE_SHARD_COUNT control
    point, the baseline point, an events-axis growth point (same shard
    count), and a shard-axis delta point (same events_per_shard, one
    additional shard). Returns a dict carrying the AC3 numbers plus the raw
    measured points for audit.

    The 0-event control isolates interpreter-cold-start/import-chain cost
    from `read_events`'s own per-event work -- it must be captured here, not
    hand-run and transcribed into a commit message, so the conclusion it
    supports is reproducible from this committed artifact.
    # Review: code-reviewer -- this control point drove the headline
    # "cost is cold-start, not per-event work" conclusion but was never
    # part of the shipped, reusable measurement path; added as a real
    # measured point rather than a memory-transcribed number.
    """
    manifest = budget_mod.load_manifest()
    budget = budget_mod.resolve_budget("tracker.read_events", "COMPUTE_ONLY", manifest)
    target_ms = budget["target_ms"]
    band_ms = _band_ms(target_ms, budget["tolerance"])

    zero_event_control = measure(0, BASELINE_SHARD_COUNT, n=n, warmup=warmup)
    baseline = measure(BASELINE_EVENTS_PER_SHARD, BASELINE_SHARD_COUNT, n=n, warmup=warmup)
    growth_point = measure(GROWTH_EVENTS_PER_SHARD, BASELINE_SHARD_COUNT, n=n, warmup=warmup)
    shard_delta_point = measure(
        BASELINE_EVENTS_PER_SHARD, BASELINE_SHARD_COUNT + 1, n=n, warmup=warmup
    )

    breach_total_events = _extrapolate_breach_total_events(baseline, growth_point, band_ms)
    shard_delta_ms = shard_delta_point["min_ms"] - baseline["min_ms"]
    noise_check = _is_noise_dominated(baseline, growth_point)

    return {
        "budget_target_ms": target_ms,
        "budget_tolerance": budget["tolerance"],
        "budget_band_ms": band_ms,
        "zero_event_control": zero_event_control,
        "baseline": baseline,
        "growth_point": growth_point,
        "shard_delta_point": shard_delta_point,
        "extrapolated_breach_total_events_at_shard_count": {
            "shard_count": BASELINE_SHARD_COUNT,
            "total_events": breach_total_events,
            "noise_dominated": noise_check["noise_dominated"],
            "noise_floor_ms": noise_check["noise_floor_ms"],
        },
        "measured_shard_delta_ms": {
            "from_shard_count": BASELINE_SHARD_COUNT,
            "to_shard_count": BASELINE_SHARD_COUNT + 1,
            "events_per_shard": BASELINE_EVENTS_PER_SHARD,
            "delta_ms": shard_delta_ms,
        },
    }


if __name__ == "__main__":  # pragma: no cover
    declare_benchmark_origin()
    result = run_ac3_measurement()
    print(json.dumps(result, indent=2, default=str))
