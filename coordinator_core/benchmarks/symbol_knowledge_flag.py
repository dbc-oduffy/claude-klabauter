"""Benchmark for ``symbol_knowledge.contract.flag_symbols``: process time and git spawns.

    python -m coordinator_core.benchmarks.symbol_knowledge_flag [--repo <root>] [--sha <sha>] [--runs N]

Reports the median ``time.process_time`` over N runs and the count of ``subprocess.Popen``
constructions during them. The bar: <= 50 ms process time, 0 spawns. Exit 1 when either is
exceeded; exit 2 when no index or HEAD is found.
"""

from __future__ import annotations

import argparse
import statistics
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

from coordinator_core.benchmarks import declare_benchmark_origin

BAR_MS = 50.0


def measure(common_dir: Path, sha: str, graph_db: Path, runs: int) -> "tuple[float, int, int]":
    """``(median process_ms, spawn count, flagged count)`` over *runs* calls of ``flag_symbols``."""
    from coordinator_core.symbol_knowledge import contract

    spawns = 0
    real_popen = subprocess.Popen

    def counting_popen(*args, **kwargs):
        nonlocal spawns
        spawns += 1
        return real_popen(*args, **kwargs)

    samples: "list[float]" = []
    flagged = 0
    with mock.patch.object(subprocess, "Popen", counting_popen):
        for _ in range(runs):
            start = time.process_time()
            flagged = len(contract.flag_symbols(common_dir, sha, graph_db))
            samples.append((time.process_time() - start) * 1000.0)
    return statistics.median(samples), spawns, flagged


def main(argv: "list[str]") -> int:
    declare_benchmark_origin()
    from coordinator_core.git.git_state import head_sha
    from coordinator_core.symbol_knowledge.leg import index_graph_db

    parser = argparse.ArgumentParser(prog="symbol_knowledge_flag")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--sha", default=None)
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args(argv)
    root = Path(args.repo).resolve()
    sha = args.sha or head_sha(root)
    graph_db = index_graph_db(root)
    if not sha or graph_db is None:
        print("no HEAD or no symbol index for this repo")
        return 2
    ms, spawns, flagged = measure(root / ".git", sha, graph_db, max(1, args.runs))
    print(f"flag_symbols: process_ms={ms:.1f} git_spawns={spawns} flagged={flagged} bar_ms={BAR_MS:.0f}")
    return 0 if ms <= BAR_MS and spawns == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
