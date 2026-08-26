"""The listener validates `Host` once, centrally, against loopback literals.

Spec backlink: docs/plans/2026-08-26-the-loopback-listener-gets-a-credential.md § C2 (AC5/AC6).

WHY HOST AND NOT AN ORIGIN ALLOWLIST -- the reasoning this file exists to pin,
because the cheaper-looking option is the one that rots. Origin validation is a
PER-HANDLER discipline: it must be re-applied on every route and every upgrade
path, and it lapses silently when someone adds one. Vite shipped Origin checks
on its HTTP path and not on its WebSocket upgrade TWICE (CVE-2025-24010,
CVE-2026-39363) -- in both cases the check was never RUN, so its logic was
irrelevant. `Host` is on every HTTP request line, WS handshakes included, so it
is validated ONCE before routing and cannot lapse as the code grows.

WHAT AC5 ACTUALLY RESOLVED TO. The plan required observing the harness's real
`type: "http"` hook client before pinning, because pinning against a guessed
value would fail closed on the hook hot path for every session on the box.
Enumerated instead of assumed: **there are NO `type: "http"` hook registrations
at all** -- zero across both `~/.claude` settings files and 190 scanned config
files, project and plugin included. `supervisor.py`'s own module docstring says
so from the other direction: pointing `hooks.json`'s `type: "http"` entries at
the discovered port is named there as a follow-up chunk that has not happened.

So there is no harness client to observe, and the feared breakage is not live.
Every client that reaches this listener today derives its authority from
`supervisor.listener_url` (`http://127.0.0.1:<port>`) -- `check_health`, the
benchmarks, and curl, whose `Host: 127.0.0.1:<port>` was captured directly.
`_pinned_hosts` is derived from that same function precisely so the authority
published and the authority accepted cannot drift apart.

**This is defence-in-depth, not authentication.** It stops a page the operator
visits and it stops a DNS-rebound name -- rebinding changes where the socket
lands, never the `Host` a browser writes. The credential is the load-bearing
control and is a separate chunk.
"""

import json
import socket
import threading
import urllib.error
import urllib.request

import pytest

from coordinator_core.warm import supervisor


# --------------------------------------------------------------------------
# The accepted set itself.
# --------------------------------------------------------------------------


def test_pinned_hosts_are_exactly_the_two_loopback_spellings():
    assert supervisor._pinned_hosts(58894) == {"127.0.0.1:58894", "localhost:58894"}


def test_pinned_hosts_tracks_the_published_url():
    """The pin is derived from `listener_url`, so the authority this listener
    publishes is always one it will accept. A drift here is the failure this
    derivation exists to prevent."""
    port = 4321
    published = supervisor.listener_url({"port": port})
    authority = published.split("//", 1)[1]
    assert authority in supervisor._pinned_hosts(port)


@pytest.mark.parametrize(
    "hostile",
    [
        "attacker.test",
        "attacker.test:58894",
        "evil.com",
        # A BARE IP LITERAL. webpack-dev-server's CVE-2025-30360 accepted any
        # IP-literal as "local", which an attacker's own IP satisfies.
        "10.0.0.5:58894",
        "127.0.0.1",  # right host, no port -- not the published authority
        # SUFFIX MATCH -- the case a naive `startswith`/`in` test would pass.
        "127.0.0.1:58894.evil.com",
        # PREFIX MATCH -- the case a naive `endswith` test would pass.
        "evil.com:127.0.0.1:58894",
        "localhost:1",  # right name, wrong port
    ],
)
def test_hostile_hosts_are_not_in_the_pinned_set(hostile):
    assert hostile not in supervisor._pinned_hosts(58894)


# --------------------------------------------------------------------------
# End-to-end against a real bound listener -- the check must fire BEFORE
# routing, which only a live request can demonstrate.
# --------------------------------------------------------------------------


@pytest.fixture()
def live_listener():
    """A real `_Handler` on a real loopback socket.

    Uses the module's own handler factory so the test exercises the shipped
    `parse_request` override rather than a reimplementation of it.
    """
    from http.server import ThreadingHTTPServer

    class _Ctx:
        engine_token = "testtoken0000000"
        in_flight = None

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), supervisor._make_handler(_Ctx()))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()


