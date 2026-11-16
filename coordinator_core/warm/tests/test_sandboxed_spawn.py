"""Tests for `coordinator_core.warm.sandboxed_spawn`: probe delegation and the warm forward outcomes."""

from __future__ import annotations

import pytest

from coordinator_core.warm import client, sandboxed_spawn as ss


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    spawned = []
    monkeypatch.setattr(client, "_spawn_once", lambda: spawned.append(1))
    monkeypatch.setattr(client, "is_warm_enabled", lambda: True)
    monkeypatch.setattr(client, "_cold_reason", None)
    return spawned


def _dispatch(monkeypatch, reply, seen=None):
    def fake(msg, **kw):
        if seen is not None:
            seen.append(msg)
        return reply

    monkeypatch.setattr(client, "try_warm_dispatch_no_spawn", fake)


@pytest.mark.parametrize("restricted", [True, False])
def test_caller_is_sandboxed_mirrors_probe(monkeypatch, restricted):
    monkeypatch.setattr(ss, "_process_token_is_restricted", lambda: restricted)
    assert ss.caller_is_sandboxed() is restricted


def test_served_result_returned(monkeypatch):
    seen = []
    _dispatch(monkeypatch, {"jsonrpc": "2.0", "id": 1, "result": {"run_id": "r1"}}, seen)
    assert ss.forward_spawn_to_warm("workflow.fire", {"a": 1}, "repo-token") == {"run_id": "r1"}
    assert seen[0]["method"] == "workflow.fire"
    assert seen[0]["params"] == {"a": 1}
    assert seen[0]["_origin_worktree"] == "repo-token"


def test_miss_refused_names_sandbox_and_reason_and_never_spawns_server(monkeypatch, _isolate):
    def no_server(msg, read_deadline_secs=None, hook_path=False):
        client._spawn_wanted = True

    monkeypatch.setattr(client, "_try_warm_dispatch_inner", no_server)
    monkeypatch.setattr(client, "_record_cold_fallback", lambda op=None: None)
    with pytest.raises(ss.SandboxedSpawnRefused) as exc:
        ss.forward_spawn_to_warm("workflow.fire", {}, "r")
    text = str(exc.value)
    assert "sandbox" in text and "no_server" in text and "publish/restart the warm engine" in text
    assert _isolate == []


def test_dispatching_door_spawns_on_the_same_miss(monkeypatch, _isolate):
    def no_server(msg, read_deadline_secs=None, hook_path=False):
        client._spawn_wanted = True

    monkeypatch.setattr(client, "_try_warm_dispatch_inner", no_server)
    monkeypatch.setattr(client, "_record_cold_fallback", lambda op=None: None)
    assert client.try_warm_dispatch({"method": "x"}) is None
    assert _isolate == [1]


def test_miss_prefers_permanent_cold_reason(monkeypatch):
    monkeypatch.setattr(client, "_cold_reason", "pipe open denied (winerror 5)")
    _dispatch(monkeypatch, None)
    with pytest.raises(ss.SandboxedSpawnRefused, match="winerror 5"):
        ss.forward_spawn_to_warm("workflow.fire", {}, "r")


def test_miss_with_warm_disabled_says_so(monkeypatch):
    monkeypatch.setattr(client, "is_warm_enabled", lambda: False)
    _dispatch(monkeypatch, None)
    with pytest.raises(ss.SandboxedSpawnRefused, match="warm_disabled"):
        ss.forward_spawn_to_warm("workflow.fire", {}, "r")


def test_error_envelope_raised_as_that_error_not_refusal(monkeypatch):
    err = {"code": -32602, "message": "bad params"}
    _dispatch(monkeypatch, {"jsonrpc": "2.0", "id": 1, "error": err})
    with pytest.raises(ss.WarmServerError) as exc:
        ss.forward_spawn_to_warm("workflow.fire", {}, "r")
    assert exc.value.error == err
    assert not isinstance(exc.value, ss.SandboxedSpawnRefused)


def test_indeterminate_names_run_id_and_is_not_a_refusal(monkeypatch):
    err = {"code": client.WARM_DISPATCH_INDETERMINATE, "message": "unanswered", "data": {"dispatch_key": "dk1"}}
    _dispatch(monkeypatch, {"jsonrpc": "2.0", "id": 1, "error": err})
    with pytest.raises(ss.SpawnIndeterminate) as exc:
        ss.forward_spawn_to_warm("workflow.fire", {"run_id": "run-9"}, "r")
    assert exc.value.run_id == "run-9"
    assert "run-9" in str(exc.value) and "reconcile" in str(exc.value)
    assert not isinstance(exc.value, ss.SandboxedSpawnRefused)


def test_indeterminate_without_run_id_falls_back_to_dispatch_key(monkeypatch):
    err = {"code": client.WARM_DISPATCH_INDETERMINATE, "message": "m", "data": {"dispatch_key": "dk1"}}
    _dispatch(monkeypatch, {"jsonrpc": "2.0", "id": 1, "error": err})
    with pytest.raises(ss.SpawnIndeterminate) as exc:
        ss.forward_spawn_to_warm("workflow.fire", {}, "r")
    assert exc.value.run_id == "dk1"


def test_probe_is_a_bool_and_false_off_windows(monkeypatch):
    assert isinstance(ss._process_token_is_restricted(), bool)
    monkeypatch.setattr(ss.sys, "platform", "linux")
    assert ss._process_token_is_restricted() is False
