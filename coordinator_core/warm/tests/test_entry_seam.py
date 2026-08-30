"""Tests for `coordinator_core.warm.entry_seam` — the per-request state seam
C13 gives entry paths 2 (`cli_entry.run_op_main`) and 3 (`get_op_handler`
re-entry) to converge on.

Purpose: C13 of `docs/plans/2026-08-15-warm-engine-retires-the-per-invocation-
cold-start.md`. Covers `per_request_state` isolation across overlapping
scopes (the AC4 shape: two dispatches must not cross-contaminate declared
writes), `reentrant_dispatch` against a REAL registered op (`ping`, never
invented here), its unknown-op failure, and `cli_entry.run_op_main`'s own
declared-write collection now opening through this seam.

Spec backlink: docs/plans/2026-08-15-warm-engine-retires-the-per-invocation-cold-start.md § C13
"""

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
    # Closed: no collection open outside the block.
    assert active_declarations() is None


def test_per_request_state_accepts_a_preexisting_list():
    into: list = []
    with per_request_state(into, isolated=False) as declared:
        assert declared is into
        declare_write("a.txt")
    assert into == ["a.txt"]


def test_per_request_state_nesting_does_not_cross_contaminate():
    """AC4 shape: an inner per-request scope must not leak into, or be
    visible from, an outer one once it closes — the failure direction C11
    fixed at the dispatch core (a bare rebind instead of Token/reset) for
    exactly this reason.
    """
    with per_request_state(isolated=False) as outer:
        declare_write("outer.txt")
        with per_request_state(isolated=False) as inner:
            declare_write("inner.txt")
            assert inner == ["inner.txt"]
            assert active_declarations() is inner
        # Outer scope's list is restored and untouched by the inner one.
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
    """Regression for the reviewer-found latent trap: a future path-3
    migration onto this seam whose op resolves to an `async def` handler
    must fail loud (TypeError) rather than silently returning an unawaited
    coroutine — see entry_seam.py's docstring negative-spec.
    """
    from coordinator_core import ipc

    async def _fake_async_handler(params, repo_root=None):
        return {"ok": True}

    monkeypatch.setattr(ipc, "get_op_handler", lambda name: _fake_async_handler)

    with pytest.raises(TypeError):
        reentrant_dispatch("fake.async.op", {})


def test_reentrant_dispatch_scopes_declared_writes_per_call():
    """A handler invoked via `reentrant_dispatch` gets its own declared-
    writes scope, isolated from a caller's outer scope — the exact property
    that makes it safe for a `get_op_handler` re-entry call site to adopt
    without leaking its own declarations into (or out of) the op it calls.
    """
    with per_request_state(isolated=False) as outer:
        declare_write("caller.txt")
        reentrant_dispatch("ping", {})
        # ping declares nothing; the outer scope is unaffected either way.
        assert outer == ["caller.txt"]


def test_reentrant_dispatch_inherits_warm_served_from_the_outer_scope(monkeypatch):
    """Regression for the reviewer-found P1: a nested `per_request_state()`
    opened by `reentrant_dispatch` used to leave `warm_served` at its bare
    `False` default regardless of the outer scope, so a handler reached
    through path-3 re-entry from inside a warm-served dispatch would
    resolve `in_warm_served_request()` as cold and degrade to the server
    owner's ambient environment — the exact 2026-08-29 misattribution
    defect (state/bug-backlog/2026-08-29-the-warm-door-s-exe-route-stamps-
    the-ser-47373b19c77e.yaml), reintroduced for the nested call only.
    """
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
    # Unwound outside the block -- reproducing today's env-only behaviour.


def test_per_request_state_with_no_session_id_is_a_no_op(monkeypatch):
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    with per_request_state(isolated=False):
        assert resolve_session_id() == ""


def test_per_request_state_rejects_a_non_uuid_shaped_session_id(monkeypatch):
    """A malformed value crossing the wire must never be trusted -- it is
    treated as "no override" and resolution falls through to the ordinary
    env chain, same fail-safe direction as `commit_trailers.compute_
    missing_trailer_args`'s own `session_id_override` gate."""
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "env-value")
    with per_request_state(session_id="not-a-uuid", isolated=False):
        assert resolve_session_id() == "env-value"


def test_run_op_main_collection_opens_through_the_seam(tmp_path, monkeypatch):
    """`cli_entry.run_op_main` now opens its declared-write collection via
    `entry_seam.per_request_state` rather than calling `session.declared_
    writes.collecting()` directly — assert the seam is actually on the call
    path by having a fake op module call `declare_write` and observing it
    land in the recorded envelope `cli_entry._record` receives.
    """
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


