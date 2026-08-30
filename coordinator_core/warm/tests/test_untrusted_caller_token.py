"""Tests for `coordinator_core.warm.server`'s handling of a request that
carries no `_engine_token` field at all.

Spec backlink: state/handoffs/2026-08-21_103635_reaching-the-warm-engine.md
-- "Measured findings" section, session 1c9c881e. Prior to the fix under
test, `_serve_line`'s skew check (`client_token is not None and
version_state.is_skewed(client_token)`) short-circuited past `is_skewed`
entirely for a tokenless frame -- served whatever the listening generation
happened to be, with the comparison never attempted. No in-tree caller (see
`coordinator_core.warm.client.engine_token`, which always stamps a token,
falling back to the literal `"unversioned"` rather than omitting the field)
ever produced that shape, so the gap was latent, not live traffic.

Reuses `test_server_loop.py`'s own `_FakeIO` / `_FakeVersionState` / `_frame`
helpers rather than redefining them -- same fake-connection-object pattern
that module's own docstring explains (no real Windows named pipe needed to
exercise `_serve_line`, importable on non-Windows).
"""

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

    io_obj = _FakeIO([_frame(id_=1, method="ping", token=None)])  # token=None: no _engine_token at all

    server._handle_connection(
        io_obj,
        version_state=_FakeVersionState(skewed=False),
        server_sha="deadbeef",
        close_listener=lambda: (_ for _ in ()).throw(AssertionError("must not close listener")),
        drain=lambda: (_ for _ in ()).throw(AssertionError("must not drain")),
        in_flight=server.InFlightCounter(),
        dispatch=_dispatch,
    )

    assert dispatch_calls == []  # never reached the engine core
    response = json.loads(io_obj.written[0])
    assert response["error"]["code"] == server.UNTRUSTED_CALLER_ERROR
    assert response["id"] == 1
    assert io_obj.closed


def test_tokenless_request_does_not_evict_the_server():
    """Distinct from the skew path: a tokenless request must NOT run
    `close_listener` / `drain` -- doing so would let any anonymous caller
    kill the shared resident server (every other session's pipe) merely by
    omitting one field. This test's `close_listener`/`drain` both raise if
    called, so a regression toward "treat missing as skewed" fails loudly
    rather than passing by accident.
    """
    io_obj = _FakeIO([_frame(id_="x", method="ping", token=None)])

    server._handle_connection(
        io_obj,
        version_state=_FakeVersionState(skewed=True),  # would evict IF reached
        server_sha="deadbeef",
        close_listener=lambda: (_ for _ in ()).throw(AssertionError("must not close listener")),
        drain=lambda: (_ for _ in ()).throw(AssertionError("must not drain")),
        in_flight=server.InFlightCounter(),
        dispatch=lambda msg, **_: (_ for _ in ()).throw(AssertionError("must not dispatch")),
    )

    response = json.loads(io_obj.written[0])
    assert response["error"]["code"] == server.UNTRUSTED_CALLER_ERROR


def test_present_token_still_reaches_skew_check_unchanged():
    """Negative-spec companion: a request that DOES carry `_engine_token`
    must be unaffected by this row -- still reaches `version_state.
    is_skewed`, still evicts on a real mismatch. Guards against a fix that
    over-broadly refuses every request instead of only the tokenless ones.
    """
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
