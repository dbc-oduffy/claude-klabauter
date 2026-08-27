"""The HTTP path must never let a CALLER's token reach `skew.evict_on_skew`.

NEGATIVE SPEC, and the reason this file exists rather than a comment. A wrong
`_engine_token` is not refused by `warm.server._serve_line` -- it is routed to
`skew.evict_on_skew`, which runs `close_listener()` then `drain()`, taking the warm
engine down so every other session on the box sees `FileNotFoundError` and starts a
current server. That remedy is correct for a stale SERVER and catastrophic for a stale
CALLER, and `ServerVersionState.is_skewed` cannot tell them apart: it is a plain
inequality, `compute_client_token(self._root) != client_token`.

WHAT HAS BEEN KEEPING THAT PATH UNREACHABLE, on each transport:

  * Named pipe -- the token is a COMPONENT OF THE PIPE NAME
    (`\\\\.\\pipe\\coordinator-core.<user-sid>.<clone-hash>.<engine-token>`, built by
    `election.pipe_name` from the client's own `engine_token()`). A caller holding a
    stale token dials a name that does not exist, takes `FileNotFoundError`, and goes
    cold. It cannot reach `_serve_line` at all, so the ambiguity above is harmless
    there. What axis 1 actually catches on the pipe is the narrow race where a publish
    lands BETWEEN pipe-name computation and the request -- where the server really is
    being stranded and evicting is the right answer.

  * HTTP -- a fixed, published port with NO token in its name and therefore no
    equivalent binding. The only thing standing between a caller-supplied token and
    `evict_on_skew` is `supervisor.do_POST` overwriting it:
    `_frame_from_request(request_frame, ctx.engine_token)` stamps the SERVER's own
    token at the frame's top level, so a caller's `_engine_token` lands nested inside
    `params` and is never read by `_serve_line`'s `msg.pop("_engine_token")`.

THE LINE IS NOW GONE, AND THE REFUSAL LANDED WITH IT (C5, 2026-08-26). It was removed
deliberately: an op CLI needs its own token honoured or it gets served silently by a
stale generation instead of `ENGINE_SKEW`. What holds the line now is
`supervisor._Handler._refuse_stale_caller`, which supplies the axis distinction
`is_skewed` cannot -- server current + caller behind is REFUSED (409 / `ENGINE_SKEW`),
server stale is axis 2 and still evicts, unchanged. A tokenless request still receives
the server's own stamp, so the hook-fire path is untouched.

The tripwire therefore moved rather than retired: it no longer asserts that a caller's
token is overwritten, because it is not. It asserts that a caller's token reaching the
server CANNOT take the engine down -- which is the property the overwrite was ever a
proxy for.

Refusal semantics are specified once, normatively, in
`docs/research/spike-verdicts/2026-08-26-loopback-op-dispatch-credential-shape.md`
§ Refusal semantics -- axis 1 over HTTP refuses THAT CALLER, axis 2 keeps its eviction
unchanged. Consume that section; do not re-derive it here.

If this file goes red, do not "fix" it by relaxing an assertion. The still-serving
assertions are the point: a refusal that ALSO drains or closes the listener is an
eviction spelt differently, and it costs every session on the box (measured at 16.8s
under a 17s drain). Red here means the refusal became an eviction again.
"""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from coordinator_core.warm import cookie, hook_http, skew, supervisor
from coordinator_core.warm.http_listener import ENGINE_TOKEN_HEADER, _frame_from_request

SERVER_TOKEN = "5e4be47a0000ffff"


def _live_listener(tmp_path):
    """A real supervisor handler on a real socket, over a real cookie and a real
    engine stamp -- the refusal under test is a property of the live path, not of a
    frame helper."""
    skew.write_engine_stamp(tmp_path, "sha-test")
    token = cookie.ensure(tmp_path)
    ctx = supervisor._ServerContext(
        httpd=None,
        engine_root=tmp_path,
        version_state=skew.ServerVersionState(tmp_path),
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), supervisor._make_handler(ctx))
    ctx.httpd = httpd
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd.server_address[1], token, httpd


