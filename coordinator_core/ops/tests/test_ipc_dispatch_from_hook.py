"""dispatch_from_hook() -- the single engine-side home for the JSON-RPC envelope
build + asyncio.run(dispatch_message) + result-unwrap body that seven example-doctrine-repo hook
shims currently hand-roll identically.

Spec backlink: cross-repo/archive/2026-07-31-example-doctrine-repo-em-dr116-seam-contents-and-ipc-hook-dispatch.md
               (example-doctrine-repo's DR-118 — the memo's filename says dr116 because example-doctrine-repo renumbered the
               ruling at execute time; DR-116 in the example-doctrine-repo tree is an unrelated record)

Coverage:
  (a) happy path returns the handler's return value.
  (b) _origin_worktree stamped into the envelope when origin_worktree is supplied
      (non-empty string); absent from the envelope when omitted or empty.
  (c) unknown op -> HookDispatchError with code == ipc.METHOD_NOT_FOUND (-32601).
  (d) handler raising -> surfaces as HookDispatchError, not a bare traceback.
  (e) absent "result" key on a success response -> returns {} (the shape this
      module deliberately chose over KeyError).

Registers throwaway test ops directly in ipc._REGISTRY (register_op's own
storage) and removes them in a fixture teardown -- mirrors this suite's existing
direct-registration precedent (ops/tests/test_op_registration.py parametrizes
over ipc._REGISTRY entries already populated by real op modules; this file
instead registers its OWN disposable ops so it does not depend on any real
op's params/behavior).
"""

from __future__ import annotations

import pytest

import coordinator_core.ipc as ipc


_ECHO_OP = "test.hook_dispatch_echo"
_ENVELOPE_CAPTURE_OP = "test.hook_dispatch_envelope_capture"
_RAISING_OP = "test.hook_dispatch_raises"
_NO_RESULT_OP = "test.hook_dispatch_no_result"

_captured_envelopes: list = []


async def _echo_handler(params, repo_root=None):
    return {"echo": params}


async def _envelope_capture_handler(params, repo_root=None):
    # Records nothing about the envelope itself (dispatch_message only ever
    # passes params/repo_root to handlers) -- instead this handler's job is
    # just to exist so dispatch_from_hook's _origin_worktree stamping can be
    # asserted by monkeypatching dispatch_message directly (see the test that
    # needs envelope-shape visibility below).
    return {"ok": True}


async def _raising_handler(params, repo_root=None):
    raise ValueError("boom")


async def _no_result_handler(params, repo_root=None):
    # dispatch_message always sets "result" on success in real life; this
    # handler exists only so a monkeypatched dispatch_message can simulate a
    # response dict missing "result" entirely for test (e).
    return {}


@pytest.fixture
def _register_test_ops():
    ipc.register_op(_ECHO_OP, _echo_handler)
    ipc.register_op(_ENVELOPE_CAPTURE_OP, _envelope_capture_handler)
    ipc.register_op(_RAISING_OP, _raising_handler)
    ipc.register_op(_NO_RESULT_OP, _no_result_handler)
    try:
        yield
    finally:
        for op in (_ECHO_OP, _ENVELOPE_CAPTURE_OP, _RAISING_OP, _NO_RESULT_OP):
            ipc._REGISTRY.pop(op, None)


def test_happy_path_returns_handler_result(_register_test_ops):
    result = ipc.dispatch_from_hook(_ECHO_OP, {"a": 1})
    assert result == {"echo": {"a": 1}}


def test_origin_worktree_stamped_when_supplied(_register_test_ops, monkeypatch):
    captured = {}

    async def _fake_dispatch_message(msg):
        captured["msg"] = msg
        return {"jsonrpc": "2.0", "id": 1, "result": {}}

    monkeypatch.setattr(ipc, "dispatch_message", _fake_dispatch_message)

    ipc.dispatch_from_hook(_ENVELOPE_CAPTURE_OP, {}, origin_worktree="/some/repo")

    assert captured["msg"]["_origin_worktree"] == "/some/repo"


@pytest.mark.parametrize("origin_worktree", [None, ""])
def test_origin_worktree_omitted_when_absent_or_empty(
    _register_test_ops, monkeypatch, origin_worktree
):
    captured = {}

    async def _fake_dispatch_message(msg):
        captured["msg"] = msg
        return {"jsonrpc": "2.0", "id": 1, "result": {}}

    monkeypatch.setattr(ipc, "dispatch_message", _fake_dispatch_message)

    ipc.dispatch_from_hook(_ENVELOPE_CAPTURE_OP, {}, origin_worktree=origin_worktree)

    assert "_origin_worktree" not in captured["msg"], (
        "an absent/empty origin_worktree must be OMITTED from the envelope, not "
        "carried as an empty string -- resolve_request_repo() already treats "
        "empty/absent identically, so carrying '' would be a no-op change but a "
        "deviation from the chosen contract."
    )


def test_unknown_op_raises_hook_dispatch_error_method_not_found():
    with pytest.raises(ipc.HookDispatchError) as exc_info:
        ipc.dispatch_from_hook("test.this_op_does_not_exist_anywhere", {})

    assert exc_info.value.code == ipc.METHOD_NOT_FOUND
    assert "test.this_op_does_not_exist_anywhere" in str(exc_info.value)


def test_handler_exception_surfaces_as_hook_dispatch_error(_register_test_ops):
    with pytest.raises(ipc.HookDispatchError) as exc_info:
        ipc.dispatch_from_hook(_RAISING_OP, {})

    assert exc_info.value.code == ipc.INTERNAL_ERROR
    assert _RAISING_OP in str(exc_info.value)


def test_absent_result_key_returns_empty_dict(_register_test_ops, monkeypatch):
    async def _fake_dispatch_message(msg):
        # Simulate a (hypothetical) success response missing "result" entirely.
        return {"jsonrpc": "2.0", "id": 1}

    monkeypatch.setattr(ipc, "dispatch_message", _fake_dispatch_message)

    result = ipc.dispatch_from_hook(_NO_RESULT_OP, {})
    assert result == {}
