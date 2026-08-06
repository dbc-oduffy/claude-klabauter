"""
coordinator_core.testing.test_run — scoped tests for the subprocess
executor + ThreadPool aggregation engine (`run.py`, C2).

Purpose: exercises AC3 (per-suite invocation shape), AC4 (aggregate
overall_ok), AC6 (parallel dispatch + worker cap), and the DEC-10
fail-loud / Finding-5 timeout discipline, against hermetic `tmp_path`
fixture suites (never committed dummies — Finding 9).

Port source: none — net-new (DR-059 harness authoring).
Spec backlink: docs/plans/2026-07-19-claude-klabauter-doe-full-test-runner.md § C2

Negative-spec:
    - Does NOT write any dummy `test_*.py` / `.test.py` fixture file anywhere
      under `coordinator_core/` — every fixture suite used here is
      materialized under pytest's `tmp_path` at runtime only (Finding 9). A
      committed failing dummy would permanently red claude-klabauter's own suite
      (`testpaths=["coordinator_core"]`, `python_files=["test_*.py"]`).
    - Does NOT assert on wall-clock speedup from parallelism (flaky) — the
      worker-cap test instead monkeypatches `os.cpu_count()` and asserts on
      the *computed* cap, not on timing.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from coordinator_core.testing.collect import Suite
from coordinator_core.testing.run import (
    _resolve_worker_count,
    overall_ok,
    run_suites,
)


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _suite(path: Path, runner_kind: str, family: str) -> Suite:
    return Suite(family=family, path=path, runner_kind=runner_kind)


# ---------------------------------------------------------------------------
# AC3 — per-suite invocation shape (py-nonnative, py-native batching)
# ---------------------------------------------------------------------------


def test_py_nonnative_suite_passing(tmp_path: Path) -> None:
    passing = _write(tmp_path / "pass.test.py", "import sys\nsys.exit(0)\n")
    suites = [_suite(passing, "python3", "py-nonnative")]
    results = run_suites(suites, repo_root=tmp_path)
    assert len(results) == 1
    assert results[0].exit_code == 0
    assert results[0].suite is suites[0]


def test_py_nonnative_suite_failing(tmp_path: Path) -> None:
    failing = _write(tmp_path / "fail.test.py", "import sys\nsys.exit(1)\n")
    suites = [_suite(failing, "python3", "py-nonnative")]
    results = run_suites(suites, repo_root=tmp_path)
    assert len(results) == 1
    assert results[0].exit_code == 1


def test_py_native_batch_single_invocation_passing(tmp_path: Path) -> None:
    p1 = _write(
        tmp_path / "test_one.py", "def test_one():\n    assert True\n"
    )
    p2 = _write(
        tmp_path / "test_two.py", "def test_two():\n    assert True\n"
    )
    suites = [
        _suite(p1, "pytest", "py-native"),
        _suite(p2, "pytest", "py-native"),
    ]
    results = run_suites(suites, repo_root=tmp_path)
    # DEC-5: all py-native suites collapse into exactly ONE dispatched result.
    assert len(results) == 1
    assert results[0].suite is None
    assert results[0].exit_code == 0


def test_py_native_batch_single_invocation_failing(tmp_path: Path) -> None:
    p1 = _write(
        tmp_path / "test_one.py", "def test_one():\n    assert True\n"
    )
    p2 = _write(
        tmp_path / "test_two.py", "def test_two():\n    assert False\n"
    )
    suites = [
        _suite(p1, "pytest", "py-native"),
        _suite(p2, "pytest", "py-native"),
    ]
    results = run_suites(suites, repo_root=tmp_path)
    assert len(results) == 1
    assert results[0].exit_code != 0


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_node_suite_passing(tmp_path: Path) -> None:
    passing = _write(
        tmp_path / "example.test.js",
        "const { test } = require('node:test');\n"
        "test('ok', () => {});\n",
    )
    suites = [_suite(passing, "node", "js-suffix")]
    results = run_suites(suites, repo_root=tmp_path)
    assert len(results) == 1
    assert results[0].exit_code == 0


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_node_suite_failing(tmp_path: Path) -> None:
    failing = _write(
        tmp_path / "test-example.js",
        "const assert = require('node:assert');\n"
        "const { test } = require('node:test');\n"
        "test('bad', () => { assert.ok(false); });\n",
    )
    suites = [_suite(failing, "node", "js-prefix")]
    results = run_suites(suites, repo_root=tmp_path)
    assert len(results) == 1
    assert results[0].exit_code != 0


# ---------------------------------------------------------------------------
# AC4 — aggregate overall_ok
# ---------------------------------------------------------------------------


def test_overall_ok_true_when_all_pass(tmp_path: Path) -> None:
    passing = _write(tmp_path / "pass.test.py", "import sys\nsys.exit(0)\n")
    suites = [_suite(passing, "python3", "py-nonnative")]
    results = run_suites(suites, repo_root=tmp_path)
    assert overall_ok(results) is True


def test_overall_ok_false_when_any_fails(tmp_path: Path) -> None:
    passing = _write(tmp_path / "pass.test.py", "import sys\nsys.exit(0)\n")
    failing = _write(tmp_path / "fail.test.py", "import sys\nsys.exit(1)\n")
    suites = [
        _suite(passing, "python3", "py-nonnative"),
        _suite(failing, "python3", "py-nonnative"),
    ]
    results = run_suites(suites, repo_root=tmp_path)
    assert overall_ok(results) is False


def test_overall_ok_empty_results_is_true() -> None:
    assert overall_ok([]) is True


# ---------------------------------------------------------------------------
# AC6 — worker cap honored (parallel dispatch)
# ---------------------------------------------------------------------------


def test_worker_cap_default_uses_half_cpu_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.cpu_count", lambda: 8)
    assert _resolve_worker_count(unit_count=10, jobs=None) == 4


def test_worker_cap_floors_at_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.cpu_count", lambda: 1)
    assert _resolve_worker_count(unit_count=10, jobs=None) == 1


def test_worker_cap_bounded_by_unit_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.cpu_count", lambda: 32)
    # cap would be 16, but only 2 units exist -> min(2, 16) == 2
    assert _resolve_worker_count(unit_count=2, jobs=None) == 2


def test_worker_cap_jobs_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.cpu_count", lambda: 32)
    assert _resolve_worker_count(unit_count=10, jobs=3) == 3


def test_jobs_le_1_short_circuits_to_serial(tmp_path: Path) -> None:
    p1 = _write(tmp_path / "one.test.py", "import sys\nsys.exit(0)\n")
    p2 = _write(tmp_path / "two.test.py", "import sys\nsys.exit(0)\n")
    suites = [
        _suite(p1, "python3", "py-nonnative"),
        _suite(p2, "python3", "py-nonnative"),
    ]
    results = run_suites(suites, repo_root=tmp_path, jobs=1)
    assert len(results) == 2
    assert overall_ok(results) is True


def test_parallel_dispatch_runs_all_suites(tmp_path: Path) -> None:
    suites = [
        _suite(
            _write(tmp_path / f"s{i}.test.py", "import sys\nsys.exit(0)\n"),
            "python3",
            "py-nonnative",
        )
        for i in range(6)
    ]
    results = run_suites(suites, repo_root=tmp_path, jobs=3)
    assert len(results) == 6
    assert overall_ok(results) is True


# ---------------------------------------------------------------------------
# Timeout discipline (Finding 5) — TimeoutExpired surfaces as FAILED, not skipped
# ---------------------------------------------------------------------------


def test_timeout_surfaces_as_failed_not_skipped(tmp_path: Path) -> None:
    slow = _write(tmp_path / "slow.test.py", "import time\ntime.sleep(5)\n")
    suites = [_suite(slow, "python3", "py-nonnative")]
    results = run_suites(suites, repo_root=tmp_path, timeout=1)
    assert len(results) == 1
    assert results[0].exit_code != 0
    assert "TIMEOUT" in results[0].captured_output


def test_timeout_failure_included_in_overall_ok(tmp_path: Path) -> None:
    slow = _write(tmp_path / "slow.test.py", "import time\ntime.sleep(5)\n")
    fast_pass = _write(tmp_path / "fast.test.py", "import sys\nsys.exit(0)\n")
    suites = [
        _suite(slow, "python3", "py-nonnative"),
        _suite(fast_pass, "python3", "py-nonnative"),
    ]
    results = run_suites(suites, repo_root=tmp_path, timeout=1)
    assert overall_ok(results) is False


# ---------------------------------------------------------------------------
# DEC-10 — missing runner surfaces as FAILED (rc 127), never masked as skip
# ---------------------------------------------------------------------------


def test_unknown_runner_kind_raises(tmp_path: Path) -> None:
    ghost = _write(tmp_path / "ghost.test.py", "import sys\nsys.exit(0)\n")
    suite = _suite(ghost, "definitely-not-a-real-runner-kind", "py-nonnative")
    with pytest.raises(KeyError):
        run_suites([suite], repo_root=tmp_path)


def test_missing_runner_binary_surfaces_as_failed_rc_127(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Simulate a missing runner binary by pointing PATH somewhere empty, so
    # `node` resolves to nothing. DEC-10: this must surface as a FAILED
    # SuiteResult (rc 127), never a silently-masked skip.
    fake = _write(tmp_path / "example.test.js", "// noop\n")
    suites = [_suite(fake, "node", "js-suffix")]
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    results = run_suites(suites, repo_root=tmp_path)
    assert len(results) == 1
    assert results[0].exit_code == 127
    assert overall_ok(results) is False
