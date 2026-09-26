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
    event = {"hook_event_name": "PreToolUse", "session_id": "sess-distinct-abc123"}
    frame = json.loads(hook_http.build_request(event, hook_http.DEFAULT_OP_NAME))
    assert _caller_of(frame)["session_id"] == "sess-distinct-abc123"


def test_top_level_session_id_matches_the_nested_payload_copy():
    event = {"hook_event_name": "PreToolUse", "session_id": "sess-xyz"}
    frame = json.loads(hook_http.build_request(event, hook_http.DEFAULT_OP_NAME))
    assert _caller_of(frame)["session_id"] == frame["params"]["payload"]["session_id"] == "sess-xyz"


def test_absent_session_id_carries_no_fabricated_identity():
    frame = json.loads(hook_http.build_request({"hook_event_name": "PreToolUse"}, hook_http.DEFAULT_OP_NAME))
    assert _caller_of(frame)["session_id"] is None


def test_empty_string_session_id_also_carries_no_identity():
    frame = json.loads(
        hook_http.build_request({"hook_event_name": "PreToolUse", "session_id": ""}, hook_http.DEFAULT_OP_NAME)
    )
    assert _caller_of(frame)["session_id"] is None


#: object SERVER-SIDE by `warm.server._serve_line` via `merge_env_axis`
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

    for excluded in _CALLER_CONTEXT_FIELDS_NOT_ON_THE_WIRE_OBJECT:
        assert excluded not in _caller_of(frame)
    assert settings_home_claim.SETTINGS_HOME_FIELD == "_settings_home"


def test_distinct_callers_do_not_cross_contaminate():
    frame_a = json.loads(
        hook_http.build_request({"session_id": "caller-a"}, hook_http.DEFAULT_OP_NAME)
    )
    frame_b = json.loads(
        hook_http.build_request({"session_id": "caller-b"}, hook_http.DEFAULT_OP_NAME)
    )
    assert _caller_of(frame_a)["session_id"] == "caller-a"
    assert _caller_of(frame_b)["session_id"] == "caller-b"


def test_retired_session_id_key_is_not_resurrected():
    frame = json.loads(hook_http.build_request({"session_id": "sess-1"}, hook_http.DEFAULT_OP_NAME))
    assert "_session_id" not in frame


def test_caller_stamp_does_not_disturb_the_engine_token_contract():
    frame = json.loads(
        hook_http.build_request({"session_id": "sess-1", "env": {}}, "guard.evaluate")
    )
    assert "_engine_token" not in frame
    assert _caller_of(frame)["session_id"] == "sess-1"


# NEGATIVE SPEC. This section does not touch the HTTP leg's own tests above

from coordinator_core.warm import client as _warm_client
from coordinator_core.warm import server as _warm_server
from coordinator_core.warm.caller_context import CallerContext


class _FakePipe:

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
    assert sent["_caller"]["pid"]


def test_serve_line_reads_the_widened_caller_object_and_threads_the_pid():
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
