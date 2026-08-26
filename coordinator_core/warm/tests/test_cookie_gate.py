"""The listener REQUIRES the boot cookie, and refusing never evicts.

Spec backlink: docs/plans/2026-08-26-the-loopback-listener-gets-a-credential.md § AC4,
against docs/research/spike-verdicts/2026-08-26-loopback-op-dispatch-credential-shape.md
§ Refusal semantics row 1 (cookie absent, unreadable, or wrong -> refuse the caller,
401-shaped, fail-closed; never evict; never fall through to serving).

WHY THIS FILE EXISTS SEPARATELY FROM THE SKEW REFUSAL. § Refusal semantics is the single
home for what the HTTP path does with a bad credential AND with a skewed caller, and
sharing a spec section is not sharing an implementation. The cookie check answers WHO IS
CALLING and lives in `parse_request`, at the same chokepoint as the Host pin. The skew
refusal answers WHICH ENGINE GENERATION the caller thinks it is dialling, lives in
`do_POST`, and is coupled to the self-stamping line a peer baton removes. Reading the
shared section as a shared dependency is what kept this half unbuilt.

THE PROPERTY THAT MATTERS MOST HERE IS THE SECOND ONE. Any gate can return 401. The
failure this workstream exists to prevent is a refusal that ALSO takes the engine down --
a drain, a close, an exit -- which is an eviction spelt differently and costs every
session on the box (measured at 16.8s under a 17s drain). Every refusal test below is
paired with a still-serving assertion for that reason.
"""

import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from coordinator_core.warm import cookie, supervisor


class _Ctx:
    """Minimal stand-in for `_ServerContext`: the cookie gate reads only
    `engine_root`, and binding a real context would drag in election and
    skew state this file has no use for."""

    in_flight = None

    def __init__(self, engine_root: Path) -> None:
        self.engine_root = engine_root
        self.engine_token = "testtoken0000000"


@pytest.fixture
def live_listener(tmp_path):
    """A real listener over a real cookie, on a real socket. Yields
    `(port, token)`."""
    token = cookie.ensure(tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), supervisor._make_handler(_Ctx(tmp_path)))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1], token
    finally:
        httpd.shutdown()
        httpd.server_close()


def _request(port, path, headers=None, method="POST"):
    """Raw socket, because the header shapes under test (absent, repeated)
    are ones urllib will not produce. Returns the status line."""
    crlf = "\r\n"
    lines = [f"{method} {path} HTTP/1.1", f"Host: 127.0.0.1:{port}"]
    lines.extend(headers or [])
    lines.append("Content-Length: 0")
    raw = (crlf.join(lines) + crlf + crlf).encode("ascii")
    import socket

    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(raw)
        chunks = []
        while crlf.encode("ascii") not in b"".join(chunks):
            piece = sock.recv(4096)
            if not piece:
                break
            chunks.append(piece)
    return b"".join(chunks).split(crlf.encode("ascii"), 1)[0].decode("latin-1")


def _still_serving(port, token) -> bool:
    """The assertion that separates a refusal from an eviction."""
    status = _request(
        port,
        supervisor.HEALTH_PATH,
        [f"{cookie.COOKIE_HEADER}: {token}"],
        method="GET",
    )
    return "200" in status


def test_a_good_cookie_passes_the_gate(live_listener):
    """POSITIVE CONTROL, AND IT HAS TO BE SHARP. Without one, a gate that
    refused EVERYTHING would pass every negative test below and this file
    would prove nothing.

    An unrouted path is the cleanest probe: with a good cookie it reaches
    routing and 404s, without one it never gets there and 401s. Same
    request, same path, one header apart -- so the difference isolates the
    gate rather than anything downstream of it."""
    port, token = live_listener
    passed = _request(
        port, "/not-a-route", [f"{cookie.COOKIE_HEADER}: {token}"]
    )
    refused = _request(port, "/not-a-route")
    assert "404" in passed, "a good cookie must reach routing"
    assert "401" in refused, "no cookie must not reach routing"


def test_no_cookie_is_refused(live_listener):
    port, token = live_listener
    status = _request(port, supervisor.HOOK_PATH)
    assert "401" in status
    assert _still_serving(port, token), "a refusal must not stop the listener serving"


def test_a_wrong_cookie_is_refused(live_listener):
    port, token = live_listener
    status = _request(
        port, supervisor.HOOK_PATH, [f"{cookie.COOKIE_HEADER}: {'0' * 64}"]
    )
    assert "401" in status
    assert _still_serving(port, token)


def test_a_repeated_cookie_header_is_refused(live_listener):
    """Smuggling shape, same treatment the Host pin gives a repeated Host:
    refuse rather than pick a value a downstream reader might disagree with."""
    port, token = live_listener
    status = _request(
        port,
        supervisor.HOOK_PATH,
        [f"{cookie.COOKIE_HEADER}: {token}", f"{cookie.COOKIE_HEADER}: {'0' * 64}"],
    )
    assert "401" in status
    assert _still_serving(port, token)


def test_the_listener_survives_a_burst_of_refusals(live_listener):
    """THE EVICTION-BY-ANOTHER-NAME CHECK, REPEATED. One refusal leaving the
    listener up could be luck; a run of them establishes that refusal is not
    wired to any drain or close path."""
    port, token = live_listener
    for _ in range(12):
        assert "401" in _request(port, supervisor.HOOK_PATH)
    assert _still_serving(port, token)


def test_health_is_exempt_and_that_exemption_is_narrow(live_listener):
    """`check_health` runs before a caller has reason to have read the
    cookie, and `/health` returns a fixed literal. The exemption is one
    path, one method: the same path under POST is NOT exempt."""
    port, _token = live_listener
    assert "200" in _request(port, supervisor.HEALTH_PATH, method="GET")
    assert "401" in _request(port, supervisor.HEALTH_PATH, method="POST")


def test_an_unreadable_expected_cookie_refuses_every_caller(live_listener, tmp_path):
    """FAIL CLOSED AFTER BOOT, TOO. `_assert_credential_ready` covers boot;
    this covers the cookie being removed under a running listener. The
    listener must refuse rather than admit, and must still be serving."""
    port, token = live_listener
    cookie.cookie_path(tmp_path).unlink()
    try:
        assert "401" in _request(
            port, supervisor.HOOK_PATH, [f"{cookie.COOKIE_HEADER}: {token}"]
        )
    finally:
        cookie.mint(tmp_path)


def test_the_gate_is_not_defeated_by_an_unrouted_path(live_listener):
    """The check is in `parse_request`, ahead of routing, so it covers a
    path no `do_*` method serves -- the lapse an add-a-handler change would
    otherwise introduce."""
    port, _token = live_listener
    assert "401" in _request(port, "/not-a-route")
