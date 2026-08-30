"""The supervisor HTTP leg is fenced, not pooled (C2, docs/plans/2026-08-30-every-op-
runs-in-the-callers-environment.md).

Purpose: `supervisor.py` serves on a `ThreadingHTTPServer` and leaves `dispatch` at its
`_run_dispatch` default -- production never overrides it, so production dispatch runs
in-process on the connection's own thread. The spike behind this plan measured 8 of 8
concurrent requests on that shape reading another caller's identity, some reading `None`
because a peer's restore fired mid-flight. This leg is therefore FENCED rather than
pooled: it binds the caller's identity as a `ContextVar` only (`isolated: False`, C3's
terms) and never borrows into `os.environ`.

This file pins the fence property itself, against the REAL production dispatch path --
`ctx.dispatch` is left at its default (`None`), so every request here goes through
`warm.server._run_dispatch` exactly as a live `/hook` fire does. Two concurrent HTTP
requests, distinct carried identities, are held in lockstep with a `threading.Barrier` so
both are in-flight inside the served op at once -- the exact overlap window the spike
measured contaminated -- and each must resolve ONLY its own identity via
`session.core.carried_session_id()`, with `os.environ` left untouched throughout.

Negative-spec (RAG-bait):
    Does not exercise the process-pool leg (`_pool_dispatch`) or the `BrokenProcessPool`
    degrade path -- `test_entry_seam_env_borrow.py` (C3) pins `per_request_state`'s own
    `isolated` axis in isolation, and this file's job is only to prove the supervisor leg
    actually reaches that axis with `isolated=False` under real concurrent load, not to
    re-pin the axis itself.
    Does not fake `dispatch` -- `test_supervisor_hook_serves_real_guard.py` already covers
    the guard-verdict shape with a stand-in `dispatch`; this file leaves `dispatch` at its
    production default so the fence is proven against the real `_run_dispatch` call.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.request
from pathlib import Path
from typing import Optional

from coordinator_core import ipc
from coordinator_core.session.core import carried_session_id
from coordinator_core.warm import cookie, hook_http, skew, supervisor

_SID_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_SID_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

_SESSION_ENV_NAMES = ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID")

_BOUND_TOKEN: Optional[str] = None


def _post(port: int, event: dict, timeout: float = 5.0):
    body = json.dumps(event).encode("utf-8")
    headers = {"Content-Type": "application/json", cookie.COOKIE_HEADER: _BOUND_TOKEN}
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (port, supervisor.HOOK_PATH),
        data=body,
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read())


def _bind_handler(tmp_path: Path):
    """Real production wiring, `ctx.dispatch` left at its default -- mirrors
    `test_supervisor_hook_serves_real_guard.py :: _bind_handler` minus the `dispatch=`
    stand-in, which is the whole point of this file."""
    from http.server import ThreadingHTTPServer

    global _BOUND_TOKEN
    skew.write_engine_stamp(tmp_path, "sha-test")
    root = tmp_path
    _BOUND_TOKEN = cookie.ensure(root)
    version_state = skew.ServerVersionState(root)
    ctx = supervisor._ServerContext(httpd=None, engine_root=root, version_state=version_state)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), supervisor._make_handler(ctx))
    ctx.httpd = httpd
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, port


def test_two_concurrent_callers_each_resolve_their_own_identity_via_carried_session_id(
    tmp_path: Path, monkeypatch
):
    for name in _SESSION_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    before_env = dict(os.environ)

    barrier = threading.Barrier(2)

    async def _capture_identity_op(params: dict, repo_root: Optional[Path] = None) -> dict:
        # Force both requests to be in-flight inside the served op at once -- the exact
        # overlap window the spike measured 8/8 contaminated.
        barrier.wait(timeout=5)
        sid = carried_session_id()
        borrowed = any(name in os.environ for name in _SESSION_ENV_NAMES)
        return {"systemMessage": "sid=%s|env_borrowed=%s" % (sid, borrowed)}

    monkeypatch.setitem(ipc._REGISTRY, hook_http.DEFAULT_OP_NAME, _capture_identity_op)

    httpd, port = _bind_handler(tmp_path)
    results: dict = {}
    errors: list = []

    def _fire(sid: str, key: str) -> None:
        try:
            _, body = _post(
                port,
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Bash",
                    "session_id": sid,
                },
            )
            results[key] = body["systemMessage"]
        except Exception as exc:  # noqa: BLE001 -- surfaced via `errors`, not swallowed
            errors.append(exc)

    t1 = threading.Thread(target=_fire, args=(_SID_A, "a"))
    t2 = threading.Thread(target=_fire, args=(_SID_B, "b"))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)
    httpd.shutdown()

    assert not errors, errors
    assert results["a"] == "sid=%s|env_borrowed=False" % _SID_A
    assert results["b"] == "sid=%s|env_borrowed=False" % _SID_B

    # The fence's other half: this threaded leg never mutates process-wide os.environ,
    # at any point, for any caller -- unlike the pool leg's `isolated=True` mirror.
    assert dict(os.environ) == before_env
