
from __future__ import annotations

import pytest

from coordinator_core.session.core import resolve_session_id
from coordinator_core.session.declared_writes import active_declarations, declare_write
from coordinator_core.warm.entry_seam import (
    METHOD_NOT_FOUND,
    WarmGuardOutcome,
    per_request_state,
    reentrant_dispatch,
    try_warm_guard_dispatch,
)


def test_per_request_state_yields_the_collecting_list():
    with per_request_state(isolated=False) as declared:
        assert declared == []
        declare_write("some/path.txt")
        assert declared == ["some/path.txt"]
    assert active_declarations() is None


def test_per_request_state_accepts_a_preexisting_list():
    into: list = []
    with per_request_state(into, isolated=False) as declared:
        assert declared is into
        declare_write("a.txt")
    assert into == ["a.txt"]


def test_per_request_state_nesting_does_not_cross_contaminate():
    with per_request_state(isolated=False) as outer:
        declare_write("outer.txt")
        with per_request_state(isolated=False) as inner:
            declare_write("inner.txt")
            assert inner == ["inner.txt"]
            assert active_declarations() is inner
        assert outer == ["outer.txt"]
        assert active_declarations() is outer
        declare_write("outer-again.txt")
        assert outer == ["outer.txt", "outer-again.txt"]


def test_reentrant_dispatch_invokes_a_real_registered_op():
    result = reentrant_dispatch("ping", {})
    assert result.get("ok") is True
    assert "ts" in result


def test_reentrant_dispatch_unknown_op_raises_lookup_error():
    with pytest.raises(LookupError):
        reentrant_dispatch("this.op.does.not.exist", {})


def test_reentrant_dispatch_async_handler_raises_type_error_instead_of_silently_dropping(monkeypatch):
    from coordinator_core import ipc

    async def _fake_async_handler(params, repo_root=None):
        return {"ok": True}

    monkeypatch.setattr(ipc, "get_op_handler", lambda name: _fake_async_handler)

    with pytest.raises(TypeError):
        reentrant_dispatch("fake.async.op", {})


def test_reentrant_dispatch_scopes_declared_writes_per_call():
    with per_request_state(isolated=False) as outer:
        declare_write("caller.txt")
        reentrant_dispatch("ping", {})
        assert outer == ["caller.txt"]


def test_reentrant_dispatch_inherits_warm_served_from_the_outer_scope(monkeypatch):
    from coordinator_core import ipc
    from coordinator_core.session.core import in_warm_served_request

    seen = {}

    def _probe(params, repo_root=None):
        seen["warm_served"] = in_warm_served_request()
        return {"ok": True}

    monkeypatch.setattr(ipc, "get_op_handler", lambda name: _probe)

    with per_request_state(warm_served=True, isolated=False):
        reentrant_dispatch("probe.warm_served", {})

    assert seen["warm_served"] is True, (
        "reentrant_dispatch must inherit the outer scope's warm_served flag, "
        "not re-default it to False"
    )


def test_per_request_state_binds_the_given_session_id():
    with per_request_state(session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", isolated=False):
        assert resolve_session_id() == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def test_per_request_state_with_no_session_id_is_a_no_op(monkeypatch):
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    with per_request_state(isolated=False):
        assert resolve_session_id() == ""


def test_per_request_state_rejects_a_non_uuid_shaped_session_id(monkeypatch):
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "env-value")
    with per_request_state(session_id="not-a-uuid", isolated=False):
        assert resolve_session_id() == "env-value"


def test_run_op_main_collection_opens_through_the_seam(tmp_path, monkeypatch):
    from coordinator_core import cli_entry

    recorded: list = []
    monkeypatch.setattr(cli_entry, "_record", lambda declared, cwd: recorded.append(list(declared)))

    module = type("FakeOpModule", (), {})()

    def _main(argv):
        declare_write("written.txt")
        return 0

    module.main = _main
    import sys

    monkeypatch.setitem(sys.modules, "fake_entry_seam_test_op_module", module)

    code = cli_entry.run_op_main("fake_entry_seam_test_op_module", [], cwd=str(tmp_path))

    assert code == 0
    assert recorded == [["written.txt"]]
    assert active_declarations() is None


def _patch_try_warm_dispatch(monkeypatch, fn):
    from coordinator_core.warm import client

    monkeypatch.setattr(client, "try_warm_dispatch", fn)


def test_try_warm_guard_dispatch_reports_a_real_hit(monkeypatch):
    envelope = {"jsonrpc": "2.0", "id": 1, "result": {"verdict": "allow"}}
    _patch_try_warm_dispatch(monkeypatch, lambda msg: envelope)

    outcome = try_warm_guard_dispatch("some.guard.op", {"tool_input": {}})

    assert outcome == WarmGuardOutcome(hit=True, response=envelope)


