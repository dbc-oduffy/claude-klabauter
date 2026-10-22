"""Tests for coordinator/bin/box-memory-wait-pid.py with injected waiters; no real spawn."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1] / "box-memory-wait-pid.py"
_spec = importlib.util.spec_from_file_location("box_memory_wait_pid", TOOL)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def test_exited_line(capsys):
    assert mod.main(["42"], waiter=lambda p, i, t: ("exited", 3)) == 0
    out = capsys.readouterr().out
    assert out.startswith("EXITED pid=42 code=3 elapsed=") and out.endswith("s\n")
    assert out.count("\n") == 1


def test_exited_unknown_code(capsys):
    assert mod.main(["42"], waiter=lambda p, i, t: ("exited", None)) == 0
    assert "code=?" in capsys.readouterr().out


def test_timeout(capsys):
    assert mod.main(["7", "--timeout", "1"], waiter=lambda p, i, t: ("timeout", None)) == 1
    assert capsys.readouterr().out == "TIMEOUT pid=7\n"


def test_missing_pid(capsys):
    assert mod.main(["9"], waiter=lambda p, i, t: ("nopid", None)) == 1
    assert capsys.readouterr().out == "NO-SUCH-PID pid=9\n"


def test_poll_branch_exits_after_alive_then_gone():
    calls = []

    def kill(pid, sig):
        calls.append((pid, sig))
        if len(calls) >= 3:
            raise ProcessLookupError

    state, code = mod.poll_waiter(5, 0.0, None, kill=kill, sleep=lambda s: None)
    assert (state, code) == ("exited", None) and calls[0] == (5, 0)


def test_poll_branch_missing_at_start():
    def kill(pid, sig):
        raise ProcessLookupError

    assert mod.poll_waiter(5, 0.0, None, kill=kill, sleep=lambda s: None)[0] == "nopid"


def test_poll_branch_timeout():
    t = [0.0]

    def sleep(s):
        t[0] += s

    state, _ = mod.poll_waiter(
        5, 1.0, 3.0, kill=lambda p, s: None, sleep=sleep, clock=lambda: t[0]
    )
    assert state == "timeout"


def test_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as e:
        mod.main(["--help"])
    assert e.value.code == 0 and "PID" in capsys.readouterr().out.upper()


def test_windows_without_psutil_refuses_instead_of_signalling(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)
    monkeypatch.setattr(mod.os, "name", "nt")
    with pytest.raises(SystemExit) as e:
        mod.default_waiter()
    assert "psutil is required" in str(e.value)


def test_posix_without_psutil_falls_back_to_poll(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)
    monkeypatch.setattr(mod.os, "name", "posix")
    assert mod.default_waiter() is mod.poll_waiter
