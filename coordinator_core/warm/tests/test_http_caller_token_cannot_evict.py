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

THAT SINGLE LINE IS LOAD-BEARING SECURITY, AND IT DOES NOT LOOK IT. It reads like a
redundant assignment, and removing it is exactly what the op-CLI widening has to do --
an op CLI needs its own token honoured or it gets served silently by a stale generation
instead of `ENGINE_SKEW`. So it will be removed, deliberately, and when it is, the
refusal gate MUST land in the same change.

Refusal semantics are specified once, normatively, in
`docs/research/spike-verdicts/2026-08-26-loopback-op-dispatch-credential-shape.md`
§ Refusal semantics -- axis 1 over HTTP refuses THAT CALLER, axis 2 keeps its eviction
unchanged. Consume that section; do not re-derive it here.

If this file goes red, do not "fix" it by asserting the new behaviour. It is red
because a caller-controlled token can now reach the eviction path, and the question to
answer is whether the refusal gate landed with it.
"""

import json

from coordinator_core.warm import hook_http
from coordinator_core.warm.http_listener import _frame_from_request

SERVER_TOKEN = "5e4be47a0000ffff"
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


def test_supervisor_frame_stamps_the_server_token_over_a_hostile_one():
    """The supervisor path's two steps, in order, on an event that tries to supply a token."""
    frame = hook_http.build_request(_hostile_event(), hook_http.DEFAULT_OP_NAME)
    stamped = _frame_from_request(frame, SERVER_TOKEN)

    assert _top_level_token(stamped) == SERVER_TOKEN, (
        "supervisor.do_POST must stamp the SERVER's own engine token at the frame's top "
        "level. If a caller's token survives here it reaches _serve_line, and a wrong one "
        "evicts the warm engine for the whole box. See this module's docstring."
    )
    assert _top_level_token(stamped) != HOSTILE_TOKEN


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
