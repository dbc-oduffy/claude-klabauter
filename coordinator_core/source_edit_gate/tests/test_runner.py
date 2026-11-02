"""Tests for `coordinator_core.source_edit_gate.runner.run_selected` --
temp-dir hygiene, process-group spawn/cleanup, and timeout handling.

In-process tests monkeypatch `subprocess.Popen`, so nothing there spawns a
real pytest/vitest process (no `spawns_process` mark needed). A handful of
tests marked `spawns_process` exercise the real process-group cleanup against
an actual grandchild process.
"""

from __future__ import annotations

import glob
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from coordinator_core.conftest import ForeignProcessKill as ForeignProcessKillLike
from coordinator_core.source_edit_gate import runner as runner_module


def _tmp_dirs_matching(prefix: str) -> list:
    return glob.glob(os.path.join(__import__("tempfile").gettempdir(), f"{prefix}*"))


@pytest.fixture(autouse=True)
def _top_level_depth(monkeypatch):
    """Ambient depth is whatever launched this suite; pin it to 0 so the runner sees a top-level caller."""
    monkeypatch.setenv("COORDINATOR_PYTEST_DEPTH", "0")


@pytest.fixture(autouse=True)
def _capped_job_over_patched_popen(monkeypatch):
    """Routes `spawn_in_capped_job` to whatever `subprocess.Popen` the test installed (no real job), recording its kwargs."""
    from coordinator_core.diagnostics import contained_run as cr

    seen = {}

    def fake_capped(argv, *, popen_kwargs=None, **kw):
        seen["popen_kwargs"] = dict(popen_kwargs or {})
        return subprocess.Popen(argv, **(popen_kwargs or {})), lambda: seen.setdefault("closed", True)

    monkeypatch.setattr(cr, "spawn_in_capped_job", fake_capped)
    return seen


class _FakeProc:
    """Stand-in for `subprocess.Popen` -- already "exited" by the time
    `.wait()` returns, so `_terminate_group`'s `proc.poll() is not None`
    early-return means no real `killpg`/`taskkill` call happens for these
    in-process tests."""

    def __init__(self, argv, cwd=None, stdout=None, stderr=None, **kwargs):
        self.argv = argv
        self.cwd = cwd
        self.kwargs = kwargs
        self.pid = 999999
        self._returncode = 0
        self._on_wait = None

    def wait(self, timeout=None):
        if self._on_wait is not None:
            self._on_wait()
        return self._returncode

    def poll(self):
        return self._returncode


def _write_junit(junit_path: Path) -> None:
    junit_path.parent.mkdir(parents=True, exist_ok=True)
    junit_path.write_text(
        '<?xml version="1.0"?><testsuite tests="0" failures="0"></testsuite>',
        encoding="utf-8",
    )


def test_run_selected_pytest_removes_temp_dir_on_success(tmp_path, monkeypatch):
    captured = {}

    def fake_popen(argv, cwd=None, stdout=None, stderr=None, **kwargs):
        for arg in argv:
            if isinstance(arg, str) and arg.startswith("--basetemp="):
                captured["basetemp"] = Path(arg.split("=", 1)[1])
            if isinstance(arg, str) and arg.startswith("--junitxml="):
                junit_path = Path(arg.split("=", 1)[1])
                _write_junit(junit_path)
                captured["junit"] = junit_path
        assert captured["basetemp"].parent.is_dir()
        return _FakeProc(argv, cwd=cwd, stdout=stdout, stderr=stderr, **kwargs)

    monkeypatch.setattr(runner_module.subprocess, "Popen", fake_popen)

    before = set(_tmp_dirs_matching("source-edit-gate-pytest-"))
    result = runner_module.run_selected(str(tmp_path), ["tests/test_x.py"], "pytest")
    after = set(_tmp_dirs_matching("source-edit-gate-pytest-"))

    assert result is not None
    assert after - before == set()
    assert not captured["junit"].parent.exists()


def test_run_selected_pytest_removes_temp_dir_on_subprocess_failure(tmp_path, monkeypatch):
    def fake_popen(*a, **k):
        raise OSError("boom")

    monkeypatch.setattr(runner_module.subprocess, "Popen", fake_popen)

    before = set(_tmp_dirs_matching("source-edit-gate-pytest-"))
    with pytest.raises(OSError):
        runner_module.run_selected(str(tmp_path), ["tests/test_x.py"], "pytest")
    after = set(_tmp_dirs_matching("source-edit-gate-pytest-"))

    assert after - before == set()


