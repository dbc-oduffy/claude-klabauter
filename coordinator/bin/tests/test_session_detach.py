"""Tests for coordinator/bin/lib/session_detach.py (injected platform, popen, proc_table; no real spawn)."""

from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parents[1] / "lib"
_spec = importlib.util.spec_from_file_location("session_detach_under_test", _LIB / "session_detach.py")
sd = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = sd
_spec.loader.exec_module(sd)

NO_WINDOW = 0x08000000
NEW_GROUP = 0x00000200
BREAKAWAY = 0x01000000


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(sd.DETACHED_ENV, raising=False)
    monkeypatch.delenv(sd.FAKE_SESSION_ENV, raising=False)


class FakeProc:
    def __init__(self, pid=777):
        self.pid = pid


class FakePopen:
    """Records calls; raises OSError for the first `fail` calls."""

    def __init__(self, fail=0):
        self.fail = fail
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        if len(self.calls) <= self.fail:
            raise OSError("breakaway denied")
        return FakeProc()


def _row(pid, ppid, image, create=1.0):
    host = sd._host()
    return host.ProcRow(pid, ppid, create, 0, 0, image)


# --- spawn_detached -------------------------------------------------------------------------


def test_win32_flags_and_pipes(tmp_path):
    popen = FakePopen()
    pid = sd.spawn_detached(["py", "x"], tmp_path / "l" / "g.log", strict=True, popen=popen, platform="win32")
    assert pid == 777
    cmd, kw = popen.calls[0]
    assert kw["creationflags"] == NO_WINDOW | NEW_GROUP | BREAKAWAY
    assert kw["stdin"] is subprocess.DEVNULL
    assert kw["close_fds"] is True
    assert "start_new_session" not in kw
    assert (tmp_path / "l" / "g.log").exists()


def test_posix_start_new_session(tmp_path):
    popen = FakePopen()
    sd.spawn_detached(["py"], tmp_path / "g.log", strict=True, popen=popen, platform="linux")
    kw = popen.calls[0][1]
    assert kw["start_new_session"] is True
    assert "creationflags" not in kw


def test_strict_refuses_on_oserror(tmp_path):
    popen = FakePopen(fail=1)
    assert sd.spawn_detached(["py"], tmp_path / "g.log", strict=True, popen=popen, platform="win32") is None
    assert len(popen.calls) == 1


def test_non_strict_retries_without_breakaway(tmp_path):
    popen = FakePopen(fail=1)
    pid = sd.spawn_detached(["py"], tmp_path / "g.log", strict=False, popen=popen, platform="win32")
    assert pid == 777
    assert popen.calls[1][1]["creationflags"] == NO_WINDOW | NEW_GROUP


def test_non_strict_second_failure_propagates(tmp_path):
    popen = FakePopen(fail=2)
    with pytest.raises(OSError):
        sd.spawn_detached(["py"], tmp_path / "g.log", strict=False, popen=popen, platform="win32")


def test_pythonw_swapped_for_python(tmp_path):
    popen = FakePopen()
    exe = str(tmp_path / "py" / "pythonw.exe")
    sd.spawn_detached([exe, "s.py"], tmp_path / "g.log", strict=True, popen=popen, platform="win32")
    assert Path(popen.calls[0][0][0]).name == "python.exe"
    assert popen.calls[0][0][1] == "s.py"


# --- session_parented -----------------------------------------------------------------------


def _chain(*images):
    """Rows pid 100.. where each is the child of the next; the first is 'me'."""
    return [_row(100 + i, 101 + i if i + 1 < len(images) else 0, img, create=10.0 - i) for i, img in enumerate(images)]


def test_claude_ancestor_detected():
    rows = _chain("python.exe", "bash.exe", "claude.exe", "explorer.exe")
    assert sd.session_parented(proc_table=lambda: rows, platform="win32", pid=100) is True


def test_no_claude_ancestor():
    rows = _chain("python.exe", "cmd.exe", "explorer.exe")
    assert sd.session_parented(proc_table=lambda: rows, platform="win32", pid=100) is False


