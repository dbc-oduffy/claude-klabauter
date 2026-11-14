"""Cost of a landing's worktree sync: `commit_paths` on a 20-path fixture with 5 peer-dirty paths.

Reports process ms (`time.process_time`, never wall clock) and git spawn count (Popen constructions)
for the `commit_paths` call and for `worktree_sync.sync_landed_paths` alone over the same plan. The
base run (sync not yet wired into `commit_paths`) is the comparison the wired run is held to:
zero added spawns, process time within BAR_MS.

Run: `python -m coordinator_core.benchmarks.landing_worktree_sync_cost [--runs N] [--enforce-bar]`.
Scratch lives in a managed temp dir this script removes.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Tuple

from coordinator_core.benchmarks import declare_benchmark_origin
from coordinator_core.win_portability import no_console_creationflags

BAR_MS = 500.0
DEFAULT_RUNS = 5
PATHS = 20
PEER_DIRTY = 5
_MSG = "bench landing\n\nSession-Id: 11111111-1111-4111-8111-111111111111"


def _git(repo: Path, *args: str, data: bytes | None = None) -> str:
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(repo.parent))
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, input=data,
        env=env, **no_console_creationflags(),
    ).stdout.decode().strip()


def _body(i: int, tag: str = "") -> bytes:
    return "".join(f"p{i} line {n}{tag if n == 2 else ''}\n" for n in range(1, 41)).encode()


def _build(scratch: Path) -> Tuple[Path, Dict[str, str]]:
    """A repo with 20 committed paths, 5 of them peer-dirty in the worktree (another line edited)
    and a landed variant blob (line 2 edited) written for every path."""
    repo = scratch / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "b@b.example")
    _git(repo, "config", "user.name", "b")
    _git(repo, "config", "core.autocrlf", "false")
    for i in range(PATHS):
        (repo / f"f{i}.py").write_bytes(_body(i))
    _git(repo, "add", "--", ".")
    _git(repo, "commit", "-q", "-m", "seed")
    blobs: Dict[str, str] = {}
    for i in range(PATHS):
        blobs[f"f{i}.py"] = _git(repo, "hash-object", "-w", "--stdin", data=_body(i, " LANDED"))
    for i in range(PEER_DIRTY):
        (repo / f"f{i}.py").write_bytes(_body(i).replace(b"line 30\n", b"PEER 30\n"))
    return repo, blobs


class _SpawnCount:
    def __init__(self) -> None:
        self.n = 0
        self._real = subprocess.Popen.__init__

    def __enter__(self) -> "_SpawnCount":
        counter = self

        def counting(self_, *a, **k):
            counter.n += 1
            counter._real(self_, *a, **k)

        subprocess.Popen.__init__ = counting  # type: ignore[method-assign]
        return self

    def __exit__(self, *exc) -> None:
        subprocess.Popen.__init__ = self._real  # type: ignore[method-assign]


def measure_once() -> dict:
    """One fixture build and one `commit_paths` call plus one standalone sync; the pin test's entry."""
    from coordinator_core.git.commit import commit_paths

    scratch = Path(tempfile.mkdtemp(prefix="landing-sync-bench-"))
    try:
        repo, blobs = _build(scratch)
        head = _git(repo, "rev-parse", "HEAD")
        sync_ms = None
        sync_spawns = None
        with _SpawnCount() as spawns:
            t0 = time.process_time()
            outcome = commit_paths(
                repo, sorted(blobs), _MSG, supplied_blobs=blobs
            )
            commit_ms = (time.process_time() - t0) * 1000.0
        commit_spawns = spawns.n
        try:
            from coordinator_core.git.worktree_sync import sync_landed_paths
        except ImportError:
            sync_landed_paths = None
        if sync_landed_paths is not None:
            plan = {}
            for p, sha in blobs.items():
                parent = _git(repo, "ls-tree", head, "--", p).split()
                plan[p] = ((int(parent[0], 8), parent[2]), (0o100644, sha))
            with _SpawnCount() as spawns:
                t0 = time.process_time()
                sync_landed_paths(repo, plan)
                sync_ms = (time.process_time() - t0) * 1000.0
            sync_spawns = spawns.n
        return {
            "paths": PATHS, "peer_dirty": PEER_DIRTY,
            "commit_paths_process_ms": round(commit_ms, 1), "commit_paths_git_spawns": commit_spawns,
            "converged": len(getattr(outcome, "converged", ())),
            "left_alone": len(getattr(outcome, "left_alone", ())),
            "sync_alone_process_ms": None if sync_ms is None else round(sync_ms, 1),
            "sync_alone_git_spawns": sync_spawns,
        }
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    ap.add_argument("--enforce-bar", action="store_true")
    args = ap.parse_args(argv)
    declare_benchmark_origin()
    rows = [measure_once() for _ in range(args.runs)]
    report = dict(rows[0])
    report["commit_paths_process_ms"] = round(
        statistics.median(r["commit_paths_process_ms"] for r in rows), 1
    )
    report["runs"] = args.runs
    report["bar_ms"] = BAR_MS
    print(json.dumps(report, indent=2))
    if args.enforce_bar and report["commit_paths_process_ms"] > BAR_MS:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