def test_try_warm_guard_dispatch_treats_a_real_op_error_as_a_hit(monkeypatch):
    """An op-computed error (any code OTHER than METHOD_NOT_FOUND) is still
    a genuine warm hit -- the server answered the question, it just answered
    with a refusal. Only METHOD_NOT_FOUND is special-cased."""
    envelope = {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32000, "message": "refused"},
    }
    _patch_try_warm_dispatch(monkeypatch, lambda msg: envelope)

    outcome = try_warm_guard_dispatch("some.guard.op", {})

    assert outcome == WarmGuardOutcome(hit=True, response=envelope)


def test_try_warm_guard_dispatch_treats_method_not_found_as_cold_fallthrough(monkeypatch):
    """THE TRAP THAT BLOCKED C14: a well-formed METHOD_NOT_FOUND error
    envelope -- exactly what dispatching an unregistered op name produces --
    must never be mistaken for a guard verdict. This is the pin."""
    envelope = {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": METHOD_NOT_FOUND, "message": "no such op"},
    }
    _patch_try_warm_dispatch(monkeypatch, lambda msg: envelope)

    outcome = try_warm_guard_dispatch("this.op.does.not.exist", {})

    assert outcome == WarmGuardOutcome(hit=False, response=None)


def test_try_warm_guard_dispatch_falls_open_on_none(monkeypatch):
    _patch_try_warm_dispatch(monkeypatch, lambda msg: None)

    outcome = try_warm_guard_dispatch("some.guard.op", {})

    assert outcome == WarmGuardOutcome(hit=False, response=None)


def test_try_warm_guard_dispatch_falls_open_on_a_malformed_non_dict_response(monkeypatch):
    _patch_try_warm_dispatch(monkeypatch, lambda msg: "not a dict")

    outcome = try_warm_guard_dispatch("some.guard.op", {})

    assert outcome == WarmGuardOutcome(hit=False, response=None)


def test_try_warm_guard_dispatch_falls_open_when_try_warm_dispatch_raises(monkeypatch):

    def _boom(msg):
        raise RuntimeError("unexpected transport failure")

    _patch_try_warm_dispatch(monkeypatch, _boom)

    outcome = try_warm_guard_dispatch("some.guard.op", {})

    assert outcome == WarmGuardOutcome(hit=False, response=None)


def test_try_warm_guard_dispatch_falls_open_when_client_module_is_unimportable(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "coordinator_core.warm.client":
            raise ImportError("simulated import failure")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    outcome = try_warm_guard_dispatch("some.guard.op", {})

    assert outcome == WarmGuardOutcome(hit=False, response=None)


def test_try_warm_guard_dispatch_sends_a_well_formed_jsonrpc_request(monkeypatch):
    """The outgoing message is a proper JSON-RPC 2.0 request envelope
    carrying the caller's op name and params verbatim."""
    captured: list = []

    def _capture(msg):
        captured.append(msg)
        return None

    _patch_try_warm_dispatch(monkeypatch, _capture)

    try_warm_guard_dispatch("some.guard.op", {"tool_input": {"command": "ls"}}, request_id="abc")

    assert len(captured) == 1
    sent = captured[0]
    assert sent["jsonrpc"] == "2.0"
    assert sent["id"] == "abc"
    assert sent["method"] == "some.guard.op"
    assert sent["params"] == {"tool_input": {"command": "ls"}}


def test_try_warm_guard_dispatch_defaults_params_to_empty_dict(monkeypatch):
    captured: list = []
    _patch_try_warm_dispatch(monkeypatch, lambda msg: captured.append(msg) or None)

    try_warm_guard_dispatch("some.guard.op")

    assert captured[0]["params"] == {}


# AC5a closure -- "warm off" and "no door" exercised AS THEMSELVES, not


def test_try_warm_guard_dispatch_falls_open_when_warm_is_genuinely_disabled(monkeypatch):
    from coordinator_core.warm import settings

    monkeypatch.delenv(settings.ENV_VAR, raising=False)
    monkeypatch.setattr(settings, "registry_get", lambda key: None)
    settings._reset_for_test()
    try:
        outcome = try_warm_guard_dispatch("some.guard.op", {})
    finally:
        settings._reset_for_test()

    assert outcome == WarmGuardOutcome(hit=False, response=None)


def test_try_warm_guard_dispatch_falls_open_when_the_door_is_absent(monkeypatch):
    from coordinator_core.warm import breadcrumb, client

    monkeypatch.setattr(client, "is_warm_enabled", lambda: True)
    monkeypatch.setattr(client, "engine_token", lambda: "test-entry-seam-no-door-token")
    monkeypatch.setattr(client, "_spawned_this_process", False)
    monkeypatch.setattr(client, "_live_tree_cold", False)
    monkeypatch.setattr(breadcrumb, "should_spawn", lambda engine_root=None, **kw: False)

    outcome = try_warm_guard_dispatch("some.guard.op", {})

    assert outcome == WarmGuardOutcome(hit=False, response=None)
