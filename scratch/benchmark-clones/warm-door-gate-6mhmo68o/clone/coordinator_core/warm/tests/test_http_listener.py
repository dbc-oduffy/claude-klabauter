"""The loopback HTTP transport binds the right address and reuses the shared frame path.

The bind-address test is the load-bearing one. `localhost` resolves to both `::1` and
`127.0.0.1`; a client that dials the NAME tries IPv6 first against an IPv4-bound listener
and pays a full SYN retry -- ~2s per call on this box, measured. A transport whose whole
purpose is a sub-millisecond round trip is destroyed by that, and the failure is invisible
in code review because the difference is one string. Hence an assertion rather than a
comment.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from coordinator_core.warm import http_listener


def _post(port: int, payload: dict, token: str | None = None, timeout: float = 5.0):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "http://%s:%d/hook" % (http_listener.bind_host(), port),
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if token is not None:
        req.add_header(http_listener.ENGINE_TOKEN_HEADER, token)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def test_bind_host_is_the_ipv4_literal_never_the_name():
    """`localhost` costs ~2s per call via dual-stack SYN retry -- see module docstring."""
    assert http_listener.bind_host() == "127.0.0.1"
    assert "localhost" not in http_listener.bind_host()


def test_round_trip_reaches_serve_line_and_returns_its_frame():
    seen = {}

    def fake_serve_line(raw, *, write, **kwargs):
        seen["raw"] = raw
        seen["kwargs"] = kwargs
        write(b'{"jsonrpc":"2.0","id":1,"result":{"ok":true}}')

    srv, port, _thread = http_listener.start(
        lambda: (fake_serve_line, {"version_state": "vs"})
    )
    try:
        status, body = _post(port, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
    finally:
        srv.shutdown()

    assert status == 200
    assert json.loads(body)["result"] == {"ok": True}
    assert json.loads(seen["raw"])["method"] == "ping"
    # The binding is read per request, so the listener sees live dispatch state rather
    # than whatever it was at bind time -- the skew path mutates it while running.
    assert seen["kwargs"]["version_state"] == "vs"


def test_token_header_lands_in_the_frame_for_serve_line_to_judge():
    """We package the token; `_serve_line` decides whether it is acceptable.

    Refusing here instead would give the two transports two different notions of a
    trusted caller, which is exactly what this module must not introduce.
    """
    seen = {}

    def fake_serve_line(raw, *, write, **kwargs):
        seen["frame"] = json.loads(raw)
        write(b"{}")

    srv, port, _t = http_listener.start(lambda: (fake_serve_line, {}))
    try:
        _post(port, {"method": "ping"}, token="tok-123")
    finally:
        srv.shutdown()

    assert seen["frame"]["_engine_token"] == "tok-123"


def test_absent_token_reaches_serve_line_without_one_rather_than_being_faked():
    seen = {}

    def fake_serve_line(raw, *, write, **kwargs):
        seen["frame"] = json.loads(raw)
        write(b"{}")

    srv, port, _t = http_listener.start(lambda: (fake_serve_line, {}))
    try:
        _post(port, {"method": "ping"})
    finally:
        srv.shutdown()

    assert "_engine_token" not in seen["frame"]


def test_dispatch_exception_becomes_500_not_a_hung_connection():
    """`_serve_line` promises never to raise; if that promise breaks the caller still
    gets an answer rather than a socket that never closes."""

    def boom(raw, *, write, **kwargs):
        raise RuntimeError("dispatch exploded")

    srv, port, _t = http_listener.start(lambda: (boom, {}))
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(port, {"method": "ping"})
        assert exc.value.code == 500
    finally:
        srv.shutdown()


def test_oversized_body_is_refused_before_it_reaches_the_frame_parser():
    called = []

    def fake_serve_line(raw, *, write, **kwargs):
        called.append(raw)
        write(b"{}")

    srv, port, _t = http_listener.start(lambda: (fake_serve_line, {}))
    try:
        body = b"x" * (http_listener.MAX_BODY_BYTES + 1)
        req = urllib.request.Request(
            "http://%s:%d/hook" % (http_listener.bind_host(), port),
            data=body,
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5.0)
        assert exc.value.code == 413
    finally:
        srv.shutdown()
    assert called == []


def test_concurrent_requests_are_served(monkeypatch):
    """Threading server: a slow request must not block a fast one, or the listener
    becomes a serialization point for every hook on the box."""
    barrier = threading.Barrier(2, timeout=10)

    def fake_serve_line(raw, *, write, **kwargs):
        barrier.wait()
        write(b'{"ok":true}')

    srv, port, _t = http_listener.start(lambda: (fake_serve_line, {}))
    results = []

    def worker():
        results.append(_post(port, {"method": "ping"})[0])

    threads = [threading.Thread(target=worker) for _ in range(2)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
    finally:
        srv.shutdown()

    assert results == [200, 200]