def test_run_selected_pytest_uses_xdist_when_importable(tmp_path, monkeypatch):
    captured = {}

    def fake_find_spec(name):
        return object() if name == "xdist" else None

    def fake_popen(argv, cwd=None, stdout=None, stderr=None, **kwargs):
        captured["argv"] = argv
        for arg in argv:
            if isinstance(arg, str) and arg.startswith("--junitxml="):
                _write_junit(Path(arg.split("=", 1)[1]))
        return _FakeProc(argv, cwd=cwd, stdout=stdout, stderr=stderr, **kwargs)

    monkeypatch.setattr(runner_module.importlib.util, "find_spec", fake_find_spec)
    monkeypatch.setattr(runner_module.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(runner_module, "_usable_ram_gb", lambda: 16.0)
    monkeypatch.setattr(runner_module.subprocess, "Popen", fake_popen)

    runner_module.run_selected(str(tmp_path), ["tests/test_x.py"], "pytest")

    argv = captured["argv"]
    assert "-n" in argv
    n_index = argv.index("-n")
    assert argv[n_index + 1] == str(min(8 // 2, int(16.0 * 1024 // 150)))
    assert "-p" in argv
    p_index = argv.index("-p")
    assert argv[p_index + 1] == "no:cacheprovider"


def test_usable_ram_gb_reads_avail_mb(monkeypatch):
    from coordinator_core.telemetry import host_sampler

    monkeypatch.setattr(host_sampler, "avail_mb", lambda: (2048, 65536))
    assert runner_module._usable_ram_gb() == 2.0


def test_usable_ram_gb_unreadable_avail_is_conservative_not_total(monkeypatch):
    from coordinator_core.telemetry import host_sampler

    monkeypatch.setattr(host_sampler, "avail_mb", lambda: (None, 65536))
    assert runner_module._usable_ram_gb() == 4.0


def test_run_selected_pytest_skips_xdist_when_not_importable(tmp_path, monkeypatch):
    captured = {}

    def fake_popen(argv, cwd=None, stdout=None, stderr=None, **kwargs):
        captured["argv"] = argv
        for arg in argv:
            if isinstance(arg, str) and arg.startswith("--junitxml="):
                _write_junit(Path(arg.split("=", 1)[1]))
        return _FakeProc(argv, cwd=cwd, stdout=stdout, stderr=stderr, **kwargs)

    monkeypatch.setattr(runner_module.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(runner_module.subprocess, "Popen", fake_popen)

    runner_module.run_selected(str(tmp_path), ["tests/test_x.py"], "pytest")

    argv = captured["argv"]
    assert "-n" not in argv
    assert "-p" not in argv


def test_run_selected_pytest_adds_timeout_flag_when_pytest_timeout_importable(tmp_path, monkeypatch):
    captured = {}
    real_find_spec = runner_module.importlib.util.find_spec

    def fake_find_spec(name):
        if name == "pytest_timeout":
            return object()
        if name == "xdist":
            return None
        return real_find_spec(name)

    def fake_popen(argv, cwd=None, stdout=None, stderr=None, **kwargs):
        captured["argv"] = argv
        for arg in argv:
            if isinstance(arg, str) and arg.startswith("--junitxml="):
                _write_junit(Path(arg.split("=", 1)[1]))
        return _FakeProc(argv, cwd=cwd, stdout=stdout, stderr=stderr, **kwargs)

    monkeypatch.setattr(runner_module.importlib.util, "find_spec", fake_find_spec)
    monkeypatch.setattr(runner_module.subprocess, "Popen", fake_popen)

    runner_module.run_selected(str(tmp_path), ["tests/test_x.py"], "pytest", pytest_timeout_s=42)

    argv = captured["argv"]
    # No explicit `-p pytest_timeout` -- an importable pytest_timeout
    # auto-registers itself, and `-p` for it a second time is a pytest
    # usage error (double registration).
    assert "pytest_timeout" not in argv
    assert "--timeout=42" in argv


def test_run_selected_pytest_omits_timeout_flag_when_pytest_timeout_not_importable(tmp_path, monkeypatch):
    captured = {}
    real_find_spec = runner_module.importlib.util.find_spec

    def fake_find_spec(name):
        if name == "pytest_timeout":
            return None
        if name == "xdist":
            return None
        return real_find_spec(name)

    def fake_popen(argv, cwd=None, stdout=None, stderr=None, **kwargs):
        captured["argv"] = argv
        for arg in argv:
            if isinstance(arg, str) and arg.startswith("--junitxml="):
                _write_junit(Path(arg.split("=", 1)[1]))
        return _FakeProc(argv, cwd=cwd, stdout=stdout, stderr=stderr, **kwargs)

    monkeypatch.setattr(runner_module.importlib.util, "find_spec", fake_find_spec)
    monkeypatch.setattr(runner_module.subprocess, "Popen", fake_popen)

    runner_module.run_selected(str(tmp_path), ["tests/test_x.py"], "pytest")

    argv = captured["argv"]
    assert "pytest_timeout" not in argv
    assert not any(isinstance(a, str) and a.startswith("--timeout=") for a in argv)


def test_run_selected_pytest_spawns_in_own_process_group(tmp_path, monkeypatch):
    captured = {}

    def fake_popen(argv, cwd=None, stdout=None, stderr=None, **kwargs):
        captured["kwargs"] = kwargs
        for arg in argv:
            if isinstance(arg, str) and arg.startswith("--junitxml="):
                _write_junit(Path(arg.split("=", 1)[1]))
        return _FakeProc(argv, cwd=cwd, stdout=stdout, stderr=stderr, **kwargs)

    monkeypatch.setattr(runner_module.subprocess, "Popen", fake_popen)

    runner_module.run_selected(str(tmp_path), ["tests/test_x.py"], "pytest")

    if runner_module._IS_WINDOWS:
        assert captured["kwargs"].get("creationflags", 0) & subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert captured["kwargs"].get("start_new_session") is True


def test_run_selected_pytest_group_timeout_returns_none_and_kills_group(tmp_path, monkeypatch):
    killed = {}

    class _HangingProc(_FakeProc):
        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd=self.argv, timeout=timeout)

        def poll(self):
            return None  # still "alive" from `_terminate_group`'s point of view

    def fake_popen(argv, cwd=None, stdout=None, stderr=None, **kwargs):
        for arg in argv:
            if isinstance(arg, str) and arg.startswith("--junitxml="):
                _write_junit(Path(arg.split("=", 1)[1]))
        return _HangingProc(argv, cwd=cwd, stdout=stdout, stderr=stderr, **kwargs)

    def fake_terminate_group(proc):
        killed["called"] = True

    monkeypatch.setattr(runner_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(runner_module, "_terminate_group", fake_terminate_group)

    with pytest.raises(runner_module.GroupTimeout):
        runner_module.run_selected(
            str(tmp_path), ["tests/test_x.py"], "pytest", group_timeout_s=0.01
        )

    assert killed.get("called") is True


@pytest.mark.parametrize("timed_out", [False, True])
def test_run_with_group_cleanup_frees_its_admission_reservation_on_exit_and_on_timeout(monkeypatch, tmp_path, timed_out):
    from coordinator_core.bash_guards import _heavy_command_class as hcc

    released = []
    monkeypatch.setattr(hcc, "admit_before_spawn", lambda *a, **k: hcc.SpawnAdmission(True, "", "k1", 1.0))
    monkeypatch.setattr(hcc, "register_spawn", lambda *a, **k: None)
    monkeypatch.setattr(hcc, "release_spawn", lambda key, pid, host=None: released.append((key, pid)))

    class _Proc(_FakeProc):
        def wait(self, timeout=None):
            if timed_out:
                raise subprocess.TimeoutExpired(cmd=self.argv, timeout=timeout)
            return 0

    monkeypatch.setattr(runner_module.subprocess, "Popen", _Proc)

    result = runner_module._run_with_group_cleanup(["pytest", "t.py", "-n", "2"], cwd=str(tmp_path), timeout_s=1)
    assert result is (not timed_out)
    assert released == [("k1", 999999)]


def test_run_selected_vitest_removes_temp_dir_on_success(tmp_path, monkeypatch):
    captured = {}

    def fake_which(name):
        return "npx"

    def fake_popen(argv, cwd=None, stdout=None, stderr=None, **kwargs):
        for arg in argv:
            if isinstance(arg, str) and arg.startswith("--outputFile="):
                out_path = Path(arg.split("=", 1)[1])
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text('{"testResults": []}', encoding="utf-8")
                captured["out"] = out_path
        return _FakeProc(argv, cwd=cwd, stdout=stdout, stderr=stderr, **kwargs)

    monkeypatch.setattr(runner_module.shutil, "which", fake_which)
    monkeypatch.setattr(runner_module.subprocess, "Popen", fake_popen)

    before = set(_tmp_dirs_matching("source-edit-gate-vitest-"))
    runner_module.run_selected(str(tmp_path), ["src/x.test.ts"], "vitest")
    after = set(_tmp_dirs_matching("source-edit-gate-vitest-"))

    assert after - before == set()
    assert not captured["out"].parent.exists()


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group semantics")
def test_terminate_group_kills_grandchild_spawned_under_child(tmp_path):
    """A real child that spawns its own grandchild (mirrors the observed
    leak: a pytest run spawning a daemon supervisor / `sleep_forever.py`)
    must have BOTH reaped by `_terminate_group`, not just the immediate
    child -- proves the `start_new_session=True` process-group spawn plus
    `os.killpg` cleanup actually reaches a grandchild."""
    grandchild_script = tmp_path / "grandchild.py"
    grandchild_script.write_text(
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        "print(p.pid, flush=True)\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )

    proc = subprocess.Popen(
        [sys.executable, str(grandchild_script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        **runner_module._no_console_group_popen_kwargs(),
    )
    try:
        grandchild_pid = int(proc.stdout.readline().strip())

        def _alive(pid):
            # `os.kill(pid, 0)` is guarded in this test tree
            # (`conftest.py::_guarded_os_kill`) to refuse a pid outside our
            # own process tree -- which is also what a dead grandchild's pid
            # look like once the OS recycles it, so that guard's own
            # `ForeignProcessKill` is read as "not alive" here, same as
            # `ProcessLookupError`.
            try:
                os.kill(pid, 0)
                return True
            except (ProcessLookupError, ForeignProcessKillLike):
                return False

        assert _alive(grandchild_pid)

        runner_module._terminate_group(proc)

        deadline = time.time() + 5
        while time.time() < deadline and (_alive(proc.pid) or _alive(grandchild_pid)):
            time.sleep(0.1)

        assert not _alive(proc.pid)
        assert not _alive(grandchild_pid)
    finally:
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def _boom_popen(*a, **k):
    raise AssertionError("spawned")


def test_run_with_group_cleanup_refuses_nested_pytest_without_spawn(monkeypatch):
    monkeypatch.setenv("COORDINATOR_PYTEST_DEPTH", "1")
    monkeypatch.setattr(runner_module.subprocess, "Popen", _boom_popen)
    assert runner_module._run_with_group_cleanup([sys.executable, "-m", "pytest", "t.py"], cwd=".", timeout_s=1) is None


def test_run_with_group_cleanup_refuses_selection_holding_the_calling_test(monkeypatch, tmp_path):
    monkeypatch.delenv("COORDINATOR_PYTEST_DEPTH", raising=False)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_self.py::test_x (call)")
    monkeypatch.setattr(runner_module.subprocess, "Popen", _boom_popen)
    for target in ("tests/test_self.py", "tests", "."):
        argv = [sys.executable, "-m", "pytest", target, "-n", "2"]
        assert runner_module._run_with_group_cleanup(argv, cwd=str(tmp_path), timeout_s=1) is None


def test_run_with_group_cleanup_spawns_pytest_child_one_level_deeper(monkeypatch, tmp_path):
    monkeypatch.delenv("COORDINATOR_PYTEST_DEPTH", raising=False)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_self.py::test_x (call)")
    seen = {}

    def fake_popen(argv, cwd=None, stdout=None, stderr=None, **kwargs):
        seen.update(kwargs)
        return _FakeProc(argv, cwd=cwd, **kwargs)

    monkeypatch.setattr(runner_module.subprocess, "Popen", fake_popen)
    argv = [sys.executable, "-m", "pytest", "tests/test_other.py"]
    assert runner_module._run_with_group_cleanup(argv, cwd=str(tmp_path), timeout_s=1) is True
    assert seen["env"]["COORDINATOR_PYTEST_DEPTH"] == "1"
    assert "PYTEST_CURRENT_TEST" not in seen["env"]


def test_run_with_group_cleanup_pytest_child_carries_guard_plugin_and_closes_job(
    monkeypatch, tmp_path, _capped_job_over_patched_popen
):
    monkeypatch.setattr(runner_module.subprocess, "Popen", lambda argv, cwd=None, **kw: _FakeProc(argv, cwd=cwd, **kw))
    argv = [sys.executable, "-m", "pytest", "tests/test_other.py"]
    assert runner_module._run_with_group_cleanup(argv, cwd=str(tmp_path), timeout_s=1) is True
    env = _capped_job_over_patched_popen["popen_kwargs"]["env"]
    assert "coordinator_core.testing.nested_session_guard" in env["PYTEST_ADDOPTS"]
    if runner_module._IS_WINDOWS:
        assert _capped_job_over_patched_popen.get("closed") is True


def test_run_with_group_cleanup_leaves_non_pytest_env_alone(monkeypatch, tmp_path):
    monkeypatch.setenv("COORDINATOR_PYTEST_DEPTH", "1")
    seen = {}

    def fake_popen(argv, cwd=None, stdout=None, stderr=None, **kwargs):
        seen.update(kwargs)
        return _FakeProc(argv, cwd=cwd, **kwargs)

    monkeypatch.setattr(runner_module.subprocess, "Popen", fake_popen)
    assert runner_module._run_with_group_cleanup(["pnpm", "vitest", "run"], cwd=str(tmp_path), timeout_s=1) is True
    assert "env" not in seen
