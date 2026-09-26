
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.warm import engine_root, skew, supervisor
from coordinator_core.warm.entry_seam import (
    WarmGuardOutcome,
    _trigger_listener_boot,
    try_warm_guard_dispatch,
)


def _stamp(tmp_path: Path) -> None:
    skew.write_engine_stamp(tmp_path, "sha-entry-seam-trigger")


def _patch_try_warm_dispatch(monkeypatch, fn):
    from coordinator_core.warm import client

    monkeypatch.setattr(client, "try_warm_dispatch", fn)


def test_trigger_listener_boot_calls_ensure_listener_from_a_stamped_root(tmp_path, monkeypatch):
    _stamp(tmp_path)
    monkeypatch.setattr(engine_root, "current_engine_clone", lambda: tmp_path)

    calls = []
    monkeypatch.setattr(
        supervisor,
        "ensure_listener",
        lambda root=None, **kwargs: calls.append(root) or "http://127.0.0.1:1",
    )

    _trigger_listener_boot()

    assert calls == [tmp_path]


def test_try_warm_guard_dispatch_fires_the_trigger_on_the_cold_path(tmp_path, monkeypatch):
    _stamp(tmp_path)
    monkeypatch.setattr(engine_root, "current_engine_clone", lambda: tmp_path)

    calls = []
    monkeypatch.setattr(
        supervisor,
        "ensure_listener",
        lambda root=None, **kwargs: calls.append(root) or None,
    )
    _patch_try_warm_dispatch(monkeypatch, lambda msg: None)

    try_warm_guard_dispatch("some.guard.op", {})

    assert calls == [tmp_path]


def test_trigger_listener_boot_is_a_no_op_from_an_unstamped_root(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_root, "current_engine_clone", lambda: tmp_path)

    calls = []
    monkeypatch.setattr(
        supervisor,
        "ensure_listener",
        lambda root=None, **kwargs: calls.append(root) or None,
    )

    _trigger_listener_boot()

    assert calls == []


def test_try_warm_guard_dispatch_unchanged_when_ensure_listener_raises(tmp_path, monkeypatch):
    _stamp(tmp_path)
    monkeypatch.setattr(engine_root, "current_engine_clone", lambda: tmp_path)

    def _boom(root=None, **kwargs):
        raise OSError("discovery file unreadable")

    monkeypatch.setattr(supervisor, "ensure_listener", _boom)
    _patch_try_warm_dispatch(monkeypatch, lambda msg: None)

    outcome = try_warm_guard_dispatch("some.guard.op", {})

    assert outcome == WarmGuardOutcome(hit=False, response=None)


def test_try_warm_guard_dispatch_unchanged_when_ensure_listener_raises_on_a_real_hit(tmp_path, monkeypatch):
    _stamp(tmp_path)
    monkeypatch.setattr(engine_root, "current_engine_clone", lambda: tmp_path)

    def _boom(root=None, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(supervisor, "ensure_listener", _boom)

    envelope = {"jsonrpc": "2.0", "id": 1, "result": {"decision": "deny"}}
    _patch_try_warm_dispatch(monkeypatch, lambda msg: envelope)

    outcome = try_warm_guard_dispatch("some.guard.op", {})

    assert outcome == WarmGuardOutcome(hit=True, response=envelope)


def test_trigger_listener_boot_swallows_an_unresolvable_engine_root(monkeypatch):

    def _boom():
        raise RuntimeError("cannot resolve engine root")

    monkeypatch.setattr(engine_root, "current_engine_clone", _boom)

    _trigger_listener_boot()


def test_trigger_listener_boot_swallows_an_unimportable_supervisor_module(tmp_path, monkeypatch):
    _stamp(tmp_path)
    monkeypatch.setattr(engine_root, "current_engine_clone", lambda: tmp_path)

    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "coordinator_core.warm.supervisor":
            raise ImportError("simulated import failure")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    _trigger_listener_boot()
