"""Spike: per-commit in-process decode cost of the landed-trailer record's pruned walk.

Run as ``python -m coordinator_core.benchmarks.probe_landed_trailer_record_cost [--repo PATH]``.
Times ``landed_trailer_record._delta`` over deltas of 1/8/32/64 commits with the object and
commit caches cleared before each trial (a warm pack lookup source, cold decode), prints
per-commit cost and the bound K with ``K * per-commit < BUDGET_MS``. The git spawns here are
setup only and are never inside a timed region.
"""

from __future__ import annotations

import argparse
import statistics
import subprocess
import sys
import time
from pathlib import Path

from coordinator_core.git import commit_walk, git_objects
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.ops.dispatch_emit import landed_trailer_record as rec
from coordinator_core.win_portability import no_console_creationflags

BUDGET_MS = 200.0
DELTAS = (1, 8, 32, 64)
TRIALS = 5


def _rev(repo: Path, spec: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", spec], cwd=str(repo), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _cold() -> None:
    commit_walk._COMMIT_CACHE.clear()
    git_objects._OBJECT_CACHE.clear()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[2]))
    args = parser.parse_args(argv)
    repo = Path(args.repo)
    common = resolve_git_common_dir(repo)
    head = _rev(repo, "HEAD")
    configured = rec.MAX_WALK_READS
    try:
        rec.MAX_WALK_READS = 10_000
        rec._delta(common, head, _rev(repo, "HEAD~1"))

        per_commit = []
        for delta in DELTAS:
            base = _rev(repo, f"HEAD~{delta}")
            samples = []
            reads = 0
            for _ in range(TRIALS):
                _cold()
                t0 = time.perf_counter()
                rec._delta(common, head, base)
                samples.append((time.perf_counter() - t0) * 1000)
                reads = len(commit_walk._COMMIT_CACHE)
            median = statistics.median(samples)
            per = median / reads
            per_commit.append(per)
            print(f"delta={delta:3d}  median={median:8.2f}ms  per-commit~{per:6.3f}ms  (reads~{reads})")
    finally:
        rec.MAX_WALK_READS = configured
    worst = max(per_commit)
    k = int(BUDGET_MS // worst)
    print(f"worst per-commit cost {worst:.3f}ms -> K = {k} reads (K * cost = {k * worst:.1f}ms < {BUDGET_MS:.0f}ms)")
    verdict = "OK" if configured * worst < BUDGET_MS else "OVER"
    print(f"MAX_WALK_READS in module: {configured} -> {configured * worst:.1f}ms worst case, {verdict}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
