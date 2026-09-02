"""The HTTP leg carries the caller's identity at the request's TOP LEVEL, where `_serve_line` reads it.

THE FALSIFIER THIS PINS. `warm/server.py :: _serve_line` resolves caller identity off the
envelope's top level -- the same way it pops `_engine_token`. But `hook_http.build_request`
used to nest the caller's id only at `params.payload.session_id`, and
`http_listener._frame_from_request` injects only `_engine_token` above it. The key
`_serve_line` looks for was simply never sent, so a resident server's per-request identity
fell back to whichever session spawned it.

This file pins the wire shape, not the plumbing: an event carrying a distinct `session_id`
must produce a frame whose TOP-LEVEL caller object carries it.

WHICH KEY, AND WHY IT CHANGED. C1a first stamped a bare top-level `_session_id`. C1b then
widened BOTH legs to one `_caller` object (a serialised `warm.caller_context.CallerContext`)
and retired the `_session_id` key outright, with no alias -- `_serve_line` reads only
`_caller` now. These tests assert `_caller`, because asserting the retired key would read
green against an envelope dispatch no longer looks at: the identity would be on the wire and
still dropped on the floor. The `pid` field is why the widening matters here and not only on
the pipe leg -- `harness_registry.self_record()` keys off `CLAUDE_PID`, not off `session_id`.
"""

from __future__ import annotations

import json

from coordinator_core.warm import hook_http


def _caller_of(frame):
    return frame["_caller"]


def test_event_session_id_lands_at_the_frames_top_level():
    """The falsifier's own assertion, promoted to a durable test."""
    event = {"hook_event_name": "PreToolUse", "session_id": "sess-distinct-abc123"}
    frame = json.loads(hook_http.build_request(event, hook_http.DEFAULT_OP_NAME))
    assert _caller_of(frame)["session_id"] == "sess-distinct-abc123"


def test_top_level_session_id_matches_the_nested_payload_copy():
    """`_serve_line` reads the top-level object; `payload_from_event` still carries
    `session_id` verbatim for op code that reads it off the payload. Both must agree --
    this is a second copy of one fact, not two independent ones."""
    event = {"hook_event_name": "PreToolUse", "session_id": "sess-xyz"}
    frame = json.loads(hook_http.build_request(event, hook_http.DEFAULT_OP_NAME))
    assert _caller_of(frame)["session_id"] == frame["params"]["payload"]["session_id"] == "sess-xyz"


def test_absent_session_id_carries_no_fabricated_identity():
    """Omit-never-substitute, at the field rather than at the object: an unresolvable
    session id stays `None` INSIDE `_caller`, never this process's own id. The object
    itself is still sent -- `pid` and `cwd` are the caller's and are resolvable even when
    the session id is not, and `self_record()` keys off exactly those."""
    frame = json.loads(hook_http.build_request({"hook_event_name": "PreToolUse"}, hook_http.DEFAULT_OP_NAME))
    assert _caller_of(frame)["session_id"] is None


def test_empty_string_session_id_also_carries_no_identity():
    frame = json.loads(
        hook_http.build_request({"hook_event_name": "PreToolUse", "session_id": ""}, hook_http.DEFAULT_OP_NAME)
    )
    assert _caller_of(frame)["session_id"] is None


#: The two `CallerContext` fields that do NOT ride inside the `_caller`
#: object `hook_http.build_request` serialises, per that dataclass's own
#: docstring: `settings_home` rides its own top-level `_settings_home` wire
#: field (`warm/settings_home_claim.py`), and `env` is joined onto the
#: object SERVER-SIDE by `warm.server._serve_line` via `merge_env_axis`
#: after `resolve_caller_context` already ran -- neither producer leg
#: (`hook_http.build_request`, `client.py`) has a value to put there at
#: build time, so both stay unset on the wire object this test reads.
_CALLER_CONTEXT_FIELDS_NOT_ON_THE_WIRE_OBJECT = frozenset({"settings_home", "env"})


def test_caller_object_mirrors_the_caller_context_dataclass():
    """`_caller` IS `CallerContext` serialised -- not a shape merely said to match it.
    A field added to the dataclass and forgotten here is the drift this asserts against,
    except the two fields that are never part of THIS wire object by design (see
    `_CALLER_CONTEXT_FIELDS_NOT_ON_THE_WIRE_OBJECT`). Excluded DERIVED -- dataclass
    fields minus that named set, never a hand-typed list of the ones that DO ride --
    so a field added tomorrow is caught here rather than silently accommodated."""
    import dataclasses

    from coordinator_core.warm import settings_home_claim
    from coordinator_core.warm.caller_context import CallerContext

    frame = json.loads(hook_http.build_request({"session_id": "sess-1"}, hook_http.DEFAULT_OP_NAME))
    dataclass_fields = {f.name for f in dataclasses.fields(CallerContext)}
    wire_fields = dataclass_fields - _CALLER_CONTEXT_FIELDS_NOT_ON_THE_WIRE_OBJECT
    assert set(_caller_of(frame)) == wire_fields

    # Each excluded field is absent from `_caller` by contract, and the one with a
    # top-level carrier has it named by the module that owns it -- not merely omitted
    # by coincidence.
    for excluded in _CALLER_CONTEXT_FIELDS_NOT_ON_THE_WIRE_OBJECT:
        assert excluded not in _caller_of(frame)
    assert settings_home_claim.SETTINGS_HOME_FIELD == "_settings_home"


