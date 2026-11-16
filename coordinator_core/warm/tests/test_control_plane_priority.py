"""Supervisor and pool workers run above-normal; accept stays HIGH."""

from __future__ import annotations

import sys
import threading
from unittest import mock

import pytest

from coordinator_core.warm import breadcrumb, server, supervisor


def query_priority_class() -> int:
    """Runs inside the pool worker: read back its own priority class."""
    import ctypes

    k32 = ctypes.WinDLL("kernel32")
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    k32.GetPriorityClass.argtypes = [ctypes.c_void_p]
    return k32.GetPriorityClass(k32.GetCurrentProcess())


def _fake_win_ctypes(monkeypatch, set_result=1):
    kernel32 = mock.MagicMock()
    kernel32.SetPriorityClass.return_value = set_result
    kernel32.GetCurrentProcess.return_value = 321
    fake = mock.MagicMock()
    fake.windll.kernel32 = kernel32
    monkeypatch.setattr(server.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "ctypes", fake)
    return kernel32


def test_windows_worker_boost_sets_above_normal(monkeypatch):
    kernel32 = _fake_win_ctypes(monkeypatch)
    assert server._raise_worker_priority() is True
    assert server._WINDOWS_ABOVE_NORMAL_PRIORITY_CLASS == 0x8000
    kernel32.SetPriorityClass.assert_called_once_with(321, 0x8000)


def test_windows_boost_refused_reports_false(monkeypatch):
    _fake_win_ctypes(monkeypatch, set_result=0)
    assert server._raise_worker_priority() is False


def test_windows_boost_exception_is_swallowed(monkeypatch):
    kernel32 = _fake_win_ctypes(monkeypatch)
    kernel32.SetPriorityClass.side_effect = OSError
    assert server._raise_worker_priority() is False


def _posix(monkeypatch, base):
    calls = []
    monkeypatch.setattr(server.sys, "platform", "linux")
    monkeypatch.setattr(server.os, "PRIO_PROCESS", 0, raising=False)
    monkeypatch.setattr(server.os, "getpriority", lambda *_a: base, raising=False)
    monkeypatch.setattr(server.os, "setpriority", lambda *a: calls.append(a), raising=False)
    monkeypatch.setenv(server._BASE_NICE_ENV, "")  # registers the restore of the production write
    monkeypatch.delenv(server._BASE_NICE_ENV)
    return calls


def test_posix_worker_nice_is_minus_two_for_base_zero(monkeypatch):
    calls = _posix(monkeypatch, 0)
    assert server._raise_worker_priority() is True
    assert calls == [(0, 0, -2)]


def test_posix_worker_never_outranks_the_accept_process(monkeypatch):
    calls = _posix(monkeypatch, -5)
    monkeypatch.setenv(server._BASE_NICE_ENV, "0")
    assert server._raise_worker_priority() is True
    assert calls == [(0, 0, -2)]


def test_posix_supervisor_nice_is_minus_five(monkeypatch):
    calls = _posix(monkeypatch, 0)
    assert server._raise_worker_priority(supervisor._POSIX_SUPERVISOR_NICE) is True
    assert calls == [(0, 0, -5)]


def test_posix_boost_denied_reports_false(monkeypatch):
    _posix(monkeypatch, 0)

    def _deny(*_a):
        raise PermissionError

    monkeypatch.setattr(server.os, "setpriority", _deny, raising=False)
    assert server._raise_worker_priority() is False


def test_boost_result_is_logged(capsys):
    server._log_priority_boost("worker", False)
    assert "worker boost held=False" in capsys.readouterr().err


def test_supervisor_main_raises_priority_after_election_win(monkeypatch):
    seen = []
    monkeypatch.setattr(supervisor, "_raise_worker_priority", lambda n=None: seen.append(n) or True)
    monkeypatch.setattr(supervisor, "_log_priority_boost", lambda role, held: seen.append((role, held)))
    monkeypatch.setattr(supervisor, "_default_engine_clone", lambda: "root")
    monkeypatch.setattr(supervisor, "engine_root_divergence_message", lambda r: None)
    monkeypatch.setattr(supervisor.idle, "ladder_gone_tombstoned", lambda r: False)
    monkeypatch.setattr(supervisor.breadcrumb, "publish_marker_blocks_boot", lambda d: False)
    monkeypatch.setattr(supervisor.breadcrumb, "svc_dir", lambda r: r)
    monkeypatch.setattr(supervisor, "_elect_supervisor_slot", lambda r: object())

    def _stop(_root):
        raise RuntimeError("stop after priority")

    monkeypatch.setattr(supervisor, "_assert_credential_ready", _stop)
    monkeypatch.setattr(supervisor, "_release_election_handle", lambda h: None)
    with pytest.raises(RuntimeError, match="stop after priority"):
        supervisor.main()
    assert seen == [supervisor._POSIX_SUPERVISOR_NICE, ("supervisor", True)]


def test_supervisor_election_loss_does_not_boost(monkeypatch):
    seen = []
    monkeypatch.setattr(supervisor, "_log_priority_boost", lambda role, held: seen.append(role))
    monkeypatch.setattr(supervisor, "_default_engine_clone", lambda: "root")
    monkeypatch.setattr(supervisor, "engine_root_divergence_message", lambda r: None)
    monkeypatch.setattr(supervisor.idle, "ladder_gone_tombstoned", lambda r: False)
    monkeypatch.setattr(supervisor.breadcrumb, "publish_marker_blocks_boot", lambda d: False)
    monkeypatch.setattr(supervisor.breadcrumb, "svc_dir", lambda r: r)

    def _lost(_root):
        raise supervisor.election.ElectionLost("ep")

    monkeypatch.setattr(supervisor, "_elect_supervisor_slot", _lost)
    assert supervisor.main() == 0
    assert seen == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows priority readback only")
def test_real_pool_worker_reads_above_normal(tmp_path, monkeypatch):
    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(tmp_path / "runtime"))
    ctx = server._ServerContext.__new__(server._ServerContext)
    ctx._dispatch_pool = None
    ctx._dispatch_pool_lock = threading.Lock()
    ctx.engine_root = tmp_path / "engine"
    pool = ctx._ensure_dispatch_pool()
    try:
        got = pool.submit(query_priority_class).result(timeout=120)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    # Class values are not numerically ordered: membership, never `>=`.
    assert got in {0x8000, 0x80, 0x100}, hex(got)
