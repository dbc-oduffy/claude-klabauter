"""
coordinator_core.benchmarks.harness -- Per-op N-iteration benchmark loop + record assembly.

Purpose: the integration point for the qsub-01 latency benchmark harness. For
each op in the target set, collects N samples end-to-end via the shared timer
(`coordinator_core.benchmarks.timer:time_invocation` -- imported, not
re-authored here), discards warm-up run(s), computes the min/p50/p95/p99
distribution summary, resolves the op's budget (`budget.resolve_budget`),
evaluates a verdict (`gate.evaluate`), and assembles a `ConformanceRecord`
(`record.py`) per op. `run()` is the entrypoint C7's CLI runner drives.

Fixture lifetime: `run()` owns materializing ONE synthetic fixture repo (via
`op_fixtures.materialize_fixture_repo`) for the whole sweep -- worktree-scoped
ops in the target set all share that single materialized repo, matching
op_fixtures' documented "once per benchmark run" contract. Bare-scoped ops
never touch the fixture.

Run identity: `code_sha` is captured via a SINGLE `git rev-parse HEAD`
subprocess call at `run()` entry and stamped onto every record produced by
that call -- deterministic run identity even if a concurrent session moves
HEAD mid-sweep (shared-worktree reality; see record.py's code_sha docstring).
The cold-start floor (`floor.measure_floor`) is likewise measured ONCE for
the whole run (`floor_scope="run"`) and shared across every record.

AC9 (exit-code / error-body guard): every sample is drawn through
`timer.time_invocation`, which raises `BenchmarkSampleInvalid` on a non-zero
exit or a JSON-RPC error envelope. This loop does not catch, swallow, or
retry that exception -- an erroring op fails the whole run loud, per AC9's
"never skip it, never record it as a timing sample" contract.

Spec backlink: pln-qsub-01-per-op-end-to-end-late-53ff10 § C6
(AC1, AC2, AC8).
"""

from __future__ import annotations

import statistics
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from coordinator_core.authz.classification import OP_CLASSIFICATION
from coordinator_core.benchmarks import budget as budget_mod
from coordinator_core.benchmarks import floor as floor_mod
from coordinator_core.benchmarks import gate as gate_mod
from coordinator_core.benchmarks import op_fixtures
from coordinator_core.benchmarks.record import FLOOR_SCOPE_RUN, ConformanceRecord, Tolerance
from coordinator_core.benchmarks.timer import (
    SUBPROCESS_CREATIONFLAGS,
    SUBPROCESS_TIMEOUT_S,
    time_invocation,
)

DEFAULT_N = 10
"""Default sample count per op (post-warm-up). Overridable via run(n=...)."""

DEFAULT_WARMUP = 1
"""Default number of warm-up runs discarded before the timed sample window."""

DEFAULT_FLOOR_N = 10
"""Default sample count for the run-level cold-start floor probe (floor.py)."""


def _capture_code_sha() -> str:
    """Run `git rev-parse HEAD` ONCE and return the full 40-char SHA.

    Purpose: run-level identity capture (see module docstring) -- callers must
    NOT re-shell this per op. Fails loud (raises) if git is unavailable or the
    working tree has no HEAD; a benchmark run with an unresolvable code_sha is
    not a usable measurement.
    """
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
        timeout=SUBPROCESS_TIMEOUT_S,
        creationflags=SUBPROCESS_CREATIONFLAGS,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"harness: git rev-parse HEAD failed: exit={completed.returncode} "
            f"stderr={completed.stderr!r}"
        )
    return completed.stdout.strip()


def _percentile(sorted_samples: List[float], pct: float) -> float:
    """Nearest-rank percentile over an already-sorted sample list.

    Purpose: small, dependency-free percentile helper -- statistics.quantiles
    requires n>=2 and uses interpolation methods that diverge across Python
    versions; nearest-rank keeps p50/p95/p99 simple and deterministic for the
    small N this harness runs.
    """
    if not sorted_samples:
        raise ValueError("_percentile: empty sample list")
    if len(sorted_samples) == 1:
        return sorted_samples[0]
    rank = pct / 100.0 * (len(sorted_samples) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(sorted_samples) - 1)
    frac = rank - lower
    return sorted_samples[lower] + (sorted_samples[upper] - sorted_samples[lower]) * frac


def _collect_samples(
    op: str,
    params_json: str,
    repo: Optional[str],
    n: int,
    warmup: int,
) -> List[float]:
    """Draw `warmup` discarded runs + `n` timed samples for one op via the
    shared timer. Propagates BenchmarkSampleInvalid (AC9) unmodified -- an
    erroring sample (warm-up or timed) fails the op loud, it is never skipped
    or substituted.
    """
    for _ in range(warmup):
        time_invocation(op, params_json, repo)

    return [time_invocation(op, params_json, repo) for _ in range(n)]


def _default_target_ops() -> List[str]:
    """The 20-op COMPUTE_ONLY wave-1 target set, sourced from op_fixtures'
    registry (which itself is scoped to the pinned COMPUTE_ONLY op set)."""
    return list(op_fixtures.COMPUTE_ONLY_FIXTURES.keys())


