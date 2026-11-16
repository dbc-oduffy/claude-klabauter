"""Warm server, supervisor and pool workers stay engine-class on a prime-tenant box: never demoted."""

from __future__ import annotations

import ast
import inspect
import sys

import pytest

from coordinator_core import tenant_priority
from coordinator_core.warm import polite_io, server, supervisor

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX nice readback")


@pytest.fixture
def recorder(monkeypatch):
    calls: list = []
    monkeypatch.setattr(server, "_raise_engine_priority", lambda: calls.append("raise-engine") or True)
    monkeypatch.setattr(server, "_raise_worker_priority", lambda *a: calls.append(("raise-worker", a)) or True)
    monkeypatch.setattr(polite_io, "lower_own_priority", lambda: calls.append("lower") or {})
    monkeypatch.setattr(server, "_log_priority_boost", lambda role, held: calls.append(("log", role, held)))
    return calls


@pytest.fixture
def applied(monkeypatch):
    seen: list = []
    monkeypatch.setattr(
        tenant_priority,
        "apply_to_self",
        lambda: seen.append("apply") or {"prime": True, "nice": 15, "bg": True},
        raising=False,
    )
    return seen


def _set_prime(monkeypatch, prime: bool) -> None:
    monkeypatch.setattr(tenant_priority, "is_prime_tenant", lambda: prime)


def _stub_worker_init(monkeypatch):
    monkeypatch.setattr(server, "_bind_null_std_streams", lambda: None)
    monkeypatch.setattr(server, "_bind_worker_stderr_log", lambda d: None)

    class _StubThread:
        def __init__(self, *a, **kw) -> None:
            pass

        def start(self) -> None:
            pass

    monkeypatch.setattr(server.threading, "Thread", _StubThread)


def test_no_engine_entry_calls_apply_to_self():
    for module in (server, supervisor):
        tree = ast.parse(inspect.getsource(module))
        names = {
            n.attr if isinstance(n, ast.Attribute) else n.id
            for n in ast.walk(tree)
            if isinstance(n, (ast.Attribute, ast.Name))
        }
        assert "apply_to_self" not in names and "_apply_prime_tenant_background" not in names


@posix_only
def test_worker_prime_posix_skips_nice_19_and_still_boosts(monkeypatch, recorder, applied):
    _set_prime(monkeypatch, True)
    _stub_worker_init(monkeypatch)
    server._worker_process_init()
    assert "lower" not in recorder
    assert ("raise-worker", ()) in recorder
    assert ("log", "worker", True) in recorder
    assert applied == []


@posix_only
def test_worker_prime_posix_issues_no_setpriority_19(monkeypatch, applied):
    import os

    seen: list = []
    monkeypatch.setattr(os, "setpriority", lambda which, who, prio: seen.append(prio))
    monkeypatch.setattr(os, "getpriority", lambda which, who: 0)
    monkeypatch.delenv(server._BASE_NICE_ENV, raising=False)
    _set_prime(monkeypatch, True)
    _stub_worker_init(monkeypatch)
    server._worker_process_init()
    assert 19 not in seen and seen == [server._POSIX_WORKER_NICE]
    assert applied == []


@posix_only
def test_worker_off_prime_keeps_nice_19_drop_then_boost(monkeypatch, recorder, applied):
    _set_prime(monkeypatch, False)
    _stub_worker_init(monkeypatch)
    server._worker_process_init()
    assert recorder.index("lower") < recorder.index(("raise-worker", ()))
    assert applied == []


def test_prime_posix_worker_is_false_on_win32(monkeypatch):
    _set_prime(monkeypatch, True)
    monkeypatch.setattr(server.sys, "platform", "win32")
    assert server._prime_posix_worker() is False


def test_accept_boosts_on_and_off_prime(monkeypatch, recorder, applied):
    class _Stop(Exception):
        pass

    def _boom():
        raise _Stop

    monkeypatch.setattr(server, "_engine_clone_root", _boom)
    for prime in (True, False):
        recorder.clear()
        _set_prime(monkeypatch, prime)
        with pytest.raises(_Stop):
            server._run_guarded()
        assert "raise-engine" in recorder
    assert applied == []


@pytest.mark.parametrize("prime", [True, False])
def test_supervisor_main_boosts_on_and_off_prime(monkeypatch, recorder, applied, prime):
    class _Stop(Exception):
        pass

    boosts: list = []
    _set_prime(monkeypatch, prime)
    monkeypatch.setattr(supervisor, "_default_engine_clone", lambda: "root")
    monkeypatch.setattr(supervisor, "engine_root_divergence_message", lambda r: None)
    monkeypatch.setattr(supervisor.idle, "ladder_gone_tombstoned", lambda r: False)
    monkeypatch.setattr(supervisor.breadcrumb, "publish_marker_blocks_boot", lambda d: False)
    monkeypatch.setattr(supervisor.breadcrumb, "svc_dir", lambda r: r)
    monkeypatch.setattr(supervisor, "_elect_supervisor_slot", lambda r: object())
    monkeypatch.setattr(supervisor, "_log_priority_boost", lambda role, held: None)
    monkeypatch.setattr(supervisor, "_raise_worker_priority", lambda n: boosts.append(n) or True)

    def _stop(root):
        raise _Stop

    monkeypatch.setattr(supervisor, "_assert_credential_ready", _stop)
    with pytest.raises(_Stop):
        supervisor.main()
    assert boosts == [supervisor._POSIX_SUPERVISOR_NICE]
    assert applied == []
