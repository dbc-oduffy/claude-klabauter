"""A request frame larger than one buffered read must still be SERVED.

WHAT REGRESSED, AND WHY THIS FILE EXISTS. Both named-pipe creation sites --
`election.elect` (the first instance) and `server._create_pipe_instance`
(every follow-on one) -- passed `PIPE_READMODE_MESSAGE` until 2026-09-06. In
message read mode a `ReadFile` whose buffer is smaller than the pending
message fails with `ERROR_MORE_DATA` rather than returning a partial read.
`server._wrap_handle` hands the pipe to a `BufferedReader` whose underlying
reads are `io.DEFAULT_BUFFER_SIZE` (8192), so every request frame over 8192
bytes made `_handle_connection`'s `io.readline()` raise `OSError` -- caught
there with a bare `return`, which closes the connection without a reply.

THE FAILURE SHAPE IS THE WORST ONE THIS TRANSPORT HAS. The caller's door had
already delivered every byte, so it could not fall through to a cold spawn
without risking a double-executed mutation; its only remaining move was
`-32004 warm dispatch indeterminate` -- on a request the server never so much
as parsed. Reported live from a plan-blitz wave: a 13,456-byte
`roadmap.blitz_land` request killed the connection twice, while the same call
with one field dropped (2,164 bytes) landed first try. Measured threshold:
8192 bytes served, 8193 refused, exactly.

WHY THE THRESHOLD IS PARAMETRISED AROUND `io.DEFAULT_BUFFER_SIZE` RATHER THAN
PINNED AT 8192. The number was never a protocol constant -- it is whatever
CPython buffers a raw read at, and a future interpreter is free to change it.
The property under test is "frame size does not decide whether a request is
answered", so the cases are derived from the buffer size in force at run time
and the largest one deliberately clears the 64 KiB pipe buffer too
(`server._PIPE_BUFFER_BYTES`), which is a different mechanism: past that the
client's write blocks until the server drains it, and a reader that could only
handle what fits in the kernel buffer would pass every smaller case.

BOTH CREATION SITES ARE EXERCISED, not just the one that carries the constant.
They must agree, and nothing else in this suite would notice if they stopped:
which instance accepts a given connection is a race, so a drifted pair would
present as an op that fails intermittently on large payloads -- the hardest
possible read of this defect.

NO LIVE WARM SERVER IS INVOLVED. Both pipes are created under a random name
of this test's own, so neither can collide with the fleet server's hashed
endpoint. The resident server is never consulted, started, or stopped.
"""

from __future__ import annotations

import io as _io
import json
import os
import threading
import uuid
import pytest

pytestmark = [
    pytest.mark.warm_tier,
    pytest.mark.skipif(os.name != "nt", reason="named-pipe read mode is Windows-only"),
]


class _FakeVersionState:
    """Never skewed -- skew is `test_server_loop.py`'s subject, and a skewed
    verdict short-circuits before the frame is dispatched, which would make
    this file pass without ever reading a large request."""

    server_sha = "deadbeef"

    def is_skewed(self, client_token: str) -> bool:
        return False


def _pipe_name() -> str:
    return r"\\.\pipe\claude-klabauter-oversized-frame-test-" + uuid.uuid4().hex


def _frame(payload_bytes: int) -> bytes:
    """One newline-terminated JSON-RPC request of EXACTLY `payload_bytes`,
    padded inside `params` so the size lands on the wire rather than in a
    field the server discards before reading the rest of the line."""
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "noop",
        "params": {"pad": ""},
        "_engine_token": "client-token",
    }
    base = len(json.dumps(msg).encode("utf-8")) + 1  # +1 for the newline
    msg["params"]["pad"] = "x" * max(0, payload_bytes - base)
    return (json.dumps(msg) + "\n").encode("utf-8")


def _serve_one(handle: int, seen: list, written: list) -> None:
    """The production read path, whole: `_wrap_handle` then
    `_handle_connection`. `dispatch` records what actually arrived, so a
    truncated frame fails on content rather than only on silence, and the
    connection's writes are captured so the caller can assert on the ENVELOPE
    -- "the server answered" is not the property, "the server answered THIS
    request" is, and an internal-error envelope satisfies the first.

    `**_kwargs` is not laziness: `_serve_line` calls `dispatch` with keyword
    arguments (`caller`, and whatever joins it next), and a stub pinned to
    today's exact signature would fail this file for a reason that has
    nothing to do with frame size."""
    from coordinator_core.warm import server

    conn = server._wrap_handle(handle)

    class _Tee:
        """Records what the server writes without changing what it writes to
        -- the response has to land on the real pipe, because a client
        blocked on reading it is what keeps the write from being a no-op."""

        def readline(self) -> bytes:
            return conn.readline()

        def write(self, data: bytes) -> None:
            written.append(data)
            conn.write(data)

        def flush(self) -> None:
            conn.flush()

        def close(self) -> None:
            conn.close()

    def _dispatch(msg: dict, **_kwargs) -> dict:
        seen.append(msg)
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"ok": True}}

    server._handle_connection(
        _Tee(),
        version_state=_FakeVersionState(),
        server_sha=_FakeVersionState.server_sha,
        close_listener=lambda: None,
        drain=lambda: None,
        in_flight=server.InFlightCounter(),
        dispatch=_dispatch,
    )


