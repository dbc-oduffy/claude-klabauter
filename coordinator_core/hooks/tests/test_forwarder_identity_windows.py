"""Item 38.3: the Windows forwarder-identity check must match against the
process's ARGV (the same seam the POSIX/`/proc/<pid>/cmdline` branch
matches against), never the executable's image path -- an interpreter's
image path (`.../python.exe`) never carries the trailing script argument,
so a check built on it can never match and is permanently blind.
"""

from __future__ import annotations

import psutil
import pytest

from coordinator_core.hooks import sessionstart_ensure_http_forwarder as mod


class _FakeProcess:
    def __init__(self, cmdline):
        self._cmdline = cmdline

    def cmdline(self):
        return self._cmdline


def test_windows_identity_matches_on_argv_not_image_path(monkeypatch):
    # The image path would be the interpreter itself (python.exe) -- the
    # regex must match against the script argument, not this path.
    monkeypatch.setattr(
        mod,
        "_windows_process_cmdline",
        # abs-path-ok: synthetic Windows argv fixture, not a resolved host path
        lambda pid: ["P:\\fixture-repo\\python.exe", "P:\\fixture-repo\\hooks\\http_hook_forwarder.py"],
    )
    assert mod._pid_is_a_forwarder_windows(4242) is True


def test_windows_identity_false_when_argv_is_unrelated(monkeypatch):
    monkeypatch.setattr(
        mod,
        "_windows_process_cmdline",
        # abs-path-ok: synthetic Windows argv fixture, not a resolved host path
        lambda pid: ["P:\\fixture-repo\\python.exe", "P:\\fixture-repo\\unrelated_script.py"],
    )
    assert mod._pid_is_a_forwarder_windows(4242) is False


def test_windows_identity_false_when_process_gone(monkeypatch):
    def _raise(pid):
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr(mod, "_windows_process_cmdline", _raise)
    assert mod._pid_is_a_forwarder_windows(4242) is False


def test_windows_identity_none_on_unreadable_process(monkeypatch):
    def _raise(pid):
        raise psutil.AccessDenied(pid)

    monkeypatch.setattr(mod, "_windows_process_cmdline", _raise)
    assert mod._pid_is_a_forwarder_windows(4242) is None


def test_windows_identity_none_when_psutil_unavailable(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "psutil":
            raise ImportError("no psutil")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    assert mod._pid_is_a_forwarder_windows(4242) is None


def test_windows_process_cmdline_reads_through_psutil(monkeypatch):
    monkeypatch.setattr(
        psutil, "Process", lambda pid: _FakeProcess(["python.exe", "http_hook_forwarder.py"])
    )
    assert mod._windows_process_cmdline(123) == ["python.exe", "http_hook_forwarder.py"]


def test_pid_is_a_forwarder_dispatches_to_windows_branch(monkeypatch):
    monkeypatch.setattr(mod.os, "name", "nt")
    monkeypatch.setattr(mod, "_pid_is_a_forwarder_windows", lambda pid: True)
    assert mod._pid_is_a_forwarder(1) is True
