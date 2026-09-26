from __future__ import annotations

import pytest

from coordinator_core.ops import hibernate_machine
from coordinator_core.ops.hibernate_machine import (
    HibernateDispatchError,
    UnsupportedPlatformError,
    hibernate,
)


@pytest.fixture()
def spawn_log(monkeypatch):
    calls: list[list[str]] = []

    def _fake_run_binary(argv):
        calls.append(list(argv))

    monkeypatch.setattr(hibernate_machine, "_run_binary", _fake_run_binary)
    monkeypatch.setattr(
        hibernate_machine, "_windows_ctypes_suspend", lambda: False
    )
    return calls


def test_darwin_dispatches_pmset_sleepnow(spawn_log):
    result = hibernate(platform="darwin")
    assert spawn_log == [["pmset", "sleepnow"]]
    assert result == {
        "platform": "darwin",
        "method": "pmset sleepnow",
        "dispatched": True,
    }


def test_linux_dispatches_systemctl_hibernate(spawn_log):
    result = hibernate(platform="linux")
    assert spawn_log == [["systemctl", "hibernate"]]
    assert result == {
        "platform": "linux",
        "method": "systemctl hibernate",
        "dispatched": True,
    }


def test_windows_ctypes_primary_no_binary_fallback(monkeypatch, spawn_log):
    monkeypatch.setattr(
        hibernate_machine, "_windows_ctypes_suspend", lambda: True
    )
    result = hibernate(platform="win32")
    assert spawn_log == []
    assert result == {
        "platform": "win32",
        "method": "ctypes PowrProf.SetSuspendState(1, 0, 0)",
        "dispatched": True,
    }


def test_windows_ctypes_failure_falls_back_to_shutdown_h(spawn_log):
    result = hibernate(platform="win32")
    assert spawn_log == [["shutdown", "/h"]]
    assert result == {
        "platform": "win32",
        "method": "shutdown /h",
        "dispatched": True,
    }


def test_unknown_platform_raises_structured_error_naming_platform(spawn_log):
    with pytest.raises(UnsupportedPlatformError) as excinfo:
        hibernate(platform="sunos5")
    assert excinfo.value.platform == "sunos5"
    assert "sunos5" in str(excinfo.value)
    assert spawn_log == []


def test_dispatch_failure_raises_not_reported_as_success(monkeypatch):

    def _failing_run_binary(argv):
        raise HibernateDispatchError(argv, 1, "pmset: not permitted")

    monkeypatch.setattr(hibernate_machine, "_run_binary", _failing_run_binary)
    with pytest.raises(HibernateDispatchError) as excinfo:
        hibernate(platform="darwin")
    assert excinfo.value.argv == ["pmset", "sleepnow"]
    assert excinfo.value.returncode == 1


def test_run_binary_nonzero_exit_raises(monkeypatch):

    class _Proc:
        returncode = 1
        stderr = "Operation not permitted"
        stdout = ""

    monkeypatch.setattr(
        hibernate_machine.subprocess, "run", lambda *a, **k: _Proc()
    )
    with pytest.raises(HibernateDispatchError) as excinfo:
        hibernate_machine._run_binary(["pmset", "sleepnow"])
    assert "Operation not permitted" in str(excinfo.value)


def test_default_platform_is_sys_platform(monkeypatch, spawn_log):
    monkeypatch.setattr(hibernate_machine.sys, "platform", "linux")
    result = hibernate()
    assert result["platform"] == "linux"
    assert spawn_log == [["systemctl", "hibernate"]]


def test_double_invocation_reinvocable_no_state_accrues(spawn_log):
    first = hibernate(platform="darwin")
    second = hibernate(platform="darwin")
    assert first == second
    assert spawn_log == [["pmset", "sleepnow"], ["pmset", "sleepnow"]]


def test_handler_double_invocation_via_dispatch_surface(monkeypatch, spawn_log):
    monkeypatch.setattr(hibernate_machine.sys, "platform", "darwin")
    first = hibernate_machine._machine_hibernate({}, repo_root=None)
    second = hibernate_machine._machine_hibernate({}, repo_root=None)
    assert first == second
    assert first["dispatched"] is True
    assert spawn_log == [["pmset", "sleepnow"], ["pmset", "sleepnow"]]
