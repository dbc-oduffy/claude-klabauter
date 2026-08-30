"""Measure each of the four gates `run_commit_pipeline` ran before landing a
commit -- `deletion_block_gate`, `dirty_tree_gate`, `carry_gate`,
`op_scope_coverage_gate` -- in-process, against a ceremony-sized path set.

THE QUESTION THIS ANSWERS, and it is the one the P1 asks rather than a
general benchmark: `run_commit_pipeline` was killed at the 500ms brightline
and C3 repointed every caller onto `commit_paths`/`ceremony.commit_v2`, which
runs none of these. Reinstating them inside `commit_v2` would put their cost
on the committer every session and the dispatchable `git-commit-agent` route
through, so the choice needs a per-gate number first. The P1 is the
`the-commit-v2-route-runs-none-of-the-fou` row of this repo's bug backlog.

MEASURED AGAINST THE LIVE REPO, NOT A SYNTHETIC FIXTURE, and that is the
whole methodology. Only `deletion_block_gate` scales with the path set it is
handed; the other three scale with the REPO -- `dirty_tree_gate` runs
`git status --porcelain` over the whole worktree, `carry_gate` reads staged
`state/handoffs` records, `op_scope_coverage_gate` parses the registry map.
A 40-file temp repo of the shape `probe_commit_pipeline.py` builds would
report their cost at a tree size no ceremony commit ever runs against, and
would understate the three that matter most. All four are READ-ONLY
classifiers, so measuring them here mutates nothing.

CEREMONY-SIZED IS MEASURED, NOT GUESSED: over this repo's last 300 commits
the path set is median 1, p75 2, p90 6, p99 35. The sweep below spans that.

Job-object accounting via `LiveTreeAccountant`, bracketing N calls in ONE
window -- see that class's QUANTISATION note for why a per-call snapshot pair
reports a tick count rather than a cost.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

SRC = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SRC))

from coordinator_core.benchmarks import declare_benchmark_origin
from coordinator_core.benchmarks.process_time import LiveTreeAccountant
from coordinator_core.ops.ceremony.commit_gates import (
    carry_gate,
    deletion_block_gate,
    dirty_tree_gate,
    op_scope_coverage_gate,
)

WARMUP = 3

#: Drawn from the last-300-commit distribution named in the module docstring.
SIZES = (1, 6, 35)


def _tracked_sample(root: Path, n: int) -> list:
    """`n` tracked, undeleted paths -- a stand-in for a commit's path set.

    Tracked-and-present matters: a path staged for deletion changes which leg
    of `deletion_block_gate` runs, and this probe measures the ordinary case.
    """
    out = subprocess.run(
        ["git", "ls-files", "--", "coordinator_core"],
        cwd=root, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.split("\n")
    live = [p for p in out if p.strip() and (root / p).is_file()]
    return live[:n]


def _dirty_count(root: Path) -> int:
    out = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout
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
    for size in SIZES:
        paths = _tracked_sample(root, size)
        cases = (
            ("deletion_block_gate",
             lambda: deletion_block_gate(msg, paths, cwd=root)),
            ("dirty_tree_gate",
             lambda: dirty_tree_gate(root, paths)),
            ("carry_gate",
             lambda: carry_gate(root, paths)),
            ("op_scope_coverage_gate",
             lambda: op_scope_coverage_gate(root, paths)),
        )
        for label, fn in cases:
            ms, procs, wall = _window(fn, n)
            print(f"{label:28s} {size:5d}  {ms:8.2f} {procs:6.2f}  {wall:8.2f}")

        def all_four():
            deletion_block_gate(msg, paths, cwd=root)
            dirty_tree_gate(root, paths)
            carry_gate(root, paths)
            op_scope_coverage_gate(root, paths)

        ms, procs, wall = _window(all_four, n)
        print(f"{'ALL FOUR (reinstate shape)':28s} {size:5d}  {ms:8.2f} {procs:6.2f}  {wall:8.2f}")
        print()


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 12)