def _post(port, cookie_token, event, engine_token=None):
    headers = {
        "Content-Type": "application/json",
        cookie.COOKIE_HEADER: cookie_token,
    }
    if engine_token is not None:
        headers[ENGINE_TOKEN_HEADER] = engine_token
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{supervisor.HOOK_PATH}",
        data=json.dumps(event).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read() or b"{}"
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {}


def _still_serving(port, cookie_token) -> bool:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{supervisor.HEALTH_PATH}",
        headers={cookie.COOKIE_HEADER: cookie_token},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001 -- any failure means it stopped serving
        return False
HOSTILE_TOKEN = "deadbeefdeadbeef"


def _top_level_token(frame: bytes):
    """The value `_serve_line`'s `msg.pop("_engine_token", None)` would actually read."""
    return json.loads(frame.decode("utf-8")).get("_engine_token")


def _hostile_event():
    """A posted event carrying a caller-chosen `_engine_token`, spelled every way the
    frame might absorb it -- top level of the body and inside a nested payload."""
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "echo probe"},
        "_engine_token": HOSTILE_TOKEN,
        "params": {"_engine_token": HOSTILE_TOKEN},
    }


def test_a_stale_caller_is_refused_and_the_listener_keeps_serving(tmp_path):
    """AC9, ASSERTION 1 -- REWRITTEN, NOT MADE GREEN BY ASSERTING THE NEW BEHAVIOUR.

    This assertion used to say the server overwrites a caller's token. It does not
    any more, and the honest replacement is not "it now passes through" -- it is the
    property the overwrite existed to protect: a caller-controlled token must not be
    able to take the warm engine down.

    So: present a valid cookie and a WRONG engine token, and require both halves --
    ENGINE_SKEW back to this caller, and the listener still answering afterwards.
    """
    port, token, httpd = _live_listener(tmp_path)
    try:
        status, body = _post(port, token, _hostile_event(), engine_token=HOSTILE_TOKEN)

        assert status == 409, (
            "a stale caller must be REFUSED, not served and not evicted -- see "
            "§ Refusal semantics row 2"
        )
        assert body.get("error", {}).get("code") == skew.ENGINE_SKEW, (
            "the refusal must carry ENGINE_SKEW so the caller knows to retry cold"
        )
        assert _still_serving(port, token), (
            "THE HALF THAT MATTERS: a refusal that stops the listener serving is an "
            "eviction spelt differently, and costs every session on the box."
        )
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_burst_of_stale_callers_cannot_drain_the_engine(tmp_path):
    """AC9, ASSERTION 2 -- one refusal leaving the listener up could be luck. A run of
    them establishes that the refusal path is not wired to any drain or close."""
    port, token, httpd = _live_listener(tmp_path)
    try:
        for _ in range(10):
            status, _body = _post(
                port, token, _hostile_event(), engine_token=HOSTILE_TOKEN
            )
            assert status == 409
        assert _still_serving(port, token)
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_build_request_never_lifts_a_caller_token_to_the_top_level():
    """`build_request` nests the caller's event under `params`, so nothing the caller
    writes is read as the frame's own token -- true even before the stamp overwrites it."""
    frame = hook_http.build_request(_hostile_event(), hook_http.DEFAULT_OP_NAME)

    assert _top_level_token(frame) is None, (
        "A caller-supplied `_engine_token` reached the frame's top level. `build_request` "
        "must nest the posted event under `params`."
    )
    assert HOSTILE_TOKEN in frame.decode("utf-8"), (
        "Sanity check on the fixture itself: the hostile token should still be present "
        "somewhere in the frame (nested), or this test proves nothing."
    )


def test_a_none_token_passes_the_body_through_verbatim():
    """THE ARMING CONDITION, asserted so it cannot be introduced silently.

    `_frame_from_request` returns the body UNCHANGED when the token is None. That is why
    a `None` reaching it from an HTTP handler is not a neutral no-op: a body-supplied
    `_engine_token` would then be read verbatim by `_serve_line`. This test does not
    forbid the passthrough -- it pins it, so the two tests above are understood as the
    things standing between it and the eviction path.
    """
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "_engine_token": HOSTILE_TOKEN}).encode()

    assert _frame_from_request(body, None) == body
    assert _top_level_token(_frame_from_request(body, None)) == HOSTILE_TOKEN
