"""A stop ask to the front door drains its in-flight relays before the door exits.

Real `ThreadingHTTPServer` on an ephemeral loopback port with the production
handler; only the relay hop, the credential and the process exit are faked.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from coordinator_core.warm import front_door, front_door_routing, lifecycle


class _NoDials:
    def record(self, **_kw):
        pass

    def flush(self):
        return True


@pytest.fixture(autouse=True)
def _clean_guard():
    lifecycle.reset_shutdown_guard_for_test()
    yield
    lifecycle.reset_shutdown_guard_for_test()


@pytest.fixture
def door(monkeypatch, tmp_path):
    monkeypatch.setattr(front_door._FrontDoorContext, "_boot_credential", staticmethod(lambda: "k"))
    monkeypatch.setattr(front_door.door_credential, "read_secret", lambda: "k")
    monkeypatch.setattr(
        front_door_routing,
        "resolve_route",
        lambda headers, front_door_root=None: front_door_routing.RouteResolution(
            state=front_door_routing.ROUTED, identity="x", engine_root=tmp_path
        ),
    )
    monkeypatch.setattr(lifecycle, "_DRAIN_POLL_INTERVAL_SECS", 0.01)

    exited = threading.Event()
    exit_codes: list = []

    def fake_exit(code):
        exit_codes.append(code)
        exited.set()

    class _Unbound:
        pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Unbound)
    ctx = front_door._FrontDoorContext(httpd=httpd, engine_root=tmp_path, exit_fn=fake_exit)
    ctx.dials = _NoDials()
    monkeypatch.setattr(ctx, "ctx_shutdown", lambda: None)
    httpd.RequestHandlerClass = front_door._make_handler(ctx)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    yield ctx, port, exited, exit_codes
    httpd.shutdown()
    httpd.server_close()


def _post(port, path, body=b"{}", timeout=10, key="k"):
    headers = {front_door.door_credential.CREDENTIAL_HEADER: key} if key else {}
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=body, method="POST", headers=headers
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def test_stop_ask_lets_in_flight_relay_finish_before_exit(door, monkeypatch):
    ctx, port, exited, exit_codes = door
    in_relay = threading.Event()
    release = threading.Event()

    def slow_forward(resolution, path, body, **_kw):
        in_relay.set()
        release.wait(10)
        return b'{"relayed": true}'

    monkeypatch.setattr(front_door_routing, "forward_request", slow_forward)

    relay_result: dict = {}

    def relay():
        relay_result["status"], relay_result["body"] = _post(port, "/hook")

    t = threading.Thread(target=relay)
    t.start()
    assert in_relay.wait(5)

    assert front_door.ask_front_door_to_stop({"port": port, "pid": os.getpid()}) is True

    assert not exited.wait(0.5), "door exited with a relay still in flight"
    release.set()
    t.join(10)
    assert relay_result == {"status": 200, "body": b'{"relayed": true}'}
    assert exited.wait(5)
    assert exit_codes == [0]


def test_stop_ask_with_nothing_in_flight_exits(door):
    _ctx, port, exited, _codes = door
    assert front_door.ask_front_door_to_stop({"port": port, "pid": os.getpid()}) is True
    assert exited.wait(5)


def test_stop_ask_naming_another_pid_is_refused_and_door_stays_up(door):
    _ctx, port, exited, _codes = door
    assert front_door.ask_front_door_to_stop({"port": port, "pid": 1}) is False
    assert not exited.wait(0.3)


def test_non_loopback_peer_is_refused(door, monkeypatch):
    ctx, port, exited, _codes = door
    monkeypatch.setattr(front_door, "_LOOPBACK_PEERS", ("10.0.0.9",))
    with pytest.raises(urllib.error.HTTPError) as err:
        _post(port, front_door.STOP_PATH, b"{}")
    assert err.value.code == 403
    assert not exited.wait(0.3)


@pytest.mark.parametrize("key", [None, "wrong"])
def test_stop_without_the_door_credential_is_refused(door, key):
    _ctx, port, exited, _codes = door
    body = json.dumps({"pid": os.getpid()}).encode()
    with pytest.raises(urllib.error.HTTPError) as err:
        _post(port, front_door.STOP_PATH, body, key=key)
    assert err.value.code == 403
    assert not exited.wait(0.3)


def test_ask_without_a_readable_secret_is_refused(door, monkeypatch):
    _ctx, port, exited, _codes = door
    monkeypatch.setattr(front_door.door_credential, "read_secret", lambda: None)
    assert front_door.ask_front_door_to_stop({"port": port, "pid": os.getpid()}) is False
    assert not exited.wait(0.3)


def test_ask_is_false_without_a_port_and_never_raises():
    assert front_door.ask_front_door_to_stop(None) is False
    assert front_door.ask_front_door_to_stop({"pid": 1}) is False

    def boom(*_a, **_kw):
        raise OSError("refused")

    assert front_door.ask_front_door_to_stop({"port": 1, "pid": 1}, opener=boom) is False


def test_ask_rejects_a_foreign_2xx_answer():
    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"stopping": True}).encode()

    assert (
        front_door.ask_front_door_to_stop({"port": 1, "pid": 1}, opener=lambda *a, **k: _Resp())
        is False
    )
