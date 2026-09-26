
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

    assert dict(os.environ) == before_env