def test_node_with_claude_cmdline_detected_and_plain_node_not():
    rows = _chain("python.exe", "node.exe", "explorer.exe")
    assert sd.session_parented(
        proc_table=lambda: rows, platform="win32", pid=100, cmdline=lambda p: "node claude-code/cli.js"
    )
    assert not sd.session_parented(
        proc_table=lambda: rows, platform="win32", pid=100, cmdline=lambda p: "node server.js"
    )


def test_reused_parent_pid_is_not_an_ancestor():
    me = _row(100, 101, "python.exe", create=5.0)
    stale = _row(101, 0, "claude.exe", create=9.0)
    assert sd.session_parented(proc_table=lambda: [me, stale], platform="win32", pid=100) is False


def test_ancestry_read_failure_is_inline():
    def boom():
        raise OSError("no table")

    assert sd.session_parented(proc_table=boom, platform="win32", pid=100) is False


def test_fake_session_env_forces_true(monkeypatch):
    def boom():
        raise OSError("no table")

    monkeypatch.setenv(sd.FAKE_SESSION_ENV, "1")
    assert sd.session_parented(proc_table=boom, platform="win32", pid=100) is True


# --- ensure_detached ------------------------------------------------------------------------


_SESSION = _chain("python.exe", "claude.exe", "explorer.exe")
_NO_SESSION = _chain("python.exe", "explorer.exe")


def _ensure(tmp_path, rows, popen, env=None, platform="win32"):
    out, err = io.StringIO(), io.StringIO()
    rc = sd.ensure_detached(
        "guard",
        ["--log", "g.log"],
        tmp_path / "g.log",
        env=env or {},
        script=str(tmp_path / "guard.py"),
        popen=popen,
        platform=platform,
        proc_table=lambda: rows,
        out=out,
        err=err,
    )
    return rc, out.getvalue(), err.getvalue()


def _patch_pid(monkeypatch):
    monkeypatch.setattr(sd.os, "getpid", lambda: 100)


def test_marker_set_runs_inline(tmp_path, monkeypatch):
    _patch_pid(monkeypatch)
    popen = FakePopen()
    rc, out, _ = _ensure(tmp_path, _SESSION, popen, env={sd.DETACHED_ENV: "1"})
    assert rc is None and not popen.calls and out == ""


def test_no_claude_ancestor_runs_inline(tmp_path, monkeypatch):
    _patch_pid(monkeypatch)
    popen = FakePopen()
    rc, _, _ = _ensure(tmp_path, _NO_SESSION, popen)
    assert rc is None and not popen.calls


def test_claude_ancestor_relaunches_detached(tmp_path, monkeypatch):
    _patch_pid(monkeypatch)
    popen = FakePopen()
    rc, out, err = _ensure(tmp_path, _SESSION, popen)
    assert rc == 0 and err == ""
    assert out == f"DETACHED guard pid=777 log={tmp_path / 'g.log'}\n"
    cmd, kw = popen.calls[0]
    assert cmd == [sys.executable, str(tmp_path / "guard.py"), "--log", "g.log"]
    assert kw["env"][sd.DETACHED_ENV] == "1"
    assert kw["creationflags"] & BREAKAWAY


def test_strict_failure_refuses_with_exit_5_and_one_line(tmp_path, monkeypatch):
    _patch_pid(monkeypatch)
    rc, out, err = _ensure(tmp_path, _SESSION, FakePopen(fail=1))
    assert rc == sd._host().CANNOT_DETACH_EXIT == 5
    assert out == "" and len(err.splitlines()) == 1
    assert err.startswith("guard: cannot leave the Claude Code session's job; launch it detached: Start-Process -WindowStyle Hidden ")


def test_posix_refusal_names_setsid(tmp_path, monkeypatch):
    _patch_pid(monkeypatch)
    rc, _, err = _ensure(tmp_path, _SESSION, FakePopen(fail=1), platform="linux")
    assert rc == 5 and "setsid nohup " in err and err.rstrip().endswith("&")