# ---------------------------------------------------------------------------
# `try_warm_guard_dispatch` (C14a) -- the warm-first client primitive, unit
# tested against a monkeypatched `warm.client.try_warm_dispatch`. No live
# server anywhere in this file, and no measurement claim (AC13's <50ms
# number is C14b's, not this primitive's).
# ---------------------------------------------------------------------------


def _patch_try_warm_dispatch(monkeypatch, fn):
    from coordinator_core.warm import client

    monkeypatch.setattr(client, "try_warm_dispatch", fn)


def test_try_warm_guard_dispatch_reports_a_real_hit(monkeypatch):
    """A genuine result envelope is a hit, response returned verbatim."""
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
    """`try_warm_dispatch` returning `None` (warmth disabled, no pipe, busy,
    someone else's pipe, a broken mid-request pipe, a malformed response, or
    read-deadline expiry -- the whole anti-storm table) is an ordinary cold
    fall-through, not a hit."""
    _patch_try_warm_dispatch(monkeypatch, lambda msg: None)

    outcome = try_warm_guard_dispatch("some.guard.op", {})

    assert outcome == WarmGuardOutcome(hit=False, response=None)


def test_try_warm_guard_dispatch_falls_open_on_a_malformed_non_dict_response(monkeypatch):
    """Defense in depth: even if `try_warm_dispatch`'s own contract were
    somehow violated and it returned something other than a dict or None,
    this primitive must still fail open rather than crash the caller."""
    _patch_try_warm_dispatch(monkeypatch, lambda msg: "not a dict")

    outcome = try_warm_guard_dispatch("some.guard.op", {})

    assert outcome == WarmGuardOutcome(hit=False, response=None)


def test_try_warm_guard_dispatch_falls_open_when_try_warm_dispatch_raises(monkeypatch):
    """`warm.client.try_warm_dispatch` is documented never to raise, but this
    primitive does not trust that discipline holding forever -- an
    unanticipated exception must still fail open, not propagate."""

    def _boom(msg):
        raise RuntimeError("unexpected transport failure")

    _patch_try_warm_dispatch(monkeypatch, _boom)

    outcome = try_warm_guard_dispatch("some.guard.op", {})

    assert outcome == WarmGuardOutcome(hit=False, response=None)


def test_try_warm_guard_dispatch_falls_open_when_client_module_is_unimportable(monkeypatch):
    """`warm.client` itself failing to import (e.g. a broken environment) is
    just another fail-open case, not a reason to crash the caller."""
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


# ---------------------------------------------------------------------------
# AC5a closure -- "warm off" and "no door" exercised AS THEMSELVES, not
# collapsed into the stubbed-None `try_warm_dispatch` case every test above
# uses. `try_warm_dispatch` is left unstubbed in both: the real
# `warm.client`/`warm.settings` chain is what has to produce the fall-open
# result here.
# ---------------------------------------------------------------------------


def test_try_warm_guard_dispatch_falls_open_when_warm_is_genuinely_disabled(monkeypatch):
    """"Warm off" as itself: `warm.settings.is_warm_enabled` resolves to
    False via its real registry rung (`registry_get` monkeypatched as a
    module attribute, the established seam -- see `test_warm_settings.py`),
    with `warm.client.try_warm_dispatch` left completely unstubbed. The
    "warm off" branch inside `_try_warm_dispatch_inner` is what actually
    returns `None` here, not a fake.
    """
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
    """"No door" as itself: warmth reads as enabled, but the pipe/socket
    endpoint for a fabricated engine token has no server ever bound to it,
    so `warm.client._open_pipe` raises the real `FileNotFoundError` /
    `ConnectionRefusedError` this primitive must fall open on --
    `try_warm_dispatch` is left unstubbed.

    `breadcrumb.should_spawn` is pinned to False so the FileNotFoundError
    branch's own best-effort respawn (`client._spawn_once`) stays inert --
    that debounce is a separate concern from the "no door" outcome this
    test pins, and a real `spawn_detached()` has no place in this suite.
    """
    from coordinator_core.warm import breadcrumb, client

    monkeypatch.setattr(client, "is_warm_enabled", lambda: True)
    monkeypatch.setattr(client, "engine_token", lambda: "test-entry-seam-no-door-token")
    monkeypatch.setattr(client, "_spawned_this_process", False)
    monkeypatch.setattr(client, "_live_tree_cold", False)
    monkeypatch.setattr(breadcrumb, "should_spawn", lambda engine_root=None, **kw: False)

    outcome = try_warm_guard_dispatch("some.guard.op", {})

    assert outcome == WarmGuardOutcome(hit=False, response=None)
