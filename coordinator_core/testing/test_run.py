
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.testing import nested_session_guard, spawn_depth
from coordinator_core.testing.collect import Suite
from coordinator_core.testing.run import (
    _resolve_worker_count,
    overall_ok,
    run_suites,
)


@pytest.fixture(autouse=True)
def _ambient_spawn_depth_is_zero(monkeypatch) -> None:
    """A suite run by an engine spawner carries depth 1; these tests set their own depth."""
    monkeypatch.delenv(spawn_depth.ENV_KEY, raising=False)
    monkeypatch.delenv(spawn_depth.COUNTED_KEY, raising=False)


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _suite(path: Path, runner_kind: str, family: str) -> Suite:
    return Suite(family=family, path=path, runner_kind=runner_kind)


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


class _FakeProc:
    returncode = 0

    def __init__(self, timeouts: int = 0) -> None:
        self._timeouts = timeouts

    def communicate(self, timeout=None):
        if self._timeouts:
            self._timeouts -= 1
            raise subprocess.TimeoutExpired("x", timeout, output="partial")
        return "ok", None


def _capture_run(monkeypatch: pytest.MonkeyPatch, timeouts: int = 0) -> list:
    class _Calls(list):
        closed: list

    calls = _Calls()
    calls_closed: list = []

    def fake_spawn(command, *, popen_kwargs=None, **kw):
        calls.append((list(command), dict(popen_kwargs or {})))
        return _FakeProc(timeouts), lambda: calls_closed.append(1)

    monkeypatch.setattr("coordinator_core.testing.run.spawn_in_capped_job", fake_spawn)
    calls.closed = calls_closed
    return calls


def test_py_native_batch_stamps_child_depth_and_strips_current_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _capture_run(monkeypatch)
    monkeypatch.delenv(spawn_depth.ENV_KEY, raising=False)
    monkeypatch.setenv(spawn_depth.CURRENT_TEST_KEY, "elsewhere/test_other.py::test_x (call)")
    suite = _suite(_write(tmp_path / "test_one.py", "def test_one(): pass\n"), "pytest", "py-native")
    results = run_suites([suite], repo_root=tmp_path)
    assert results[0].exit_code == 0
    env = calls[0][1]["env"]
    assert env[spawn_depth.ENV_KEY] == "1"
    assert spawn_depth.CURRENT_TEST_KEY not in env
    assert nested_session_guard.PLUGIN_MODULE in env["PYTEST_ADDOPTS"]
    assert calls.closed == [1]


def test_timeout_closes_the_job_and_keeps_captured_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _capture_run(monkeypatch, timeouts=1)
    monkeypatch.delenv(spawn_depth.ENV_KEY, raising=False)
    suite = _suite(_write(tmp_path / "s.test.py", "pass\n"), "python3", "py-nonnative")
    results = run_suites([suite], repo_root=tmp_path, timeout=1)
    assert results[0].exit_code == 124
    assert "ok" in results[0].captured_output and "[TIMEOUT]" in results[0].captured_output
    assert len(calls.closed) >= 1
    child_env = calls[0][1]["env"]
    assert child_env is None or "-p " + nested_session_guard.PLUGIN_MODULE not in child_env.get(
        "PYTEST_ADDOPTS", ""
    )


def test_py_native_batch_refuses_at_depth_and_spawns_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _capture_run(monkeypatch)
    monkeypatch.setenv(spawn_depth.ENV_KEY, "1")
    suite = _suite(_write(tmp_path / "test_one.py", "def test_one(): pass\n"), "pytest", "py-native")
    results = run_suites([suite], repo_root=tmp_path)
    assert results[0].exit_code != 0 and "refused" in results[0].captured_output
    assert calls == []


def test_py_native_batch_refuses_selection_containing_calling_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _capture_run(monkeypatch)
    monkeypatch.delenv(spawn_depth.ENV_KEY, raising=False)
    own = _write(tmp_path / "test_self.py", "def test_self(): pass\n")
    other = _write(tmp_path / "test_other.py", "def test_other(): pass\n")
    monkeypatch.setenv(spawn_depth.CURRENT_TEST_KEY, f"{own}::test_self (call)")

    refused = run_suites([_suite(own, "pytest", "py-native")], repo_root=tmp_path)
    assert refused[0].exit_code != 0 and "includes the calling test file" in refused[0].captured_output
    assert calls == []

    allowed = run_suites([_suite(other, "pytest", "py-native")], repo_root=tmp_path)
    assert allowed[0].exit_code == 0 and len(calls) == 1


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


def test_worker_cap_default_uses_half_cpu_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.cpu_count", lambda: 8)
    assert _resolve_worker_count(unit_count=10, jobs=None) == 4


def test_worker_cap_floors_at_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.cpu_count", lambda: 1)
    assert _resolve_worker_count(unit_count=10, jobs=None) == 1


def test_worker_cap_bounded_by_unit_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.cpu_count", lambda: 32)
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


def test_unknown_runner_kind_raises(tmp_path: Path) -> None:
    ghost = _write(tmp_path / "ghost.test.py", "import sys\nsys.exit(0)\n")
    suite = _suite(ghost, "definitely-not-a-real-runner-kind", "py-nonnative")
    with pytest.raises(KeyError):
        run_suites([suite], repo_root=tmp_path)


def test_missing_runner_binary_surfaces_as_failed_rc_127(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _write(tmp_path / "example.test.js", "// noop\n")
    suites = [_suite(fake, "node", "js-suffix")]
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    results = run_suites(suites, repo_root=tmp_path)
    assert len(results) == 1
    assert results[0].exit_code == 127
    assert overall_ok(results) is False
