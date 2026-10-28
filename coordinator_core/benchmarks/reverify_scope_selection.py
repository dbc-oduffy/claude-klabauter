"""Driver measuring `reverify_scope.select_tests` at the two run shapes the reverify leg sees.

Builds a tmp git repo (a seed commit, then a one-test-file commit and a one-source-file commit),
runs `select_tests` over each commit's `base..head`, and records process time (this process's
`time.process_time()` plus descendants via `commit_leg_profile._ChildMeter`) and the spawn count
(`subprocess.Popen` constructions). Wall clock is never recorded.

    python -m coordinator_core.benchmarks.reverify_scope_selection <dir>

prints one JSON report and exits non-zero when a selection is wrong or a shape exceeds BUDGET_MS.
Base figure for the bug row: the agent-chosen broad reverify leg ran 2693s and no engine selection
existed. The harness shape follows commit_v2_landing_leg_shape.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from coordinator_core.benchmarks import declare_benchmark_origin
from coordinator_core.benchmarks.commit_leg_profile import _ChildMeter
from coordinator_core.git.run import run_git

# The brightline's one bar for an engine op; never a target to approach.
BUDGET_MS = 500.0
BASE_AGENT_CHOSEN_LEG_S = 2693
UNRELATED_TESTS = 40
TEST_FILE = "pkg/tests/test_uses_mod.py"
SOURCE_FILE = "pkg/mod.py"


@dataclass
class ShapeReport:
    """One selection: its result, gated process time (own plus descendants) and spawn count."""

    shape: str
    tests: list
    fully_mapped: bool
    process_ms: float
    spawns: int


def _git(repo: Path, *args: str) -> str:
    result = run_git(list(args), cwd=str(repo))
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed rc={result.returncode}: {result.stderr.strip()}")
    return result.stdout.strip()


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def _commit(repo: Path, msg: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


def build_fixture(parent: Path) -> tuple:
    """A fresh repo under `parent`; returns (repo, seed_sha, test_run_sha, source_run_sha) where the
    test run changes only TEST_FILE and the source run changes only SOURCE_FILE."""
    repo = parent / ("rss-" + uuid.uuid4().hex[:10])
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    _write(repo, "pyproject.toml", '[tool.pytest.ini_options]\ntestpaths = ["pkg"]\n')
    _write(repo, "pkg/__init__.py", "")
    _write(repo, SOURCE_FILE, "VALUE = 1\n")
    _write(repo, TEST_FILE, "from pkg import mod\n\n\ndef test_a():\n    assert mod.VALUE == 1\n")
    for i in range(UNRELATED_TESTS):
        _write(repo, f"pkg/other{i}.py", f"X = {i}\n")
        _write(repo, f"pkg/tests/test_other{i}.py", f"from pkg import other{i}\n\n\ndef test_x():\n    assert other{i}.X == {i}\n")
    seed = _commit(repo, "seed")
    _write(repo, TEST_FILE, "from pkg import mod\n\n\ndef test_a():\n    assert mod.VALUE == 1  # edited\n")
    test_run = _commit(repo, "edit test")
    _write(repo, SOURCE_FILE, "VALUE = 2\n")
    source_run = _commit(repo, "edit source")
    return repo, seed, test_run, source_run


def measure(repo: Path, shape: str, base: str, head: str) -> ShapeReport:
    """Run `select_tests(repo, base, head)` inside one own-CPU plus descendants bracket."""
    from coordinator_core.ops.reverify_scope import select_tests

    real_init = subprocess.Popen.__init__
    spawned = [0]

    def counting_init(self, *a, **k):
        spawned[0] += 1
        return real_init(self, *a, **k)

    subprocess.Popen.__init__ = counting_init
    meter = _ChildMeter()
    meter.start()
    cpu0 = time.process_time()
    try:
        result = select_tests(repo, base, head)
    finally:
        ms = (time.process_time() - cpu0) * 1000.0 + meter.stop_ms()
        subprocess.Popen.__init__ = real_init
    return ShapeReport(shape, result["tests"], result["fully_mapped"], round(ms, 3), spawned[0])


def main(argv: list) -> int:
    declare_benchmark_origin()
    if len(argv) != 1:
        print("usage: reverify_scope_selection <dir>", file=sys.stderr)
        return 2
    repo, seed, test_run, source_run = build_fixture(Path(argv[0]))
    reports = [
        measure(repo, "one_test_file", seed, test_run),
        measure(repo, "one_source_file", test_run, source_run),
    ]
    expected = {"one_test_file": [TEST_FILE], "one_source_file": [TEST_FILE]}
    ok = all(r.tests == expected[r.shape] and r.process_ms < BUDGET_MS for r in reports)
    print(json.dumps({
        "base": {"agent_chosen_leg_s": BASE_AGENT_CHOSEN_LEG_S, "engine_selection": None},
        "end": [
            {"shape": r.shape, "tests": r.tests, "fully_mapped": r.fully_mapped,
             "process_ms": r.process_ms, "spawns": r.spawns}
            for r in reports
        ],
        "budget_ms": BUDGET_MS,
        "ok": ok,
    }))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
