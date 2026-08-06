"""
coordinator_core.tests.test_dispatch_message — Regression net for ipc.dispatch_message.

Tests:
  - dispatch_message: valid method → result dict with echoed id
  - dispatch_message: jsonrpc != "2.0" → code -32600 (INVALID_REQUEST)
  - dispatch_message: params not dict → code -32602 (INVALID_PARAMS)
  - dispatch_message: method missing / empty string → code -32600 (INVALID_REQUEST)
  - dispatch_message: unknown method → code -32601 (METHOD_NOT_FOUND)
  - dispatch_message: handler raises → code -32603 (INTERNAL_ERROR)
  - dispatch_message: handler raises a structurally_wedged-marked exception (e.g.
    ContractPinError) → code -32001 (STRUCTURAL_PIN_ERROR), distinct from a generic
    handler raise, and preserves the exception's own message text
  - dispatch_message: async handler invoked correctly
  - PRECEDENCE: jsonrpc-version checked BEFORE params-type (v1.0 + bad params → -32600)

Handlers use asyncio.run() in sync test functions — no pytest-asyncio dependency
(engine is stdlib-only; same pattern as test_hooks_roundtrip.py).

Spec backlink: docs/plans/2026-07-02-pcore-03-beachhead-coordinator-core.md § C1 / C1b
"""

from __future__ import annotations

import asyncio
import importlib
import json
import pkgutil
from pathlib import Path

import pytest