def test_distinct_callers_do_not_cross_contaminate():
    """Two events with different `session_id`s must each carry their OWN id -- the
    zero-cross-contamination exit criterion, at the unit level."""
    frame_a = json.loads(
        hook_http.build_request({"session_id": "caller-a"}, hook_http.DEFAULT_OP_NAME)
    )
    frame_b = json.loads(
        hook_http.build_request({"session_id": "caller-b"}, hook_http.DEFAULT_OP_NAME)
    )
    assert _caller_of(frame_a)["session_id"] == "caller-a"
    assert _caller_of(frame_b)["session_id"] == "caller-b"


def test_retired_session_id_key_is_not_resurrected():
    """`_serve_line` reads only `_caller`. A top-level `_session_id` beside it would be a
    second wire format nothing pops -- the alias C1b deliberately declined to keep."""
    frame = json.loads(hook_http.build_request({"session_id": "sess-1"}, hook_http.DEFAULT_OP_NAME))
    assert "_session_id" not in frame


def test_caller_stamp_does_not_disturb_the_engine_token_contract():
    """`build_request` still carries no `_engine_token` -- the transport places that from
    its header (`test_build_request_carries_no_engine_token`). The identity stamp must not
    have piggybacked a second field onto that seam."""
    frame = json.loads(
        hook_http.build_request({"session_id": "sess-1", "env": {}}, "guard.evaluate")
    )
    assert "_engine_token" not in frame
    assert _caller_of(frame)["session_id"] == "sess-1"


# ---------------------------------------------------------------------------
# C1b -- both the named-pipe/door leg's client widens to the full `_caller`
# identity SET (`warm.caller_context.CallerContext`, serialised directly),
# and `_serve_line` reads only `_caller` now (no `_session_id` alias).
#
# NEGATIVE SPEC. This section does not touch the HTTP leg's own tests above
# (C1a's own wire shape, unmodified) and does not assert on `door.c` --
# there is no Python harness in this suite that builds and runs the native
# door; the door-side change is pinned by its own source comments and by
# this module's identical wire shape on the Python leg, per the plan body's
# "both legs widen ... as one top-level `_caller` object".
# ---------------------------------------------------------------------------

from coordinator_core.warm import client as _warm_client
from coordinator_core.warm import server as _warm_server
from coordinator_core.warm.caller_context import CallerContext


class _FakePipe:
    """Minimal stand-in for `open(pipe, "r+b")`, mirroring
    `test_client_fallback.py`'s own fixture -- records what was written and
    serves one canned response frame."""

    def __init__(self, read_result=b'{"jsonrpc":"2.0","id":1,"result":{}}\n'):
        self.written: list = []
        self._read_result = read_result

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def flush(self) -> None:
        pass

    def readline(self):
        return self._read_result

    def close(self) -> None:
        pass


def test_pipe_leg_wire_carries_a_top_level_caller_object(monkeypatch):
    """An event carrying a distinct identity SET produces a frame whose TOP-LEVEL
    `_caller` equals it, serialised from a `CallerContext` carrying `pid`."""
    monkeypatch.setattr(_warm_client, "is_warm_enabled", lambda: True)
    monkeypatch.setattr(_warm_client, "engine_token", lambda: "faketoken")
    monkeypatch.setattr(_warm_client, "_caller_session_id", lambda: "sess-distinct-abc123")
    fake_pipe = _FakePipe()
    monkeypatch.setattr(_warm_client, "_open_pipe", lambda pipe: fake_pipe)

    response = _warm_client.try_warm_dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}
    )
    assert response == {"jsonrpc": "2.0", "id": 1, "result": {}}

    sent = json.loads(fake_pipe.written[0])
    assert "_session_id" not in sent
    assert "_caller" in sent
    caller = sent["_caller"]
    assert caller["session_id"] == "sess-distinct-abc123"
    assert caller["pid"] == str(__import__("os").getpid())
    assert set(caller.keys()) == {"plugin_root", "cwd", "session_id", "agent_id", "pid"}


def test_pipe_leg_absent_session_id_carries_no_fabricated_identity(monkeypatch):
    monkeypatch.setattr(_warm_client, "is_warm_enabled", lambda: True)
    monkeypatch.setattr(_warm_client, "engine_token", lambda: "faketoken")
    monkeypatch.setattr(_warm_client, "_caller_session_id", lambda: "")
    fake_pipe = _FakePipe()
    monkeypatch.setattr(_warm_client, "_open_pipe", lambda pipe: fake_pipe)

    _warm_client.try_warm_dispatch({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}})

    sent = json.loads(fake_pipe.written[0])
    assert sent["_caller"]["session_id"] is None
    assert sent["_caller"]["pid"]  # always present -- GetCurrentProcessId()/os.getpid() never fail


def test_serve_line_reads_the_widened_caller_object_and_threads_the_pid():
    """`_serve_line` pops the envelope's top-level `_caller`, resolves it into a
    `CallerContext`, and hands the WHOLE object to `dispatch` -- not merely the
    session id -- so a carried `pid` reaches the dispatch seam."""
    captured: dict = {}

    def _dispatch(msg, *, caller=None):
        captured["caller"] = caller
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {}}

    written: list = []
    raw = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "ping",
            "params": {},
            "_engine_token": "unversioned",
            "_caller": {"session_id": "sess-b", "pid": "4242"},
        }
    ).encode("utf-8")

    class _FakeVersionState:
        server_sha = "deadbeef"

        def is_skewed(self, client_token: str) -> bool:
            return False

    _warm_server._serve_line(
        raw,
        write=written.append,
        version_state=_FakeVersionState(),
        server_sha=None,
        close_listener=lambda: None,
        drain=lambda: None,
        release_in_flight=lambda: None,
        dispatch=_dispatch,
    )

    assert isinstance(captured["caller"], CallerContext)
    assert captured["caller"].session_id == "sess-b"
    assert captured["caller"].pid == "4242"
    response = json.loads(written[0])
    assert "_caller" not in response
    assert response["result"] == {}
