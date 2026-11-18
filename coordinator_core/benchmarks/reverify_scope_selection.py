"""Driver measuring `reverify_scope.select_tests` at the two run shapes the reverify leg sees.

Builds a tmp git repo (a seed commit, then a one-test-file commit and a one-source-file commit),
runs `select_tests` over each commit's `base..head`, and records process time (this process's
`time.process_time()` plus descendants via `commit_leg_profile._ChildMeter`) and the spawn count
(`subprocess.Popen` constructions). Wall clock is never recorded.

    python -m coordinator_core.benchmarks.reverify_scope_selection <dir>
    python -m coordinator_core.benchmarks.reverify_scope_selection <dir> --live-shape <paths...>

The first form prints one JSON report and exits non-zero when a selection is wrong or a shape exceeds BUDGET_MS.
`--live-shape` measures the `review.reverify` list-mode pool for edited paths against this repo at HEAD,
writes `<dir>/live-shape.json`, and exits non-zero when list-mode process time exceeds BUDGET_MS.
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


def _bracket(fn) -> tuple:
    """Run `fn()` inside one own-CPU plus descendants bracket; returns (result, process_ms, spawns)."""
    real_init = subprocess.Popen.__init__
    spawned = [0]

    def counting_init(self, *a, **k) -> None:
        spawned[0] += 1
        return real_init(self, *a, **k)

    subprocess.Popen.__init__ = counting_init
    meter = _ChildMeter()
    meter.start()
    cpu0 = time.process_time()
    try:
        result = fn()
    finally:
        ms = (time.process_time() - cpu0) * 1000.0 + meter.stop_ms()
        subprocess.Popen.__init__ = real_init
    return result, round(ms, 3), spawned[0]


def measure_live_shape(repo: Path, paths: list) -> dict:
    """Selection pool for edited `paths` against `repo` at HEAD, timed on the `review.reverify`
    list-mode path: `select_for_paths` over one `_tracked_test_bytes` pass.

    The headline `process_ms`/`spawns` are that list-mode path and `ok` is `process_ms <= BUDGET_MS`.
    The git-based `select_tests` path (diff leg pinned to `paths`) is timed separately as
    `select_tests_process_ms`/`select_tests_spawns`. Owned tests come first; past `TRIPWIRE_BOUND`
    the tripwire union is cut and the rest counted as not selected. Process time is own CPU plus
    descendants; spawns count `subprocess.Popen` constructions. Every path is an edited path:
    the created-path tree walkers (`namers`) are not measured.
    """
    from coordinator_core.ops import reverify_scope
    from coordinator_core.ops.review_tripwires import _tracked_test_bytes

    changed = sorted(set(paths))
    sel, ms, spawns = _bracket(
        lambda: reverify_scope.select_for_paths(repo, changed, _tracked_test_bytes(repo, set(changed)))
    )
    real_changed = reverify_scope._changed_paths
    setattr(reverify_scope, "_changed_paths", lambda _root, _base, _head: changed)
    try:
        _, st_ms, st_spawns = _bracket(lambda: reverify_scope.select_tests(repo, "HEAD", "HEAD"))
    finally:
        setattr(reverify_scope, "_changed_paths", real_changed)
    owned = list(sel["owned"])
    owned_set = set(owned)
    rest = [p for p in sel["tripwires"] if p not in owned_set]
    pool = len(owned) + len(rest) + len([p for p in sel["tripwires_not_selected"] if p not in owned_set])
    bound = reverify_scope.TRIPWIRE_BOUND
    chosen = rest[:bound]
    selected = owned + chosen
    return {
        "mode": "live-shape",
        "path": "review.reverify list mode (select_for_paths)",
        "paths": changed,
        "selected_files": len(selected),
        "union_files": pool,
        "owned_files": len(owned),
        "tripwire_files": len(chosen),
        "not_selected": pool - len(selected),
        "tripwire_bound": bound,
        "owned": owned,
        "selected": selected,
        "process_ms": ms,
        "spawns": spawns,
        "budget_ms": BUDGET_MS,
        "ok": ms <= BUDGET_MS,
        "select_tests_process_ms": st_ms,
        "select_tests_spawns": st_spawns,
    }


def _live_shape_main(out_dir: Path, paths: list) -> int:
    report = measure_live_shape(Path(__file__).resolve().parents[2], paths)
    out_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(report)
    (out_dir / "live-shape.json").write_text(text, encoding="utf-8", newline="")
    print(text)
    return 0 if report["ok"] else 1


def main(argv: list) -> int:
    declare_benchmark_origin()
    if len(argv) >= 3 and argv[1] == "--live-shape":
        return _live_shape_main(Path(argv[0]), argv[2:])
    if len(argv) != 1:
        print("usage: reverify_scope_selection <dir> [--live-shape <paths...>]", file=sys.stderr)
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