import coordinator_core.ipc as ipc
from coordinator_core.ipc import (
    PARSE_ERROR,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    INVALID_PARAMS,
    INTERNAL_ERROR,
    STRUCTURAL_PIN_ERROR,
    _ORIGIN_WORKTREE_FIELD,
    _OP_KEY_SCOPE,
    dispatch_message,
    resolve_request_repo,
    resolve_op_repo_key,
    _REGISTRY,
)
from coordinator_core.ops.emit.validate import ContractPinError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio needed."""
    return asyncio.run(coro)


class _RegistryScope:
    """Context manager: install test handlers into _REGISTRY, restore on exit.

    Ensures test-local op registrations do not bleed into the real registry or
    across tests. Saves/restores ONLY the keys it touches.
    """

    def __init__(self, handlers: dict) -> None:
        self._handlers = handlers
        self._saved: dict = {}

    def __enter__(self):
        for name in self._handlers:
            self._saved[name] = _REGISTRY.get(name)
        _REGISTRY.update(self._handlers)
        return self

    def __exit__(self, *_):
        for name, old in self._saved.items():
            if old is None:
                _REGISTRY.pop(name, None)
            else:
                _REGISTRY[name] = old



def _sync_handler(params: dict, ctx=None, repo_root=None) -> dict:
    """Trivial sync handler: echo params back under 'echo' key."""
    return {"echo": params}


async def _async_handler(params: dict, ctx=None, repo_root=None) -> dict:
    """Trivial async handler: echo params back under 'async_echo' key."""
    return {"async_echo": params}


def _raising_handler(params: dict, ctx=None, repo_root=None) -> dict:
    """Handler that always raises RuntimeError — triggers INTERNAL_ERROR path."""
    raise RuntimeError("deliberate test error")


def _raising_structural_handler(params: dict, ctx=None, repo_root=None) -> dict:
    """Handler that raises the REAL ContractPinError — triggers STRUCTURAL_PIN_ERROR.

    Uses the production exception class (not a synthetic marker-only stand-in) so this
    test exercises the actual duck-type contract dispatch_message relies on — the same
    class emit.cadence's real handler raises via envelope.emit -> validate.assert_version_consistency.
    """
    raise ContractPinError("deliberate structural test failure — pin desync")


_TEST_HANDLERS = {
    "test.sync": _sync_handler,
    "test.async": _async_handler,
    "test.raise": _raising_handler,
    "test.raise_structural": _raising_structural_handler,
}

# Sentinel ctx — handlers above never use ctx; None is safe.
_CTX = None


# ---------------------------------------------------------------------------
# dispatch_message — result path
# ---------------------------------------------------------------------------

def test_valid_sync_method_returns_result():
    """Valid request with sync handler → result dict with echoed id and handler output."""
    msg = {"jsonrpc": "2.0", "id": 42, "method": "test.sync", "params": {"x": 1}}
    with _RegistryScope(_TEST_HANDLERS):
        d = _run(dispatch_message(msg))
    assert d["jsonrpc"] == "2.0"
    assert d["id"] == 42
    assert "result" in d
    assert d["result"] == {"echo": {"x": 1}}
    assert "error" not in d


def test_valid_async_method_returns_result():
    """Valid request with async handler → result dict invoked correctly."""
    msg = {"jsonrpc": "2.0", "id": 7, "method": "test.async", "params": {"k": "v"}}
    with _RegistryScope(_TEST_HANDLERS):
        d = _run(dispatch_message(msg))
    assert d["id"] == 7
    assert "result" in d
    assert d["result"] == {"async_echo": {"k": "v"}}


def test_absent_params_defaults_to_empty_dict():
    """When 'params' key is absent, handler receives {} (not None)."""
    received = {}

    def _capture(params, ctx=None, repo_root=None):
        received["params"] = params
        return {}

    msg = {"jsonrpc": "2.0", "id": 1, "method": "test.capture"}
    with _RegistryScope({"test.capture": _capture}):
        d = _run(dispatch_message(msg))
    assert "result" in d
    assert received["params"] == {}


def test_id_echoed_as_none_when_absent():
    """When 'id' is absent from request, result echoes id as None."""
    msg = {"jsonrpc": "2.0", "method": "test.sync", "params": {}}
    with _RegistryScope(_TEST_HANDLERS):
        d = _run(dispatch_message(msg))
    assert d["id"] is None
    assert "result" in d


# ---------------------------------------------------------------------------
# dispatch_message — error paths
# ---------------------------------------------------------------------------

def test_invalid_jsonrpc_version_returns_32600():
    """jsonrpc != '2.0' → INVALID_REQUEST (-32600)."""
    msg = {"jsonrpc": "1.0", "id": 1, "method": "test.sync", "params": {}}
    with _RegistryScope(_TEST_HANDLERS):
        d = _run(dispatch_message(msg))
    assert "error" in d
    assert d["error"]["code"] == INVALID_REQUEST
    assert d["id"] == 1


def test_params_not_dict_returns_32602():
    """params is a list (not dict) → INVALID_PARAMS (-32602)."""
    msg = {"jsonrpc": "2.0", "id": 2, "method": "test.sync", "params": [1, 2, 3]}
    with _RegistryScope(_TEST_HANDLERS):
        d = _run(dispatch_message(msg))
    assert "error" in d
    assert d["error"]["code"] == INVALID_PARAMS
    assert d["id"] == 2


def test_params_integer_returns_32602():
    """params is an integer → INVALID_PARAMS (-32602)."""
    msg = {"jsonrpc": "2.0", "id": 3, "method": "test.sync", "params": 42}
    with _RegistryScope(_TEST_HANDLERS):
        d = _run(dispatch_message(msg))
    assert "error" in d
    assert d["error"]["code"] == INVALID_PARAMS


def test_method_missing_returns_32600():
    """'method' key absent → INVALID_REQUEST (-32600)."""
    msg = {"jsonrpc": "2.0", "id": 4, "params": {}}
    with _RegistryScope(_TEST_HANDLERS):
        d = _run(dispatch_message(msg))
    assert "error" in d
    assert d["error"]["code"] == INVALID_REQUEST


def test_method_empty_string_returns_32600():
    """method == '' → INVALID_REQUEST (-32600)."""
    msg = {"jsonrpc": "2.0", "id": 5, "method": "", "params": {}}
    with _RegistryScope(_TEST_HANDLERS):
        d = _run(dispatch_message(msg))
    assert "error" in d
    assert d["error"]["code"] == INVALID_REQUEST


def test_method_non_string_returns_32600():
    """method is an integer (not a str) → INVALID_REQUEST (-32600)."""
    msg = {"jsonrpc": "2.0", "id": 6, "method": 999, "params": {}}
    with _RegistryScope(_TEST_HANDLERS):
        d = _run(dispatch_message(msg))
    assert "error" in d
    assert d["error"]["code"] == INVALID_REQUEST


def test_unknown_method_returns_32601():
    """Method not in registry → METHOD_NOT_FOUND (-32601)."""
    msg = {"jsonrpc": "2.0", "id": 8, "method": "no.such.op", "params": {}}
    with _RegistryScope(_TEST_HANDLERS):
        d = _run(dispatch_message(msg))
    assert "error" in d
    assert d["error"]["code"] == METHOD_NOT_FOUND
    assert d["id"] == 8


def test_handler_raises_returns_32603():
    """Handler raises Exception → INTERNAL_ERROR (-32603).

    The "transient/soft" branch: a generic, unclassified handler failure — this is the
    behavior that must stay UNCHANGED for any exception that doesn't carry the
    structurally_wedged marker (see test_handler_raises_structural_error_returns_32001
    for the contrasting "contract-class/loud" branch).
    """
    msg = {"jsonrpc": "2.0", "id": 11, "method": "test.raise", "params": {}}
    with _RegistryScope(_TEST_HANDLERS):
        d = _run(dispatch_message(msg))
    assert "error" in d
    assert d["error"]["code"] == INTERNAL_ERROR
    assert d["id"] == 11


def test_handler_raises_structural_error_returns_32001():
    """Handler raises ContractPinError → STRUCTURAL_PIN_ERROR (-32001), not INTERNAL_ERROR.

    The "contract-class/loud" branch, contrasted with test_handler_raises_returns_32603's
    "transient/soft" branch above. A structurally-wedged pin failure (2026-07-22
    example-cockpit-repo-em cross-repo memo — emit.cadence's CONTRACT_VERSION-vs-vendored-
    bundle desync) is NOT a transient/one-off condition: it recurs on every subsequent
    invocation until remediated, so it must be distinguishable from a generic
    INTERNAL_ERROR rather than collapsing into the same bucket a caller might legitimately
    retry or soft-skip on.
    """
    msg = {"jsonrpc": "2.0", "id": 12, "method": "test.raise_structural", "params": {}}
    with _RegistryScope(_TEST_HANDLERS):
        d = _run(dispatch_message(msg))
    assert "error" in d
    assert d["error"]["code"] == STRUCTURAL_PIN_ERROR
    assert d["error"]["code"] != INTERNAL_ERROR
    assert d["id"] == 12
    # The exception's own message (which already states the remediation for a real
    # ContractPinError) is preserved verbatim, unlike the generic class-name-only
    # INTERNAL_ERROR message.
    assert "deliberate structural test failure — pin desync" in d["error"]["message"]


# ---------------------------------------------------------------------------
# PRECEDENCE test — jsonrpc version checked BEFORE params type
# ---------------------------------------------------------------------------

def test_version_checked_before_params():
    """jsonrpc='1.0' with bad params (int) → -32600 (version gate fires first, not params gate).

    Guards against an extraction that reorders the validation sequence: if params were
    checked first the response would be -32602 (INVALID_PARAMS) instead of -32600.
    """
    msg = {"jsonrpc": "1.0", "params": 42, "method": "test.sync", "id": 9}
    with _RegistryScope(_TEST_HANDLERS):
        d = _run(dispatch_message(msg))
    assert "error" in d
    assert d["error"]["code"] == INVALID_REQUEST, (
        f"Expected -32600 (version gate fires before params gate), got {d['error']['code']}"
    )



# ---------------------------------------------------------------------------
# PRECEDENCE test — params type checked BEFORE method type
# ---------------------------------------------------------------------------

def test_params_checked_before_method():
    """params=[1,2] (not dict) with method=999 (not str) → -32602 (params gate fires first).

    Review: code-reviewer — F3: guards the second validation-order adjacency
    (params → method). Validation order is spec-pinned as:
        jsonrpc version → params type → method string → registry lookup → handler invoke
    If this order were swapped, method-integer check would fire first and return -32600
    (INVALID_REQUEST) instead of -32602 (INVALID_PARAMS).
    """
    msg = {"jsonrpc": "2.0", "id": 10, "method": 999, "params": [1, 2]}
    with _RegistryScope(_TEST_HANDLERS):
        d = _run(dispatch_message(msg))
    assert "error" in d
    assert d["error"]["code"] == INVALID_PARAMS, (
        f"Expected -32602 (params gate fires before method gate), got {d['error']['code']}"
    )


# ---------------------------------------------------------------------------
# C1a — _ORIGIN_WORKTREE_FIELD constant and wire-level acceptance
# Spec backlink: docs/plans/2026-07-04-coordinator-core-global-multiplex-migration.md § C1a
# ---------------------------------------------------------------------------

def test_origin_worktree_field_constant_defined():
    """_ORIGIN_WORKTREE_FIELD constant is defined with the correct value.

    Guards the transport seam contract (C1a): the constant must exist and equal
    '_origin_worktree' as specified in the DR/plan.
    """
    from coordinator_core.ipc import _ORIGIN_WORKTREE_FIELD
    assert _ORIGIN_WORKTREE_FIELD == "_origin_worktree", (
        f"_ORIGIN_WORKTREE_FIELD must be '_origin_worktree'; got {_ORIGIN_WORKTREE_FIELD!r}"
    )


def test_dispatch_missing_origin_worktree():
    """A request without _origin_worktree is accepted at the wire level in C1a.

    C1a only DEFINES the _ORIGIN_WORKTREE_FIELD constant — enforcement (fail-loud for
    missing field on working-tree-scoped ops) lands in C1c. At this stage, a message
    without the field must not be rejected by dispatch_message itself.
    """
    msg = {
        "jsonrpc": "2.0",
        "id": 99,
        "method": "test.no_worktree",
        "params": {},
        # deliberately no "_origin_worktree" field
    }

    def _handler(params, ctx=None, repo_root=None):
        return {"ok": True}

    with _RegistryScope({"test.no_worktree": _handler}):
        d = _run(dispatch_message(msg))

    assert "result" in d, (
        "Missing _origin_worktree must not cause dispatch_message to reject the request "
        "(enforcement is C1c; C1a only defines the constant)"
    )
    assert d["result"] == {"ok": True}


# ---------------------------------------------------------------------------
# ctx threading — handler receives the ctx passed to dispatch_message
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# C1b-ii — resolve_request_repo + per-request repo threading
# Spec backlink: docs/plans/2026-07-04-coordinator-core-global-multiplex-migration.md § C1b
# ---------------------------------------------------------------------------

def test_resolve_request_repo_present():
    """resolve_request_repo extracts and resolves _origin_worktree from a message dict."""
    msg = {
        "jsonrpc": "2.0", "id": 1, "method": "test.op",
        _ORIGIN_WORKTREE_FIELD: "/tmp/some/repo",
    }
    result = resolve_request_repo(msg)
    assert result is not None
    assert isinstance(result, Path)
    # Resolved path should be canonical (Path.resolve() applied)
    assert result == Path("/tmp/some/repo").resolve()


def test_resolve_request_repo_absent():
    """resolve_request_repo returns None when _origin_worktree is absent."""
    msg = {"jsonrpc": "2.0", "id": 1, "method": "test.op", "params": {}}
    result = resolve_request_repo(msg)
    assert result is None


def test_resolve_request_repo_empty_string():
    """resolve_request_repo returns None when _origin_worktree is an empty string."""
    msg = {"jsonrpc": "2.0", "id": 1, "method": "test.op", _ORIGIN_WORKTREE_FIELD: ""}
    result = resolve_request_repo(msg)
    assert result is None


def test_resolve_request_repo_non_string():
    """resolve_request_repo returns None when _origin_worktree is not a string."""
    msg = {"jsonrpc": "2.0", "id": 1, "method": "test.op", _ORIGIN_WORKTREE_FIELD: 42}
    result = resolve_request_repo(msg)
    assert result is None



def test_handler_receives_none_repo_when_absent():
    """dispatch_message passes None as repo_root when _origin_worktree is absent."""
    received: dict = {}

    def _repo_capture(params, ctx=None, repo_root=None):
        received["repo_root"] = repo_root
        return {"ok": True}

    msg = {
        "jsonrpc": "2.0",
        "id": 11,
        "method": "test.repo_capture_none",
        "params": {},
        # No _origin_worktree field
    }
    with _RegistryScope({"test.repo_capture_none": _repo_capture}):
        d = _run(dispatch_message(msg))

    assert "result" in d
    assert received.get("repo_root") is None, (
        f"Handler should receive None repo_root when _origin_worktree is absent; "
        f"got {received.get('repo_root')!r}"
    )



# ---------------------------------------------------------------------------
# C1c — AC-1b op-keying table + AC-1c fail-loud
# Spec backlink: docs/plans/2026-07-04-coordinator-core-global-multiplex-migration.md § C1c
# ---------------------------------------------------------------------------

def test_keying_missing_origin_worktree_for_common_dir_op():
    """A common_dir-scoped op with no _origin_worktree → INVALID_PARAMS (-32602).

    AC-1c: fail-loud when a key is required but _origin_worktree is absent.
    hooks.session_heartbeat is "common_dir"-scoped in _OP_KEY_SCOPE.
    """
    msg = {
        "jsonrpc": "2.0",
        "id": 20,
        "method": "hooks.session_heartbeat",
        "params": {},
        # deliberately no "_origin_worktree"
    }

    def _stub(params, ctx=None, repo_root=None):
        return {"ok": True}  # should not be reached

    with _RegistryScope({"hooks.session_heartbeat": _stub}):
        d = _run(dispatch_message(msg))

    assert "error" in d, f"Expected error, got result: {d.get('result')}"
    assert d["error"]["code"] == INVALID_PARAMS, (
        f"Expected INVALID_PARAMS (-32602), got {d['error']['code']}"
    )
    assert "Missing required routing key" in d["error"]["message"], (
        f"Error message must contain 'Missing required routing key'; "
        f"got {d['error']['message']!r}"
    )


def test_emit_op_requires_origin_worktree(tmp_path):
    """artifact.emit is common_dir-scoped and REQUIRES _origin_worktree (2026-07-07 cutover).

    Prior to 2026-07-07, emit ops (artifact.emit, backlog.record, goal.append) were
    "central"-scoped and silently ignored _origin_worktree. The per-repo-emission-cutover
    plan (C3) reclassified them to "common_dir" — each caller MUST supply _origin_worktree
    pointing to a valid git worktree, or dispatch returns INVALID_PARAMS (-32602).

    This test asserts the fail-loud behaviour when _origin_worktree is absent:
    the old "bypassed_key=True" (repo_root=None) assertion is the exact regression we guard.
    Spec: docs/plans/2026-07-07-per-repo-emission-cutover.md § C3 / AC1
    """
    # Without _origin_worktree → should fail loud (common_dir-scoped, key required)
    msg_no_worktree = {
        "jsonrpc": "2.0",
        "id": 21,
        "method": "artifact.emit",
        "params": {},
        # deliberately no "_origin_worktree"
    }

    def _stub(params, ctx=None, repo_root=None):
        return {"repo_root": str(repo_root) if repo_root else None}

    with _RegistryScope({"artifact.emit": _stub}):
        d = _run(dispatch_message(msg_no_worktree))

    assert "error" in d, (
        f"artifact.emit without _origin_worktree must fail loud (common_dir-scoped); "
        f"got result: {d.get('result')}"
    )
    assert d["error"]["code"] == INVALID_PARAMS, (
        f"Missing routing key must return INVALID_PARAMS (-32602); got {d['error']['code']}"
    )

    # With a valid git worktree → dispatch succeeds and handler receives non-None repo_root
    msg_with_worktree = {
        "jsonrpc": "2.0",
        "id": 22,
        "method": "artifact.emit",
        "params": {},
        "_origin_worktree": str(Path(__file__).resolve().parent),
    }

    with _RegistryScope({"artifact.emit": _stub}):
        d2 = _run(dispatch_message(msg_with_worktree))

    assert "result" in d2, (
        f"artifact.emit with _origin_worktree must succeed; got: {d2.get('error')}"
    )
    assert d2["result"]["repo_root"] is not None, (
        "artifact.emit handler must receive a non-None repo_root derived from _origin_worktree"
    )


def test_keying_none_op_ignores_origin_worktree():
    """A none-scoped op with no _origin_worktree dispatches successfully.

    ping is "none"-scoped in _OP_KEY_SCOPE — it accesses no repo state and
    does not require _origin_worktree.
    """
    msg = {
        "jsonrpc": "2.0",
        "id": 22,
        "method": "ping",
        "params": {},
        # deliberately no "_origin_worktree"
    }

    def _stub(params, ctx=None, repo_root=None):
        return {"pong": True, "repo_root_is_none": repo_root is None}

    with _RegistryScope({"ping": _stub}):
        d = _run(dispatch_message(msg))

    assert "result" in d, (
        f"None-scoped op must not produce a routing error; got: {d.get('error')}"
    )
    assert d["result"]["repo_root_is_none"] is True, (
        "None-scoped op handler must receive None as repo_root"
    )




def test_keying_unresolvable_key_fail_loud(tmp_path):
    """common_dir-scoped op with _origin_worktree pointing to a non-git path → INVALID_PARAMS.

    AC-1c: detect-then-fail-loud on unresolvable routing key.  When git_common_dir
    fails (non-git path), dispatch_message returns INVALID_PARAMS (-32602) rather
    than silently picking a default repo or propagating INTERNAL_ERROR.

    Uses hooks.session_heartbeat (common_dir scope) with a real but non-git tmp dir.
    """
    # tmp_path exists but is not inside any git repository — git_common_dir will fail
    non_git_dir = tmp_path / "not-a-git-repo"
    non_git_dir.mkdir()
    msg = {
        "jsonrpc": "2.0",
        "id": 25,
        "method": "hooks.session_heartbeat",
        "params": {},
        _ORIGIN_WORKTREE_FIELD: str(non_git_dir),
    }

    def _stub(params, ctx=None, repo_root=None):
        return {"ok": True}  # must not be reached

    with _RegistryScope({"hooks.session_heartbeat": _stub}):
        d = _run(dispatch_message(msg))

    assert "error" in d, (
        f"Unresolvable routing key must produce an error, not a result: {d.get('result')}"
    )
    assert d["error"]["code"] == INVALID_PARAMS, (
        f"Unresolvable key must return INVALID_PARAMS (-32602), "
        f"not INTERNAL_ERROR or other code; got {d['error']['code']}"
    )
    # Must NOT silently pick a default repo (the error message must be informative)
    assert "routing key" in d["error"]["message"].lower() or \
           "unresolvable" in d["error"]["message"].lower() or \
           "_origin_worktree" in d["error"]["message"], (
        f"Error message must reference the routing key or _origin_worktree; "
        f"got {d['error']['message']!r}"
    )


def test_resolve_op_repo_key_emit_ops_require_worktree():
    """resolve_op_repo_key raises ValueError for emit ops when request_repo is None.

    Prior to 2026-07-07, artifact.emit / backlog.record / goal.append were "central"-scoped
    and resolve_op_repo_key returned None (key bypassed). After the per-repo-emission-cutover
    (C3), they are "common_dir"-scoped and a missing request_repo must fail loud (ValueError),
    which dispatch_message converts to INVALID_PARAMS (-32602).

    A regression here (ops silently returning None again) would re-introduce the hardlocked-
    to-~/.claude bug — emit would produce a snapshot attributed to the wrong repo.
    Spec: docs/plans/2026-07-07-per-repo-emission-cutover.md § C3 / AC1
    """
    import pytest
    for op in ("artifact.emit", "backlog.record", "goal.append"):
        with pytest.raises(ValueError, match="_origin_worktree"):
            resolve_op_repo_key(op, None)


def test_resolve_op_repo_key_none_returns_none():
    """resolve_op_repo_key returns None for none-scoped ops regardless of request_repo."""
    assert resolve_op_repo_key("ping", None) is None
    assert resolve_op_repo_key("hooks.suggest_sonnet_research", None) is None
    assert resolve_op_repo_key("ping", Path("/some/path")) is None


def test_resolve_op_repo_key_show_top_returns_request_repo(tmp_path):
    """resolve_op_repo_key returns the request_repo directly for show_top-scoped ops."""
    result = resolve_op_repo_key("coverage.gate", tmp_path)
    # tmp_path is a real directory; for show_top, we return request_repo directly
    assert result == tmp_path


def test_resolve_op_repo_key_common_dir_missing_raises():
    """resolve_op_repo_key raises ValueError for common_dir-scoped ops with None request_repo."""
    try:
        resolve_op_repo_key("hooks.session_heartbeat", None)
    except ValueError as exc:
        assert "_origin_worktree" in str(exc) or "requires" in str(exc), (
            f"ValueError message must reference _origin_worktree or 'requires'; got {exc!r}"
        )
    else:
        raise AssertionError(
            "resolve_op_repo_key must raise ValueError for common_dir op with None request_repo"
        )


def _is_test_like_module_name(dotted_name: str) -> bool:
    """True for a test/fixture-shaped module name the coverage walk must not treat as an op.

    Matches the house shape for test discovery (a `tests` package component, a
    `test_*.py` leaf, or `conftest.py`) rather than naming individual modules — a
    module that legitimately registers an op never has one of these shapes, so this
    is a structural exclusion, not a skip list.
    """
    parts = dotted_name.split(".")
    leaf = parts[-1]
    return "tests" in parts or leaf.startswith("test_") or leaf == "conftest"


def _import_all_ops_tree_modules() -> list:
    """Recursively import every production module under coordinator_core.ops via a
    real pkgutil.walk_packages, independent of coordinator_core.ops._EAGER_OP_MODULES.

    This is the fix for the plan's § "Registration is four surfaces, not one" surface
    1: a new op module added under coordinator_core/ops/ but never added to
    _EAGER_OP_MODULES previously registered only under whichever import order happened
    to pull it in — which is exactly how a hand-maintained import list here could pass
    without ever importing it at all. Walking the real package tree on disk means a
    module's mere presence, not its membership in any list, is what gets it imported.

    Deliberately does NOT catch-and-skip a module that raises on import: a misbehaving
    module must surface as a loud test failure naming that module, never be added to a
    skip list — a skip list is exactly how the former 16-module hand list regenerated.

    Returns the dotted names actually imported (diagnostic on failure).
    """
    import coordinator_core.ops as _ops_pkg

    imported: list = []
    for module_info in pkgutil.walk_packages(_ops_pkg.__path__, prefix=_ops_pkg.__name__ + "."):
        if _is_test_like_module_name(module_info.name):
            continue
        importlib.import_module(module_info.name)
        imported.append(module_info.name)
    return imported


def _find_unclassified_ops(registered_names, scope_table) -> list:
    """Names present in `registered_names` but absent from `scope_table`, sorted.

    Factored out of the test body so the plant-a-violation self-test
    (test_gate_detects_a_planted_unclassified_op) exercises the exact same
    classification logic the real coverage test below trusts — per DEC-4's mandate
    (extended to C0d) that a gate must be proven to FAIL on a planted violation before
    it is trusted to pass on a clean tree.
    """
    return sorted(name for name in registered_names if name not in scope_table)


def test_gate_detects_a_planted_unclassified_op():
    """Plant a synthetic unclassified op key and assert the coverage check catches it.

    DEC-4's plant-a-violation discipline, extended to C0d (same self-test discipline
    mandated for C0b/C0c): a gate that only ever runs against an already-clean tree
    proves nothing about whether it would catch a real regression. This proves
    _find_unclassified_ops flags a planted violation BEFORE
    test_op_key_scope_table_covers_all_registered_ops is trusted to rely on it against
    the real registry — without this, F1's failure mode (an op silently defaulted to
    "none" scope by omission) recurs on op 65.
    """
    planted_name = "test.planted_unclassified_op_c0d"
    assert planted_name not in _OP_KEY_SCOPE, (
        "planted sentinel name collides with a real _OP_KEY_SCOPE entry — "
        "pick a different sentinel"
    )
    fake_registered = set(_REGISTRY) | {planted_name}
    unclassified = _find_unclassified_ops(fake_registered, _OP_KEY_SCOPE)
    assert planted_name in unclassified, (
        "the coverage check failed to flag a planted unclassified op — it would "
        "silently pass a real op 65 that never made it into _OP_KEY_SCOPE"
    )


def test_op_key_scope_table_covers_all_registered_ops():
    """Every op registered anywhere under coordinator_core.ops — disk-truth via a real
    pkgutil walk, not a hand-maintained import list — carries an explicit _OP_KEY_SCOPE
    verdict.

    Replaces the former hand-maintained 16-module import list: that list only ever
    imported modules someone remembered to add to it, so a new op module under
    coordinator_core/ops/ that nobody added anywhere was never in _REGISTRY when this
    assertion ran, and passed by absence — the same disease DEC-4 diagnoses for the
    no-hardcoded-paths gate ("a gate that only asserts the tree is currently clean
    proves nothing"), live on the exact surface Waves 1-3 add ~64 entries to.

    Two deterministic import passes (neither relies on "whichever import order happens
    to pull a module in"):
      1. coordinator_core.ops._eager_import_all() — the engine's own existing
         eager-import list; also the only path that reaches self-registering modules
         OUTSIDE coordinator_core.ops (coordinator_core.hooks, .frontmatter.schema_cli,
         .orientation.regenerate_cache, .session_ledger, .plugin_health, .goals).
      2. _import_all_ops_tree_modules() — a pkgutil.walk_packages over
         coordinator_core.ops itself, closing the one gap pass 1 cannot: a module that
         exists on disk under coordinator_core/ops/ but was never added to
         _EAGER_OP_MODULES.

    An op absent from _OP_KEY_SCOPE silently defaults to "none" scope (repo_root=None)
    AND is excluded from WORKTREE_SCOPED_OPS — a double fail-open a repo-touching op
    must never hit undetected. See docs/plans/2026-07-22-coordinator-ops-buildout-from-
    fence-inventory.md § "Registration is four surfaces, not one".
    """
    import coordinator_core.ops as _ops_pkg

    _ops_pkg._eager_import_all()
    imported = _import_all_ops_tree_modules()
    assert imported, (
        "pkgutil walk imported zero modules under coordinator_core.ops — "
        "the walk itself is broken, not the tree"
    )

    unclassified = _find_unclassified_ops(_REGISTRY.keys(), _OP_KEY_SCOPE)
    assert unclassified == [], (
        f"Production ops registered but missing from _OP_KEY_SCOPE: {unclassified!r}. "
        f"Add them to coordinator_core.op_scopes._OP_KEY_SCOPE with a justified "
        f"none/common_dir/show_top verdict — never default to \"none\" by omission."
    )





# ---------------------------------------------------------------------------
# C3 — AC-3: request-level fault containment
# Tests: per-request timeout, blocking-handler isolation, BaseException absorption
# Spec backlink: docs/plans/2026-07-04-coordinator-core-global-multiplex-migration.md § C3
# ---------------------------------------------------------------------------

def test_timeout_poison_request():
    """Async handler that stalls returns INTERNAL_ERROR 'timed out'; concurrent request completes.

    AC-3 Gap-1: a runaway/hanging async op must not wedge the serve loop for all
    partitions beyond DISPATCH_TIMEOUT_SECS.  asyncio.wait_for cancels the stalled
    handler's coroutine after the timeout, and the event loop remains live for
    other requests.

    Two requests are dispatched concurrently via asyncio.gather:
    1. "test.slow" — sleeps for 60s (simulated stall, timeout=0.05s fires first)
    2. "test.fast" — returns immediately

    Asserts that "test.slow" returns INTERNAL_ERROR with "timed out" in the message,
    and "test.fast" returns a valid result, proving the event loop was not wedged.
    """
    import time as _time
    from coordinator_core.ipc import DISPATCH_TIMEOUT_SECS

    # Patch DISPATCH_TIMEOUT_SECS for the duration of this test
    import coordinator_core.ipc as _ipc
    orig_timeout = _ipc.DISPATCH_TIMEOUT_SECS
    _ipc.DISPATCH_TIMEOUT_SECS = 0.05  # 50ms — fast enough for the suite, long enough to be real

    async def _async_slow(params, ctx=None, repo_root=None):
        await asyncio.sleep(60)  # stall — timeout fires first
        return {"should_not_reach": True}

    async def _async_fast(params, ctx=None, repo_root=None):
        return {"fast": True}

    async def _run_concurrent():
        msg_slow = {"jsonrpc": "2.0", "id": 1, "method": "test.slow", "params": {}}
        msg_fast = {"jsonrpc": "2.0", "id": 2, "method": "test.fast", "params": {}}
        results = await asyncio.gather(
            dispatch_message(msg_slow),
            dispatch_message(msg_fast),
        )
        return results

    try:
        with _RegistryScope({"test.slow": _async_slow, "test.fast": _async_fast}):
            slow_result, fast_result = _run(_run_concurrent())
    finally:
        _ipc.DISPATCH_TIMEOUT_SECS = orig_timeout

    # Slow handler must have timed out → INTERNAL_ERROR
    assert "error" in slow_result, (
        f"Slow (stalled) handler must return an error; got result: {slow_result.get('result')}"
    )
    assert slow_result["error"]["code"] == INTERNAL_ERROR, (
        f"Expected INTERNAL_ERROR (-32603) for timed-out handler; "
        f"got code {slow_result['error']['code']}"
    )
    assert "timed out" in slow_result["error"]["message"], (
        f"Timeout error message must contain 'timed out'; "
        f"got {slow_result['error']['message']!r}"
    )

    # Fast handler must have completed normally — event loop was not wedged
    assert "result" in fast_result, (
        f"Fast handler must complete while slow handler is stalled; "
        f"got error: {fast_result.get('error')}"
    )
    assert fast_result["result"] == {"fast": True}


def test_blocking_handler_does_not_wedge_loop():
    """Sync handler that ACTUALLY BLOCKS (time.sleep) does not wedge the event loop.

    AC-3 Gap-3 (load-bearing test): a sync handler that calls time.sleep (blocking
    the calling thread, not just the async event loop) must be isolated via
    asyncio.to_thread so the event loop can service concurrent requests while the
    thread sleeps.

    Two requests dispatched concurrently:
    1. "test.blocking" — calls time.sleep(0.5) (BLOCKS its thread)
    2. "test.nonblock" — returns immediately

    If asyncio.to_thread is NOT used, the blocking sleep would stall the event loop
    and "test.nonblock" would not resolve until after 0.5s (or timeout fires).
    With asyncio.to_thread, the event loop is free to dispatch "test.nonblock" while
    the blocking thread sleeps.

    Timeout is set generously (2s) so the blocking handler completes normally (0.5s);
    the assertion is that the non-blocking handler also completes — verifying the loop
    is live, not wedged.
    """
    import time as _time
    import coordinator_core.ipc as _ipc
    orig_timeout = _ipc.DISPATCH_TIMEOUT_SECS
    _ipc.DISPATCH_TIMEOUT_SECS = 2.0  # generous — blocking handler finishes in 0.5s

    def _sync_blocking(params, ctx=None, repo_root=None):
        _time.sleep(0.3)  # BLOCKS — actual thread sleep, not asyncio
        return {"blocked": True}

    def _sync_fast(params, ctx=None, repo_root=None):
        return {"nonblock": True}

    async def _run_concurrent():
        msg_block = {"jsonrpc": "2.0", "id": 1, "method": "test.blocking", "params": {}}
        msg_fast = {"jsonrpc": "2.0", "id": 2, "method": "test.nonblock", "params": {}}
        return await asyncio.gather(
            dispatch_message(msg_block),
            dispatch_message(msg_fast),
        )

    try:
        with _RegistryScope({"test.blocking": _sync_blocking, "test.nonblock": _sync_fast}):
            block_result, fast_result = _run(_run_concurrent())
    finally:
        _ipc.DISPATCH_TIMEOUT_SECS = orig_timeout

    # Both must complete — the non-blocking request resolves concurrently
    assert "result" in block_result, (
        f"Blocking handler must complete and return a result; got error: {block_result.get('error')}"
    )
    assert block_result["result"] == {"blocked": True}

    assert "result" in fast_result, (
        f"Non-blocking handler must complete while blocking handler runs in a thread; "
        f"got error: {fast_result.get('error')}"
    )
    assert fast_result["result"] == {"nonblock": True}


def test_base_exception_absorbed():
    """Handler that raises SystemExit returns INTERNAL_ERROR — process does not exit.

    AC-3 Gap-2: BaseException subclasses (SystemExit, KeyboardInterrupt, MemoryError)
    escaping an op handler must be caught by dispatch_message and converted to an
    INTERNAL_ERROR envelope.  They must NOT propagate to the connection handler's
    serve loop or terminate the process.

    Uses SystemExit(1) which is a BaseException but not Exception.
    """
    def _system_exit_handler(params, ctx=None, repo_root=None):
        raise SystemExit(1)

    msg = {"jsonrpc": "2.0", "id": 30, "method": "test.base_exc", "params": {}}
    with _RegistryScope({"test.base_exc": _system_exit_handler}):
        d = _run(dispatch_message(msg))

    assert "error" in d, (
        f"SystemExit from handler must produce INTERNAL_ERROR response, not a result: {d}"
    )
    assert d["error"]["code"] == INTERNAL_ERROR, (
        f"Expected INTERNAL_ERROR (-32603) for SystemExit handler; "
        f"got code {d['error']['code']}"
    )
    # The error message should reference the exception type, not be a bare str(SystemExit)
    assert "SystemExit" in d["error"]["message"] or "Internal error" in d["error"]["message"], (
        f"Error message should reference the exception class; "
        f"got {d['error']['message']!r}"
    )


# ---------------------------------------------------------------------------
# Per-op timeout overrides — ceremony.* rows retired by DEC-2 of
# docs/plans/2026-07-22-wsc-tail-sub-2s-invoke-budget.md (a dispatch timeout is
# a runaway guard, not a performance budget; the <2s ruling is a KPI test).
# Historical: state/improvement-queue/2026-07-13-ceremony-wsc-commit-reliably-times-out-o-62330efd3dd4.yaml
# ---------------------------------------------------------------------------

def test_timeout_for_resolves_per_op_override():
    """_timeout_for falls to the global runaway guard for unlisted ops — including the retired ceremony.* rows."""
    assert ipc._timeout_for("ceremony.wsc_commit") == ipc.DISPATCH_TIMEOUT_SECS
    assert ipc._timeout_for("ceremony.wsc_resolve") == ipc.DISPATCH_TIMEOUT_SECS
    assert ipc._timeout_for("ceremony.wsc_tail") == ipc.DISPATCH_TIMEOUT_SECS
    assert ipc._timeout_for("ping") == ipc.DISPATCH_TIMEOUT_SECS
    assert ipc._timeout_for("test.nonesuch") == ipc.DISPATCH_TIMEOUT_SECS


def test_op_timeout_overrides_public_proxy_contents_and_immutability():
    """OP_TIMEOUT_OVERRIDES (public parity surface) mirrors _OP_TIMEOUT_OVERRIDES and is read-only.

    Review: code-reviewer F4 — the public export shipped with zero direct test
    coverage of its own contents or immutability, unlike OP_KEY_SCOPE's coverage test.
    DEC-2 emptied the table (ceremony.* rows deleted); the proxy must reflect that.
    """
    assert dict(ipc.OP_TIMEOUT_OVERRIDES) == dict(ipc._OP_TIMEOUT_OVERRIDES)
    assert "ceremony.wsc_tail" not in ipc.OP_TIMEOUT_OVERRIDES
    with pytest.raises(TypeError):
        ipc.OP_TIMEOUT_OVERRIDES["test.new"] = 1.0


def test_per_op_override_enforced_in_dispatch():
    """A per-op override (not the global cap) is what's enforced by dispatch_message.

    Registers a slow async op whose method is temporarily added to
    _OP_TIMEOUT_OVERRIDES with a tiny value (0.05s), while the global
    DISPATCH_TIMEOUT_SECS is patched LARGE (60s). Asserts the op times out at
    the tiny per-op value (proving the override, not the global, was
    enforced), and the reported cap in the error message matches the override.
    """
    orig_global = ipc.DISPATCH_TIMEOUT_SECS
    override_present = "test.slowoverride" in ipc._OP_TIMEOUT_OVERRIDES
    orig_override = ipc._OP_TIMEOUT_OVERRIDES.get("test.slowoverride")

    ipc.DISPATCH_TIMEOUT_SECS = 60.0
    ipc._OP_TIMEOUT_OVERRIDES["test.slowoverride"] = 0.05

    async def _async_slow(params, ctx=None, repo_root=None):
        await asyncio.sleep(60)
        return {"should_not_reach": True}

    try:
        msg = {"jsonrpc": "2.0", "id": 40, "method": "test.slowoverride", "params": {}}
        with _RegistryScope({"test.slowoverride": _async_slow}):
            d = _run(dispatch_message(msg))
    finally:
        ipc.DISPATCH_TIMEOUT_SECS = orig_global
        if override_present:
            ipc._OP_TIMEOUT_OVERRIDES["test.slowoverride"] = orig_override
        else:
            ipc._OP_TIMEOUT_OVERRIDES.pop("test.slowoverride", None)

    assert "error" in d, (
        f"Op with tiny per-op override must time out; got result: {d.get('result')}"
    )
    assert d["error"]["code"] == INTERNAL_ERROR
    assert "timed out" in d["error"]["message"]
    assert "0.05" in d["error"]["message"], (
        f"Timeout error message must report the per-op override value (0.05), "
        f"not the (large) global cap; got {d['error']['message']!r}"
    )


def test_near_miss_timeout_env_warns(caplog):
    """A COORDINATOR_*TIMEOUT*-shaped env var that isn't the real knob triggers a warning hint."""
    import logging as _logging

    with caplog.at_level(_logging.WARNING, logger="coordinator_core.ipc"):
        ipc._warn_on_near_miss_timeout_env({"COORDINATOR_CORE_OP_TIMEOUT": "120"})

    assert any(
        "COORDINATOR_DISPATCH_TIMEOUT_SECS" in rec.message
        for rec in caplog.records
    ), f"Expected a near-miss warning mentioning the real knob; got: {[r.message for r in caplog.records]}"

    caplog.clear()
    with caplog.at_level(_logging.WARNING, logger="coordinator_core.ipc"):
        ipc._warn_on_near_miss_timeout_env(
            {"CC_INVOKE_TIMEOUT_SECS": "120", "COORDINATOR_DISPATCH_TIMEOUT_SECS": "30"}
        )

    assert not caplog.records, (
        f"Recognized knob and client-side var (no COORDINATOR substring) must not warn; "
        f"got: {[r.message for r in caplog.records]}"
    )

    # Review: code-reviewer F1 — a legitimate future COORDINATOR_* var that merely
    # mentions "timeout" in passing (not shaped like the real knob) must NOT fire.
    # A bare substring test ("COORDINATOR" in key and "TIMEOUT" in key) would have
    # nagged on this; the narrowed suffix-shaped match must not.
    caplog.clear()
    with caplog.at_level(_logging.WARNING, logger="coordinator_core.ipc"):
        ipc._warn_on_near_miss_timeout_env(
            {"COORDINATOR_SUBAGENT_TIMEOUT_LOG_PATH": "/tmp/x.log"}
        )

    assert not caplog.records, (
        f"Legitimate non-timeout-knob var sharing both substrings must not warn; "
        f"got: {[r.message for r in caplog.records]}"
    )