def _get_health(port, host_header):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{supervisor.HEALTH_PATH}")
    if host_header is not None:
        req.add_header("Host", host_header)
    return urllib.request.urlopen(req, timeout=5)


def test_the_published_authority_is_served(live_listener):
    """Positive control. Without this, a pin that refuses EVERYTHING would
    pass every negative test below and this file would prove nothing."""
    with _get_health(live_listener, f"127.0.0.1:{live_listener}") as resp:
        assert resp.status == 200
        assert resp.read() == b"ok"


def test_localhost_spelling_is_served(live_listener):
    with _get_health(live_listener, f"localhost:{live_listener}") as resp:
        assert resp.status == 200


def test_a_foreign_host_is_refused_before_routing(live_listener):
    """421, and critically NOT 404 -- a 404 would mean the request reached
    stdlib's routing and was rejected by a `do_*` method instead."""
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _get_health(live_listener, "attacker.test")
    assert excinfo.value.code == 421


def test_a_rebinding_shaped_host_is_refused(live_listener):
    """The DNS-rebinding shape: the socket lands on loopback, but the browser
    writes the attacker's own registered name into `Host`."""
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _get_health(live_listener, f"127.0.0.1:{live_listener}.evil.com")
    assert excinfo.value.code == 421


def test_the_hook_path_is_refused_on_a_foreign_host(live_listener):
    """The pin is central, so it covers POST /hook without /hook knowing it
    exists -- the whole reason it lives in `parse_request`."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{live_listener}{supervisor.HOOK_PATH}",
        data=json.dumps({"hook_event_name": "PreToolUse"}).encode(),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Host", "attacker.test")
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(req, timeout=5)
    assert excinfo.value.code == 421


# --------------------------------------------------------------------------
# The header shapes urllib cannot produce: absent, repeated, oddly cased.
# Raw sockets, because urllib always writes exactly one canonical `Host`.
# --------------------------------------------------------------------------

CRLF = "\r\n"


def _raw_request(port, request_lines):
    """Send a literal request and return its status line. `request_lines` is
    the request line plus headers -- the blank terminator is appended here."""
    raw = (CRLF.join(request_lines) + CRLF + CRLF).encode("ascii")
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(raw)
        chunks = []
        while CRLF.encode("ascii") not in b"".join(chunks):
            piece = sock.recv(4096)
            if not piece:
                break
            chunks.append(piece)
    return b"".join(chunks).split(CRLF.encode("ascii"), 1)[0].decode("latin-1")


def test_an_uppercase_host_spelling_is_served(live_listener):
    """Hostnames are case-insensitive (RFC 9110). Refusing an authority this
    listener genuinely publishes, on case alone, would be a false refusal."""
    status = _raw_request(
        live_listener,
        [f"GET {supervisor.HEALTH_PATH} HTTP/1.1", f"Host: LOCALHOST:{live_listener}"],
    )
    assert "200" in status


def test_a_repeated_host_is_refused_even_when_one_value_is_pinned(live_listener):
    """Request-smuggling shape. Reading only the first `Host` would serve this;
    the pin must refuse rather than pick a value a downstream reader might
    disagree with."""
    status = _raw_request(
        live_listener,
        [
            f"GET {supervisor.HEALTH_PATH} HTTP/1.1",
            f"Host: 127.0.0.1:{live_listener}",
            "Host: attacker.test",
        ],
    )
    assert "421" in status


def test_an_absent_host_on_http_1_1_is_refused(live_listener):
    status = _raw_request(live_listener, [f"GET {supervisor.HEALTH_PATH} HTTP/1.1"])
    assert "421" in status


def test_an_absent_host_on_http_1_0_is_deliberately_served(live_listener):
    """PINNING A DECISION, NOT AN OVERSIGHT. HTTP/1.0 does not require `Host`,
    and the pin's threat model is a browser -- every browser sends one, so a
    Host-less HTTP/1.0 request cannot be the attack this check exists to stop.
    Tightening it to a refusal is a real option, but it must be a decision
    someone makes on purpose, with this test as the thing they change."""
    status = _raw_request(live_listener, [f"GET {supervisor.HEALTH_PATH} HTTP/1.0"])
    assert "200" in status
