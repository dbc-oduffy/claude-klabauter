"""session_registry.pid_alive on Windows: access denied reads alive, a missing pid reads dead."""

from __future__ import annotations

import os
import types

from coordinator_core.group_em import session_registry


def _fake_ctypes(monkeypatch, *, handle, last_error=0, exit_code=259):
    class _Code:
        def __init__(self):
            self.value = 0

    k32 = types.SimpleNamespace(
        OpenProcess=lambda *a: handle,
        GetExitCodeProcess=lambda h, ref: setattr(ref, "value", exit_code) or True,
        CloseHandle=lambda h: True,
    )
    fake = types.SimpleNamespace(
        windll=types.SimpleNamespace(kernel32=k32),
        GetLastError=lambda: last_error,
        c_ulong=_Code,
        byref=lambda x: x,
    )
    monkeypatch.setitem(__import__("sys").modules, "ctypes", fake)
    monkeypatch.setattr(session_registry, "os", types.SimpleNamespace(name="nt"))


def test_windows_access_denied_reads_alive(monkeypatch):
    _fake_ctypes(monkeypatch, handle=0, last_error=5)
    assert session_registry.pid_alive(41712) is True


def test_windows_invalid_parameter_reads_dead(monkeypatch):
    _fake_ctypes(monkeypatch, handle=0, last_error=87)
    assert session_registry.pid_alive(41712) is False


def test_windows_exited_reads_dead_and_still_active_reads_alive(monkeypatch):
    _fake_ctypes(monkeypatch, handle=7, exit_code=1)
    assert session_registry.pid_alive(41712) is False
    _fake_ctypes(monkeypatch, handle=7, exit_code=259)
    assert session_registry.pid_alive(41712) is True


def test_nonpositive_pid_reads_dead():
    assert session_registry.pid_alive(0) is False
    assert session_registry.pid_alive(-3) is False


def test_session_core_pid_alive_is_the_registry_probe():
    from coordinator_core.session import core

    assert core.pid_alive is session_registry.pid_alive


def test_nonnumeric_pid_reads_dead_and_numeric_string_probes():
    assert session_registry.pid_alive(None) is False
    assert session_registry.pid_alive("abc") is False
    assert session_registry.pid_alive(str(os.getpid())) is True


def test_real_self_alive_and_bogus_dead():
    assert session_registry.pid_alive(os.getpid()) is True
    assert session_registry.pid_alive(2**31 - 1) is False
