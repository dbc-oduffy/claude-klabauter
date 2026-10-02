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
