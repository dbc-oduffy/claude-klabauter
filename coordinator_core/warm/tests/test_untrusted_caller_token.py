
from __future__ import annotations

import json

from coordinator_core.warm import server
from coordinator_core.warm.tests.test_server_loop import _FakeIO, _FakeVersionState, _frame


def test_tokenless_request_is_refused_not_silently_served():
    """A frame with no `_engine_token` key must be refused with
    `UNTRUSTED_CALLER_ERROR`, and `dispatch` must never be called for it --
    the fail-open gap this row closes. Fails against pre-fix `server.py`,
    where such a frame reached `dispatch` and got an ordinary `ok` result.
    """
    dispatch_calls: list[dict] = []

    def _dispatch(msg: dict, *, caller=None, isolated=False) -> dict:
        dispatch_calls.append(msg)
        return {"jsonrpc": "2.0", "id": msg["id"], "result": "ok"}

    io_obj = _FakeIO([_frame(id_=1, method="ping", token=None)])

    server._handle_connection(
        io_obj,
        version_state=_FakeVersionState(skewed=False),
        server_sha="deadbeef",
        close_listener=lambda: (_ for _ in ()).throw(AssertionError("must not close listener")),
        drain=lambda: (_ for _ in ()).throw(AssertionError("must not drain")),
        in_flight=server.InFlightCounter(),
        dispatch=_dispatch,
    )

    assert dispatch_calls == []
    response = json.loads(io_obj.written[0])
    assert response["error"]["code"] == server.UNTRUSTED_CALLER_ERROR
    assert response["id"] == 1
    assert io_obj.closed


def test_tokenless_request_does_not_evict_the_server():
    io_obj = _FakeIO([_frame(id_="x", method="ping", token=None)])

    server._handle_connection(
        io_obj,
        version_state=_FakeVersionState(skewed=True),
        server_sha="deadbeef",
        close_listener=lambda: (_ for _ in ()).throw(AssertionError("must not close listener")),
        drain=lambda: (_ for _ in ()).throw(AssertionError("must not drain")),
        in_flight=server.InFlightCounter(),
        dispatch=lambda msg, **_: (_ for _ in ()).throw(AssertionError("must not dispatch")),
    )

    response = json.loads(io_obj.written[0])
    assert response["error"]["code"] == server.UNTRUSTED_CALLER_ERROR


def test_present_token_still_reaches_skew_check_unchanged():
    close_calls: list[bool] = []
    drain_calls: list[bool] = []

    io_obj = _FakeIO([_frame(id_=2, method="ping", extra={"_engine_token": "stale-token"})])

    server._handle_connection(
        io_obj,
        version_state=_FakeVersionState(skewed=True, server_sha="abc123"),
        server_sha="abc123",
        close_listener=lambda: close_calls.append(True),
        drain=lambda: drain_calls.append(True),
        in_flight=server.InFlightCounter(),
        dispatch=lambda msg, **_: (_ for _ in ()).throw(AssertionError("must not dispatch on skew")),
    )

    assert close_calls == [True]
    assert drain_calls == [True]
    response = json.loads(io_obj.written[0])
    assert response["error"]["code"] != server.UNTRUSTED_CALLER_ERROR
