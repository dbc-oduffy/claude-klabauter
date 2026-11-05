"""`commit_paths` pin-loop process-time benchmark: 664 CR-bearing `text=auto` paths over an 8000-entry index.

Builds a tmp git repo with `* text=auto`, 8000 committed LF files, then rewrites 664 of them with CRLF
bytes and drives `commit_paths` at default arguments over those 664 paths. Each run resets the repo to
the seed commit first. Reports process ms (`time.process_time`, never wall clock) and spawn count; the
cold run and the median of `--runs` warm runs. Figures are in-process CPU: a spawned child's CPU is
not in them, only its spawn count.

Run: `python coordinator_core/benchmarks/commit_paths_pin_loop_budget.py [--runs N] [--enforce-bar]`.
Scratch lives in a managed temp dir this script removes.
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pytest  # noqa: E402

from coordinator_core.benchmarks import declare_benchmark_origin  # noqa: E402
from coordinator_core.benchmarks.isolated_clone import rmtree_or_raise  # noqa: E402
from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed  # noqa: E402
from coordinator_core.git.commit import commit_paths  # noqa: E402
from coordinator_core.git.run import run_git  # noqa: E402

#: The brightline process-time bar (CLAUDE.md § The brightline).
BAR_MS = 500.0
INDEX_ENTRIES = 8000
CR_PATHS = 664
DEFAULT_RUNS = 5
_FANOUT = 80


def _git(root: Path, *args: str) -> None:
    """Fixture setup only (untimed). Each call is held to `run_git`'s local cap, so callers keep calls small."""
    env = dict(
        os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t.example", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t.example",
    )
    done = run_git(["-C", str(root), *args], env=env)
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} failed (rc={done.returncode}): {done.stderr.strip()}")


def _name(i: int) -> str:
    return f"d{i % _FANOUT}/f{i}.txt"


def build_fixture(root: Path) -> list[str]:
    """Populate `root` (an empty dir): `* text=auto`, `INDEX_ENTRIES` committed LF files. Returns the
    `CR_PATHS` paths the measured call commits."""
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "core.autocrlf", "false")
    (root / ".gitattributes").write_text("* text=auto\n", encoding="ascii", newline="\n")
    for i in range(INDEX_ENTRIES):
        path = root / _name(i)
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(f"line one {i}\nline two {i}\n".encode("ascii"))
    # One `add -A` over every entry exceeds `run_git`'s local cap; stage per fan-out directory.
    _git(root, "add", ".gitattributes")
    for d in range(_FANOUT):
        _git(root, "add", "--", f"d{d}")
    _git(root, "commit", "-q", "-m", "seed (commit_paths pin-loop bench)")
    _git(root, "tag", "bench-seed")
    return [_name(i) for i in range(CR_PATHS)]


def _stage_crlf(root: Path, names: list[str]) -> None:
    for i, name in enumerate(names):
        (root / name).write_bytes(f"line one {i}\r\nline two {i}\r\nline three {i}\r\n".encode("ascii"))


def _reset(root: Path) -> None:
    """Back to the tagged seed commit: the measured commit advances HEAD."""
    _git(root, "reset", "-q", "--hard", "bench-seed")


def _call_once(root: Path, names: list[str]) -> dict:
    """One `commit_paths` over `names` on a reset fixture; returns process ms and spawn count."""
    _reset(root)
    _stage_crlf(root, names)
    spawn_mp = pytest.MonkeyPatch()
    with _count_spawns_attributed(spawn_mp) as spawns:
        t0 = time.process_time()
        outcome = commit_paths(root, names, "bench commit")
        elapsed = (time.process_time() - t0) * 1000.0
    return {"process_ms": elapsed, "spawns": len(spawns), "spawn_argv": [s.argv for s in spawns], "outcome": outcome}


def measure(runs: int = DEFAULT_RUNS) -> dict:
    """Build the fixture; one cold call, then the median of `runs` warm calls."""
    from coordinator_core.temp_layout import managed_tempdir

    scratch = Path(tempfile.mkdtemp(prefix="commit-paths-pin-loop-", dir=str(managed_tempdir())))
    try:
        root = scratch / "repo"
        root.mkdir()
        names = build_fixture(root)
        cold = _call_once(root, names)
        samples = [_call_once(root, names) for _ in range(runs)]
        mid = sorted(samples, key=lambda s: s["process_ms"])[len(samples) // 2]
        return {
            "index_entries": INDEX_ENTRIES,
            "cr_paths": len(names),
            "cold_process_ms": round(cold["process_ms"], 1),
            "warm_process_ms": round(statistics.median(s["process_ms"] for s in samples), 1),
            "spawns": mid["spawns"],
            "spawn_argv": mid["spawn_argv"],
        }
    finally:
        rmtree_or_raise(scratch, label="commit-paths-pin-loop")


def main() -> int:
    declare_benchmark_origin()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    ap.add_argument("--enforce-bar", action="store_true", help=f"exit 2 when cold or warm process ms >= {BAR_MS:.0f}")
    args = ap.parse_args()
    res = measure(args.runs)
    for key, value in res.items():
        if key != "spawn_argv":
            print(f"{key}: {value}")
    for argv in res["spawn_argv"]:
        print(f"spawn: {' '.join(argv)[:160]}")
    over = max(res["warm_process_ms"], res["cold_process_ms"]) >= BAR_MS
    print(f"bar: {'OVER' if over else 'UNDER'} {BAR_MS:.0f}ms process time (cold {res['cold_process_ms']}, warm {res['warm_process_ms']})")
    return 2 if args.enforce_bar and over else 0


if __name__ == "__main__":
    sys.exit(main())
