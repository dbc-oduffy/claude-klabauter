"""Per-test process time and git spawn count for the coordinator/bin git-clone test files.

Runs pytest in-process over the target files. Each test gets its own GIT_TRACE2_EVENT file, so
every git process is counted wherever it was spawned (door subprocesses and percolate/publish
children included). Process time is this process's CPU time plus child CPU sampled through
psutil (os.times children is zero on Windows); a child that exits between samples is missed, so
process time is a lower bound and the spawn count is the exact figure. Output goes to stdout only.

    python coordinator/bin/tests/git_spawn_meter.py [--files F ...] [--no-companion]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from collections import defaultdict
from pathlib import Path

try:
    import psutil
except ImportError:
    print("git_spawn_meter: psutil is required (child CPU is unmeasurable without it)", file=sys.stderr)
    sys.exit(2)

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_REPO = _TESTS_DIR.parents[2]

DEFAULT_FILES = (
    "test_commit_clone.py",
    "test_percolate_mirror_push_publish_slot.py",
    "test_commit_clone_failure_refuses_door.py",
    "test_publish_refuses_dirty_publisher_code.py",
    "test_percolate_round_publisher_chain_from_pin.py",
)
COMPANION = "test_percolate_round_named_source_commit.py"
PER_TEST_BUDGET_MS = 200.0
PER_FILE_MEAN_BUDGET_MS = 500.0
_REMOTE_SUBCOMMANDS = {"ls-remote", "fetch", "push", "clone", "pull"}
_REMOTE_HELPERS = ("git-remote-", "git-upload-pack", "git-receive-pack", "git-fetch-pack", "git-send-pack")
_SAMPLE_INTERVAL_S = 0.004
_GIT_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}


def _subcommand(argv: list[str]) -> str:
    """First non-option argument of a git argv, skipping argv[0] and option values."""
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg in _GIT_OPTS_WITH_VALUE:
            i += 2
            continue
        if arg.startswith("-"):
            i += 1
            continue
        return arg
    return ""


def _parse_trace(path: Path) -> tuple[int, dict[str, int]]:
    """(git process count, tag counts) from a trace2 event file."""
    spawns = 0
    tags: dict[str, int] = defaultdict(int)
    if not path.is_file():
        return 0, tags
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("event") != "start":
            continue
        spawns += 1
        argv = event.get("argv") or []
        if not argv:
            continue
        name = Path(str(argv[0])).name.lower()
        if name.endswith(".exe"):
            name = name[:-4]
        sub = _subcommand(argv)
        if sub in _REMOTE_SUBCOMMANDS:
            tags[sub] += 1
            tags["remote"] += 1
        elif name.startswith(_REMOTE_HELPERS):
            tags["remote-helper"] += 1
            tags["remote"] += 1
    return spawns, tags


class _ChildCpuSampler(threading.Thread):
    """Polls this process's descendants, keeping the peak cumulative CPU seen per pid."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self._stop_evt = threading.Event()
        self._peak: dict[tuple[int, float], float] = {}
        self._me = psutil.Process()

    def run(self) -> None:
        while not self._stop_evt.is_set():
            self._sample()
            self._stop_evt.wait(_SAMPLE_INTERVAL_S)
        self._sample()

    def _sample(self) -> None:
        try:
            kids = self._me.children(recursive=True)
        except psutil.Error:
            return
        for kid in kids:
            try:
                times = kid.cpu_times()
                key = (kid.pid, kid.create_time())
            except psutil.Error:
                continue
            cpu = times.user + times.system
            if cpu > self._peak.get(key, 0.0):
                self._peak[key] = cpu

    def stop(self) -> float:
        self._stop_evt.set()
        self.join()
        return sum(self._peak.values())


