"""`supervisor`'s `/hook` seam serves a real guard verdict, never a default allow.

Pins the three facts the plan's C1 names:

  AC1 -- a `PreToolUse` event the guard DENIES must come back denied. Before the edit,
  `_Handler.do_POST` reads the body and discards it, answering an unconditional "allow" --
  this file's first test asserts against a live handler, so it fails against that code
  exactly as the plan requires, and passes once the handler actually dispatches through
  `warm.server._serve_line`.

  AC2 -- the boundary-deletion case `hook_http`'s own module docstring (obligation 2)
  warns about: an override present only in the SERVER's own `os.environ`, absent from the
  posted event, must not reach the guard's verdict. `bash_guards/tests/
  test_override_is_caller_keyed.py` pins the reader half in-process; nothing pins the
  writer over this transport, and a handler that forwards `os.environ` instead of the
  posted event's `env` would pass every other test here while deleting the boundary.

  AC1's forward-looking half -- AN ERROR ENVELOPE IS NEVER A VERDICT. `warm_guard.evaluate`
  (`GUARD_OP_NAME`) IS registered now (`coordinator_core/ops/warm_guard_evaluate.py`,
  landed: state/handoffs/2026-08-23-the-warm-guard-op-gets-registered.md) -- see
  `coordinator_core/ops/tests/test_warm_guard_evaluate.py` for end-to-end coverage of the
  real registered handler. Every test in THIS file still injects a fake `dispatch`, so
  `_method_not_found_dispatch` below no longer reproduces today's live default; it pins a
  regression instead -- if the registration is ever removed or the method name drifts,
  `_serve_line` falls back to exactly this METHOD_NOT_FOUND envelope, and
  `hook_http.interpret_result`'s "not a verdict" discrimination (its own contract tests
  cover it directly) must still turn that into a loud refusal over a real socket, never a
  silent allow. A future edit that "simplifies" the error path back into an allow would
  pass every other test in this file.

All tests bind a real `ThreadingHTTPServer` around `supervisor._make_handler`, mirroring
`tests/test_http_listener.py`'s own harness -- the handler is driven exactly as a fired
hook would drive it, over a real loopback socket, not by calling `do_POST` in isolation.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Optional

from coordinator_core.warm import cookie, skew, supervisor


#: it without every call site threading it. The listener now REQUIRES the
_BOUND_TOKEN: Optional[str] = None


def _post(
    port: int,
    event: dict,
    timeout: float = 5.0,
    path: Optional[str] = None,
    token: Optional[str] = None,
):
    body = json.dumps(event).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    presented = token if token is not None else _BOUND_TOKEN
    if presented is not None:
        headers[cookie.COOKIE_HEADER] = presented
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (port, path or supervisor.HOOK_PATH),
        data=body,
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read())


def _bind_handler(tmp_path: Path, *, dispatch):
    from http.server import ThreadingHTTPServer

    skew.write_engine_stamp(tmp_path, "sha-test")
    root = tmp_path
    global _BOUND_TOKEN
    _BOUND_TOKEN = cookie.ensure(root)
    version_state = skew.ServerVersionState(root)
    ctx = supervisor._ServerContext(
        httpd=None,
        engine_root=root,
        version_state=version_state,
        dispatch=dispatch,
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), supervisor._make_handler(ctx))
    ctx.httpd = httpd
    port = httpd.server_address[1]
    import threading

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, port


def _deny_dispatch(msg, *, caller=None, isolated=False):
    return {
        "jsonrpc": "2.0",
        "id": msg.get("id"),
        "result": {
            "permissionDecision": "deny",
            "permissionDecisionReason": "rm -rf outside the repo root",
        },
    }


def test_a_denied_event_comes_back_denied(tmp_path: Path):
    httpd, port = _bind_handler(tmp_path, dispatch=_deny_dispatch)
    try:
        status, body = _post(port, {"hook_event_name": "PreToolUse", "tool_name": "Bash"})
    finally:
        httpd.shutdown()

    assert status == 200
    hso = body["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "rm -rf" in hso["permissionDecisionReason"]


def test_server_environ_override_does_not_reach_the_forwarded_verdict(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_ALLOW_RM", "1")
    seen = {}

    def _capturing_dispatch(msg, *, caller=None, isolated=False):
        seen["payload"] = msg.get("params", {}).get("payload")
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {}}

    httpd, port = _bind_handler(tmp_path, dispatch=_capturing_dispatch)
    try:
        _post(port, {"hook_event_name": "PreToolUse", "tool_name": "Bash"})
    finally:
        httpd.shutdown()

    assert seen["payload"]["env"] == {}


def _method_not_found_dispatch(msg, *, caller=None, isolated=False):
    """The LIVE response shape today: `GUARD_OP_NAME` has no registered handler, so
    `_run_dispatch` (via `coordinator_core.ipc.dispatch_message`) answers exactly this --
    a well-formed JSON-RPC error envelope, code -32601. Reproduced by hand here rather than
    reached through the real registry, so this test does not depend on the registry's
    current contents remaining empty."""
    return {
        "jsonrpc": "2.0",
        "id": msg.get("id"),
        "error": {"code": -32601, "message": "method not found: %s" % msg.get("method")},
    }


def test_method_not_found_never_reads_as_an_allow(tmp_path: Path):
    httpd, port = _bind_handler(tmp_path, dispatch=_method_not_found_dispatch)
    try:
        status, body = _post(port, {"hook_event_name": "PreToolUse", "tool_name": "Bash"})
    finally:
        httpd.shutdown()

    assert status == 200
    hso = body["hookSpecificOutput"]
    assert "permissionDecision" not in hso
    assert body["suppressOutput"] is False
    assert "did not run" in body["hookSpecificOutput"]["additionalContext"]
    assert "-32601" in body["systemMessage"]


def _no_result_dispatch(msg, *, caller=None, isolated=False):
    return {"jsonrpc": "2.0", "id": msg.get("id"), "result": "ok"}


def test_non_object_result_never_reads_as_an_allow(tmp_path: Path):
    httpd, port = _bind_handler(tmp_path, dispatch=_no_result_dispatch)
    try:
        status, body = _post(port, {"hook_event_name": "PreToolUse", "tool_name": "Bash"})
    finally:
        httpd.shutdown()

    assert status == 200
    hso = body["hookSpecificOutput"]
    assert "permissionDecision" not in hso
    assert "did not run" in body["hookSpecificOutput"]["additionalContext"]


def _echoing_dispatch(msg, *, caller=None, isolated=False):
    return {
        "jsonrpc": "2.0",
        "id": msg.get("id"),
        "result": {
            "additionalContext": "routed to %s" % msg.get("method"),
            "systemMessage": "op %s ran" % msg.get("method"),
        },
    }


def test_the_url_chooses_the_op_end_to_end(tmp_path: Path):
    httpd, port = _bind_handler(tmp_path, dispatch=_echoing_dispatch)
    try:
        _, boot = _post(port, {"hook_event_name": "SessionStart", "source": "startup"}, path="/hook/session.boot_sweep")
        _, track = _post(port, {"hook_event_name": "SessionStart", "source": "startup"}, path="/hook/hooks.track_touched_files")
    finally:
        httpd.shutdown()
    assert boot["hookSpecificOutput"]["additionalContext"] == "routed to session.boot_sweep"
    assert track["hookSpecificOutput"]["additionalContext"] == "routed to hooks.track_touched_files"


def test_an_injecting_hook_injects_over_the_transport(tmp_path: Path):
    httpd, port = _bind_handler(tmp_path, dispatch=_echoing_dispatch)
    try:
        status, body = _post(
            port,
            {"hook_event_name": "UserPromptExpansion", "prompt": "pickup a handoff"},
            path="/hook/hooks.receiver_state_sensor",
        )
    finally:
        httpd.shutdown()
    assert status == 200
    assert body["hookSpecificOutput"]["additionalContext"] == "routed to hooks.receiver_state_sensor"
    assert body["systemMessage"] == "op hooks.receiver_state_sensor ran"
    assert body["hookSpecificOutput"]["hookEventName"] == "UserPromptExpansion"


def test_an_out_of_namespace_path_is_404_not_dispatched(tmp_path: Path):
    import urllib.error

    calls = []

    def _recording_dispatch(msg, *, caller=None, isolated=False):
        calls.append(msg.get("method"))
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {}}

    httpd, port = _bind_handler(tmp_path, dispatch=_recording_dispatch)
    try:
        try:
            _post(port, {"hook_event_name": "PreToolUse"}, path="/hook/ceremony.scoped_git_commit")
            raise AssertionError("out-of-namespace path was served")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
    finally:
        httpd.shutdown()
    assert calls == []


def test_a_bare_hook_post_still_reaches_the_guard_op(tmp_path: Path):
    seen = []

    def _recording_dispatch(msg, *, caller=None, isolated=False):
        seen.append(msg.get("method"))
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {}}

    httpd, port = _bind_handler(tmp_path, dispatch=_recording_dispatch)
    try:
        _post(port, {"hook_event_name": "PreToolUse", "tool_name": "Bash"})
    finally:
        httpd.shutdown()
    assert seen == [supervisor.GUARD_OP_NAME]


def test_explicit_guard_op_path_still_refuses_an_event_it_has_no_route_for(tmp_path: Path):
    seen = []

    def _recording_dispatch(msg, *, caller=None, isolated=False):
        seen.append(msg.get("method"))
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {}}

    httpd, port = _bind_handler(tmp_path, dispatch=_recording_dispatch)
    try:
        status, body = _post(
            port,
            {"hook_event_name": "SessionStart", "source": "startup"},
            path="/hook/%s" % supervisor.GUARD_OP_NAME,
        )
    finally:
        httpd.shutdown()

    assert status == 200
    hso = body["hookSpecificOutput"]
    assert "permissionDecision" not in hso
    assert "did not run" in hso["additionalContext"]
    assert seen == []


def test_an_engine_skew_answer_is_a_409_so_the_forwarder_runs_the_guard_cold(tmp_path):
    """ENGINE_SKEW is provably-not-run AND runnable cold. Wrapped as a 200 "guard did
    not run" body it passed every Bash guard unchecked for the length of every publish;
    as a 409 -- `_refuse_stale_caller`'s own status -- it sends the forwarder down its
    non-2xx ladder to `hook_http.evaluate_cold`, which runs the guard."""
    import urllib.error

    import pytest

    def _skew_dispatch(msg, *, caller=None, isolated=False):
        return {
            "jsonrpc": "2.0",
            "id": msg.get("id"),
            "error": {"code": skew.ENGINE_SKEW, "message": "engine generation changed"},
        }

    httpd, port = _bind_handler(tmp_path, dispatch=_skew_dispatch)
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(port, {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                         "tool_input": {"command": "echo hi"}})
        assert exc.value.code == 409
        assert json.loads(exc.value.read())["error"]["code"] == skew.ENGINE_SKEW
    finally:
        httpd.shutdown()
