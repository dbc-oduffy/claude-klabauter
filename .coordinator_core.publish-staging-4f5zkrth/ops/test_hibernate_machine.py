"""Tests for coordinator_core.ops.hibernate_machine (machine.hibernate, W3-B1).

Every dispatch path is mocked — these tests must NEVER actually suspend the
machine. `_run_binary` and `_windows_ctypes_suspend` are the two seams the
module routes ALL real power-state actions through; each test monkeypatches
both and asserts on recorded argv, never on live OS state.

AC7/CC-4 double-invocation coverage mocks the dispatch layer and asserts the
op is re-invocable (identical results, no state accrual) — the runtime
idempotency claim itself is vacuous per the module's DEC-7 docstring note
(a second invocation never runs on a suspended machine).

Spec backlink: pln-wave-3-design-settlements-15-d-76fdbd § B1
"""
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
    """Neutralize both dispatch seams; record every would-be binary spawn."""
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
    """SetSuspendState success → no shutdown spawn (ctypes leads per B1)."""
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
    """ctypes unavailable/failed (fixture returns False) → shutdown /h."""
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
    """CC-7 fail-loud: a nonzero exit surfaces, never `dispatched: true`."""

    def _failing_run_binary(argv):
        raise HibernateDispatchError(argv, 1, "pmset: not permitted")

    monkeypatch.setattr(hibernate_machine, "_run_binary", _failing_run_binary)
    with pytest.raises(HibernateDispatchError) as excinfo:
        hibernate(platform="darwin")
    assert excinfo.value.argv == ["pmset", "sleepnow"]
    assert excinfo.value.returncode == 1


def test_run_binary_nonzero_exit_raises(monkeypatch):
    """The real seam converts a nonzero subprocess exit into the structured
    error (subprocess itself mocked — nothing real is spawned)."""

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
    """platform=None resolves the interpreter's own sys.platform branch —
    dispatch seams are mocked, so nothing real fires on any host."""
    monkeypatch.setattr(hibernate_machine.sys, "platform", "linux")
    result = hibernate()
    assert result["platform"] == "linux"
    assert spawn_log == [["systemctl", "hibernate"]]


def test_double_invocation_reinvocable_no_state_accrues(spawn_log):
    """AC7/CC-4: dispatch layer mocked; two identical calls yield identical
    results and exactly one spawn each — the op accrues no state between
    invocations. Runtime idempotency is vacuous per the DEC-7 docstring note
    (the second invocation never runs on a suspended machine); this asserts
    re-invocability, which is what IS testable."""
    first = hibernate(platform="darwin")
    second = hibernate(platform="darwin")
    assert first == second
    assert spawn_log == [["pmset", "sleepnow"], ["pmset", "sleepnow"]]


def test_handler_double_invocation_via_dispatch_surface(monkeypatch, spawn_log):
    """Same AC7 shape through the registered async handler (platform pinned
    so the assertion is host-independent; dispatch seams stay mocked)."""
    monkeypatch.setattr(hibernate_machine.sys, "platform", "darwin")
    first = hibernate_machine._machine_hibernate({}, repo_root=None)
    second = hibernate_machine._machine_hibernate({}, repo_root=None)
    assert first == second
    assert first["dispatched"] is True
    assert spawn_log == [["pmset", "sleepnow"], ["pmset", "sleepnow"]]