class MeterPlugin:
    def __init__(self, trace_dir: Path) -> None:
        self.trace_dir = trace_dir
        self.rows: dict[str, dict] = {}
        self.outcomes: dict[str, str] = {}
        self._seq = 0

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_protocol(self, item, nextitem):
        self._seq += 1
        trace = self.trace_dir / f"{self._seq}.jsonl"
        prior = os.environ.get("GIT_TRACE2_EVENT")
        os.environ["GIT_TRACE2_EVENT"] = str(trace)
        sampler = _ChildCpuSampler()
        sampler.start()
        wall0, cpu0 = time.perf_counter(), time.process_time()
        try:
            yield
        finally:
            cpu = time.process_time() - cpu0
            wall = time.perf_counter() - wall0
            child_cpu = sampler.stop()
            if prior is None:
                os.environ.pop("GIT_TRACE2_EVENT", None)
            else:
                os.environ["GIT_TRACE2_EVENT"] = prior
            spawns, tags = _parse_trace(trace)
            self.rows[item.nodeid] = {
                "ms": (cpu + child_cpu) * 1000.0,
                "wall_ms": wall * 1000.0,
                "spawns": spawns,
                "ls_remote": tags.get("ls-remote", 0),
                "remote": tags.get("remote", 0),
            }

    def pytest_runtest_logreport(self, report) -> None:
        prior = self.outcomes.get(report.nodeid, "passed")
        if report.failed:
            self.outcomes[report.nodeid] = "error" if report.when != "call" else "failed"
        elif report.skipped and prior == "passed":
            self.outcomes[report.nodeid] = "skipped"


def _print_report(rows: dict[str, dict], outcomes: dict[str, str]) -> None:
    by_file: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for nodeid, row in rows.items():
        by_file[nodeid.split("::", 1)[0]].append((nodeid.split("::", 1)[1], row))
    print(f"{'ms':>9} {'wall_ms':>9} {'git':>4} {'lsr':>3} {'rmt':>3}  {'outcome':<8} test")
    for file, tests in by_file.items():
        print(f"\n== {file}")
        for name, row in tests:
            outcome = outcomes.get(f"{file}::{name}", "passed")
            print(
                f"{row['ms']:9.1f} {row['wall_ms']:9.1f} {row['spawns']:4d} "
                f"{row['ls_remote']:3d} {row['remote']:3d}  {outcome:<8} {name.replace(chr(10), ' ')[:90]}"
            )
    print("\n== per-file totals")
    print(f"{'file':<58} {'tests':>5} {'git':>5} {'lsr':>4} {'rmt':>4} {'total_ms':>10} {'mean_ms':>8} {'max_ms':>8} {'>200ms':>6}")
    for file, tests in by_file.items():
        ms = [r["ms"] for _, r in tests]
        over = sum(1 for m in ms if m > PER_TEST_BUDGET_MS)
        mean = sum(ms) / len(ms)
        print(
            f"{file:<58} {len(tests):5d} {sum(r['spawns'] for _, r in tests):5d} "
            f"{sum(r['ls_remote'] for _, r in tests):4d} {sum(r['remote'] for _, r in tests):4d} "
            f"{sum(ms):10.1f} {mean:8.1f} {max(ms):8.1f} {over:6d}"
            + ("  MEAN>500" if mean > PER_FILE_MEAN_BUDGET_MS else "")
        )
    named = ("test_open_only_pushes_while_the_slot_is_still_held", "test_sha_on_no_ref_materialises")
    print("\n== named hermetic tests (ls-remote / remote-transport spawns)")
    for nodeid, row in rows.items():
        if any(n in nodeid for n in named):
            print(f"{nodeid}: ls-remote={row['ls_remote']} remote={row['remote']} git={row['spawns']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--files", nargs="+", default=list(DEFAULT_FILES), help="test file names under coordinator/bin/tests")
    parser.add_argument("--no-companion", action="store_true", help=f"omit {COMPANION}")
    args = parser.parse_args(argv)
    files = list(args.files)
    if not args.no_companion and COMPANION not in files:
        files.append(COMPANION)
    paths = [str(_TESTS_DIR / f) for f in files]

    scratch = _REPO / "scratch"
    scratch.mkdir(exist_ok=True)
    trace_dir = Path(tempfile.mkdtemp(prefix="git-spawn-meter-", dir=str(scratch)))
    plugin = MeterPlugin(trace_dir)
    os.chdir(_REPO)
    try:
        code = pytest.main([*paths, "-q", "-p", "no:cacheprovider", "-o", "addopts="], plugins=[plugin])
        _print_report(plugin.rows, plugin.outcomes)
    finally:
        shutil.rmtree(trace_dir, ignore_errors=True)
    print(f"\npytest exit code: {int(code)}")
    return 0 if plugin.rows and int(code) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