def run(
    ops: Optional[List[str]] = None,
    n: int = DEFAULT_N,
    warmup: int = DEFAULT_WARMUP,
    floor_n: int = DEFAULT_FLOOR_N,
    runner_isolation_mode: str = "shared",
) -> List[ConformanceRecord]:
    """Run the per-op end-to-end latency benchmark sweep and return one
    ConformanceRecord per op.

    Args:
        ops: op names to benchmark. Defaults to the full 20-op COMPUTE_ONLY
             wave-1 target set (op_fixtures.COMPUTE_ONLY_FIXTURES keys).
        n: timed sample count per op (post-warm-up).
        warmup: warm-up runs discarded per op before timing begins.
        floor_n: sample count for the run-level cold-start floor probe.
        runner_isolation_mode: stamped onto every record ('shared' default --
             this is an ad-hoc dev-loop harness, not an isolated CI runner).

    Fails loud: any BenchmarkSampleInvalid (AC9) or fixture-materialization
    error propagates unmodified -- the whole run stops rather than silently
    dropping an op.
    """
    target_ops = ops if ops is not None else _default_target_ops()

    # Review: code-reviewer (Slice B F2, P2) — fail loud on a plausible fat-finger
    # (`--n 0`) before any subprocess spawn, naming the bad param, instead of an
    # opaque IndexError deep in the sample-collection loop below.
    if n < 1:
        raise ValueError(f"harness.run: n must be >= 1, got {n!r}")
    if warmup < 0:
        raise ValueError(f"harness.run: warmup must be >= 0, got {warmup!r}")

    code_sha = _capture_code_sha()
    run_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).isoformat()

    floor_stats = floor_mod.measure_floor(floor_n)
    cold_start_floor_ms = floor_stats["cold_start_floor_ms"]
    floor_cov = floor_stats["floor_cov"]

    manifest = budget_mod.load_manifest()
    min_gating_sample_count = manifest["min_gating_sample_count"]

    needs_worktree = any(
        op_fixtures.COMPUTE_ONLY_FIXTURES[op]["scope"] == "worktree" for op in target_ops
    )

    # Review: code-reviewer (Slice B F7, P2) — materialize_fixture_repo() moved inside
    # the try/finally: a mid-materialization failure (e.g. the bare-origin clone step
    # raising after `dest` was already created and seeded) previously propagated before
    # worktree_root was assigned and before the finally block existed, leaking the
    # partially materialized fixture directory. worktree_root is now assigned before
    # the call so the finally's cleanup always has a value to check.
    worktree_root: Optional[Path] = None
    records: List[ConformanceRecord] = []
    try:
        if needs_worktree:
            worktree_root = op_fixtures.materialize_fixture_repo()

        for op in target_ops:
            entry = op_fixtures.COMPUTE_ONLY_FIXTURES[op]
            if entry["scope"] == "bare":
                params_json = entry["params_json"]
                repo_arg: Optional[str] = None
            else:
                assert worktree_root is not None
                params_json = op_fixtures.params_json_for(op, worktree_root)
                repo_path = op_fixtures.repo_arg_for(op, worktree_root)
                repo_arg = str(repo_path) if repo_path is not None else None

            samples_ms = _collect_samples(op, params_json, repo_arg, n, warmup)
            sorted_samples = sorted(samples_ms)

            min_ms = sorted_samples[0]
            p50_ms = _percentile(sorted_samples, 50)
            p95_ms = _percentile(sorted_samples, 95)
            p99_ms = _percentile(sorted_samples, 99)

            op_class_enum = OP_CLASSIFICATION[op]
            op_class_name = op_class_enum.name

            resolved_budget = budget_mod.resolve_budget(op, op_class_enum, manifest)
            target_ms = resolved_budget["target_ms"]
            tolerance = Tolerance.from_dict(resolved_budget["tolerance"])

            gating_statistic = "min"
            gating_statistic_value = min_ms

            verdict = gate_mod.evaluate(
                observed_statistic=gating_statistic_value,
                target_ms=target_ms,
                tolerance=tolerance,
                sample_count=len(samples_ms),
                min_gating_sample_count=min_gating_sample_count,
            )

            floor_delta_ms = gating_statistic_value - cold_start_floor_ms

            record = ConformanceRecord(
                op=op,
                op_class=op_class_name,
                target_ms=target_ms,
                tolerance=tolerance,
                gating_statistic=gating_statistic,
                gating_statistic_value=gating_statistic_value,
                min=min_ms,
                p50=p50_ms,
                p95=p95_ms,
                p99=p99_ms,
                sample_count=len(samples_ms),
                cold_start_floor_ms=cold_start_floor_ms,
                floor_delta_ms=floor_delta_ms,
                floor_cov=floor_cov,
                floor_scope=FLOOR_SCOPE_RUN,
                run_id=run_id,
                verdict=verdict,
                baseline_id="",
                code_sha=code_sha,
                timestamp=timestamp,
                runner_isolation_mode=runner_isolation_mode,
            )
            records.append(record)
    finally:
        if worktree_root is not None:
            import shutil

            shutil.rmtree(worktree_root, ignore_errors=True)

    return records


if __name__ == "__main__":  # pragma: no cover
    recs = run(ops=["ping"], n=5)
    for r in recs:
        print(r.to_json())
    sys.exit(0)
