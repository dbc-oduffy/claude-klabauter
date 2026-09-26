"""Measure each of the three gates `run_commit_pipeline` ran before landing a
commit -- `deletion_block_gate`, `carry_gate`, `op_scope_coverage_gate` --
in-process, against a ceremony-sized path set. (A fourth gate, `dirty_tree_gate`,
was probed here until its brightline-kill-bar deletion -- Review: overengineering-reviewer.)

`attribution_gate` is measured too, but for a different reason: it is NOT
wired into `commit_v2` yet (plan `docs/plans/2026-09-25-reviewer-attribution-commit-gate.md`
C5) -- this is the pre-wiring cost check the C4 wiring row waits on, not a
reinstatement question. It is excluded from the "ALL THREE (reinstate shape)"
bundle below, which is specific to the three `run_commit_pipeline` gates.

THE QUESTION THIS ANSWERS, and it is the one the P1 asks rather than a
general benchmark: `run_commit_pipeline` was killed at the 500ms brightline
and C3 repointed every caller onto `commit_paths`/`ceremony.commit_v2`, which
runs none of these. Reinstating them inside `commit_v2` would put their cost
on the committer every session and the dispatchable `git-commit-agent` route
through, so the choice needs a per-gate number first. The P1 is the
`the-commit-v2-route-runs-none-of-the-fou` row of this repo's bug backlog.

MEASURED AGAINST THE LIVE REPO, NOT A SYNTHETIC FIXTURE, and that is the
whole methodology. Only `deletion_block_gate` scales with the path set it is
handed; the other two scale with the REPO -- `carry_gate` reads staged
`state/handoffs` records, `op_scope_coverage_gate` parses the registry map.
A 40-file temp repo of the shape `probe_commit_pipeline.py` builds would
report their cost at a tree size no ceremony commit ever runs against, and
would understate the two that matter most. All three are READ-ONLY
classifiers, so measuring them here mutates nothing.

CEREMONY-SIZED IS MEASURED, NOT GUESSED: over this repo's last 300 commits
the path set is median 1, p75 2, p90 6, p99 35. The sweep below spans that.

Job-object accounting via `LiveTreeAccountant`, bracketing N calls in ONE
window -- see that class's QUANTISATION note for why a per-call snapshot pair
reports a tick count rather than a cost.
"""
import os
import sys
import time
from pathlib import Path

SRC = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SRC))

from coordinator_core.attribution import is_exempt_path
from coordinator_core.benchmarks import declare_benchmark_origin
from coordinator_core.benchmarks.process_time import LiveTreeAccountant
from coordinator_core.git.run import run_git
from coordinator_core.ops.ceremony.commit_gates import (
    attribution_gate,
    carry_gate,
    deletion_block_gate,
    op_scope_coverage_gate,
)

WARMUP = 3

SIZES = (1, 2, 6, 35)
"""Ceremony-measured percentiles this repo's last 300 commits show: median 1,
p75 2, p90 6, p99 35 (see module docstring). `attribution_gate`'s C5 row
names this same sweep explicitly; the other three gates were already
measured only at (1, 6, 35) and gain the p75 point too rather than diverge
onto a second sweep."""

PROCESS_TIME_TARGET_MS = 50.0
"""Same bar `ceremony.commit_v2` measures itself against --
`coordinator_core/benchmarks/tests/test_commit_v2_process_time_gate.py:138`,
not `commit_v2.py`'s docstring mention of it (Review: coordinator:staff-eng
finding 3, coordinator:eng-director finding 2)."""


def _tracked_sample(root: Path, n: int) -> list:
    out = run_git(["ls-files", "--", "coordinator_core"], cwd=str(root)).stdout.split("\n")
    live = [p for p in out if p.strip() and (root / p).is_file()]
    return live[:n]


def _tracked_non_exempt_python_sample(root: Path, n: int) -> list:
    """Real `.py` paths from HEAD that `is_exempt_path` would NOT skip --
    `attribution_gate`'s own filter runs first and reads nothing for an
    exempt path, so a sample built from exempt paths (e.g. anything under
    `coordinator_core/attribution/` itself) would measure the empty-fast-path
    cost, not the real one the C5 row asks for."""
    out = run_git(["ls-files", "--", "coordinator_core"], cwd=str(root)).stdout.split("\n")
    live = [
        p for p in out
        if p.strip() and p.endswith(".py") and (root / p).is_file() and not is_exempt_path(p)
    ]
    return live[:n]


def _dirty_count(root: Path) -> int:
    out = run_git(["status", "--porcelain"], cwd=str(root)).stdout
    return len([l for l in out.split("\n") if l.strip()])


def _window(fn, n: int):
    """Bracket `n` calls of `fn` in one job-accounting window.

    Returns (ms_per_call, procs_per_call, wall_ms_per_call). Wall clock is
    reported ALONGSIDE process time and never instead of it -- on this box it
    measures peer load (CLAUDE.md's Load norm), so it is context for the
    process figure, not the figure itself.
    """
    for _ in range(WARMUP):
        fn()
    acc = LiveTreeAccountant(os.getpid())
    before = acc.snapshot()
    t0 = time.perf_counter()
    for _ in range(n):
        fn()
    wall = (time.perf_counter() - t0) * 1000.0
    after = acc.snapshot()
    acc.close()
    return (
        (after["process_time_ms"] - before["process_time_ms"]) / n,
        (after["procs"] - before["procs"]) / n,
        wall / n,
    )


def main(n=12):
    declare_benchmark_origin()
    root = SRC
    dirty = _dirty_count(root)
    print(f"repo={root}  dirty_paths={dirty}  n={n}/window  warmup={WARMUP}")
    print(f"{'gate':28s} {'paths':>5s}  {'proc_ms':>8s} {'procs':>6s}  {'wall_ms':>8s}")

    msg = "probe: measuring gate cost\n\nbody\n"
    attribution_results = {}
    for size in SIZES:
        paths = _tracked_sample(root, size)
        attribution_paths = _tracked_non_exempt_python_sample(root, size)
        cases = (
            ("deletion_block_gate",
             lambda: deletion_block_gate(msg, paths, cwd=root)),
            ("carry_gate",
             lambda: carry_gate(root, paths)),
            ("op_scope_coverage_gate",
             lambda: op_scope_coverage_gate(root, paths)),
            ("attribution_gate",
             lambda: attribution_gate(root, attribution_paths)),
        )
        for label, fn in cases:
            ms, procs, wall = _window(fn, n)
            print(f"{label:28s} {size:5d}  {ms:8.2f} {procs:6.2f}  {wall:8.2f}")
            if label == "attribution_gate":
                attribution_results[size] = (ms, procs, wall, len(attribution_paths))

        def all_three():
            deletion_block_gate(msg, paths, cwd=root)
            carry_gate(root, paths)
            op_scope_coverage_gate(root, paths)

        ms, procs, wall = _window(all_three, n)
        print(f"{'ALL THREE (reinstate shape)':28s} {size:5d}  {ms:8.2f} {procs:6.2f}  {wall:8.2f}")
        print()

    within_budget = all(
        procs == 0 and ms <= PROCESS_TIME_TARGET_MS
        for ms, procs, _wall, _paths in attribution_results.values()
    )
    print(
        f"attribution_gate summary: within_{PROCESS_TIME_TARGET_MS}ms_and_0_spawns="
        f"{within_budget}"
    )
    return attribution_results, within_budget


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 12)