def _sizes() -> list:
    """Derived, never pinned -- see the module docstring."""
    from coordinator_core.warm import server

    buf = _io.DEFAULT_BUFFER_SIZE
    return [buf - 1, buf, buf + 1, 13456, server._PIPE_BUFFER_BYTES * 2]


def _create_via_server(name: str) -> int:
    from coordinator_core.warm import election, server

    return server._create_pipe_instance(name, election.current_user_sid())


def _create_via_election(name: str) -> int:
    from coordinator_core.warm import election

    return election.elect(name)


@pytest.mark.parametrize("size", _sizes())
@pytest.mark.parametrize(
    "create",
    [_create_via_server, _create_via_election],
    ids=["follow_on_instance", "first_instance"],
)
def test_a_request_frame_larger_than_the_read_buffer_is_still_served(create, size):
    import _winapi

    from coordinator_core.warm import server

    name = _pipe_name()
    handle = create(name)

    seen: list = []
    written: list = []

    def _accept() -> None:
        try:
            _winapi.ConnectNamedPipe(handle, _winapi.NULL)
        except OSError as exc:
            if getattr(exc, "winerror", None) != server._ERROR_PIPE_CONNECTED:
                raise
        _serve_one(handle, seen, written)

    accept_thread = threading.Thread(target=_accept, daemon=True)
    accept_thread.start()

    client = _winapi.CreateFile(
        name,
        _winapi.GENERIC_READ | _winapi.GENERIC_WRITE,
        0,
        _winapi.NULL,
        _winapi.OPEN_EXISTING,
        0,
        _winapi.NULL,
    )
    try:
        frame = _frame(size)
        assert len(frame) == size
        # ONE `WriteFile` for the whole frame, which is what `door.c`'s
        # `write_frame_bounded` does -- the single-message shape is the input
        # that message read mode choked on, so chunking it here would test a
        # case the real caller never produces.
        _winapi.WriteFile(client, frame)
        accept_thread.join(30)
        assert not accept_thread.is_alive(), (
            f"the server never finished a {size}-byte request"
        )
    finally:
        _winapi.CloseHandle(client)

    assert written, f"connection closed without a reply at {size} bytes"
    response = json.loads(written[0].decode("utf-8"))
    assert "error" not in response, (
        f"{size}-byte request refused: {response['error']}"
    )
    assert len(seen) == 1, f"the request was never dispatched at {size} bytes"


def test_the_padding_arrives_whole_not_merely_a_response() -> None:
    """The size assertion the parametrised test cannot make cheaply: a reader
    that recovered from `ERROR_MORE_DATA` by DISCARDING the remainder would
    still answer, and answering is most of what the other test checks."""
    import _winapi

    from coordinator_core.warm import election, server

    name = _pipe_name()
    handle = server._create_pipe_instance(name, election.current_user_sid())
    size = _io.DEFAULT_BUFFER_SIZE * 3

    seen: list = []
    written: list = []

    def _accept() -> None:
        try:
            _winapi.ConnectNamedPipe(handle, _winapi.NULL)
        except OSError as exc:
            if getattr(exc, "winerror", None) != server._ERROR_PIPE_CONNECTED:
                raise
        _serve_one(handle, seen, written)

    accept_thread = threading.Thread(target=_accept, daemon=True)
    accept_thread.start()

    client = _winapi.CreateFile(
        name,
        _winapi.GENERIC_READ | _winapi.GENERIC_WRITE,
        0,
        _winapi.NULL,
        _winapi.OPEN_EXISTING,
        0,
        _winapi.NULL,
    )
    try:
        frame = _frame(size)
        _winapi.WriteFile(client, frame)
        accept_thread.join(30)
        assert not accept_thread.is_alive()
    finally:
        _winapi.CloseHandle(client)

    assert written
    assert "error" not in json.loads(written[0].decode("utf-8"))
    expected_pad = json.loads(frame.decode("utf-8"))["params"]["pad"]
    assert seen[0]["params"]["pad"] == expected_pad
