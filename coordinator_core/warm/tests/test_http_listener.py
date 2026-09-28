
from __future__ import annotations

import json
import threading
import time
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
    assert seen["kwargs"]["version_state"] == "vs"


def test_token_header_lands_in_the_frame_for_serve_line_to_judge():
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


# ---------------------------------------------------------------------------
# _engine_link_has_moved -- the stale-binding self-check
# ---------------------------------------------------------------------------


def test_engine_link_absent_at_start_never_reports_a_move(monkeypatch):
    monkeypatch.setattr(http_listener, "_link_realpath_at_start", None, raising=False)
    assert http_listener._engine_link_has_moved() is False


def test_engine_link_unchanged_since_start_does_not_report_a_move(monkeypatch, tmp_path):
    target = tmp_path / "klabauter"
    target.mkdir()
    link = tmp_path / "engine-current"
    link.symlink_to(target)
    monkeypatch.setattr(http_listener, "_ENGINE_LINK_PATH", str(link), raising=False)
    monkeypatch.setattr(
        http_listener, "_link_realpath_at_start", str(target.resolve()), raising=False
    )
    assert http_listener._engine_link_has_moved() is False


def test_engine_link_repointed_since_start_reports_a_move(monkeypatch, tmp_path):
    old_target = tmp_path / "klabauter"
    old_target.mkdir()
    new_target = tmp_path / "claude-klabauter"
    new_target.mkdir()
    link = tmp_path / "engine-current"
    link.symlink_to(old_target)
    monkeypatch.setattr(http_listener, "_ENGINE_LINK_PATH", str(link), raising=False)
    monkeypatch.setattr(
        http_listener, "_link_realpath_at_start", str(old_target.resolve()), raising=False
    )

    link_tmp = link.with_name(link.name + ".tmp")
    link_tmp.symlink_to(new_target)
    link_tmp.replace(link)

    assert http_listener._engine_link_has_moved() is True


def test_engine_link_missing_after_start_reports_a_move(monkeypatch, tmp_path):
    """A link that existed at start and is later removed/broken IS evidence of a re-point --
    only "never existed at bind time" is the no-op case (see `_engine_link_has_moved`'s own
    docstring). Serving forever off a since-deleted tree is exactly the defect this closes."""
    link = tmp_path / "engine-current"
    monkeypatch.setattr(http_listener, "_ENGINE_LINK_PATH", str(link), raising=False)
    monkeypatch.setattr(
        http_listener, "_link_realpath_at_start", str(tmp_path / "klabauter"), raising=False
    )
    assert http_listener._engine_link_has_moved() is True


def test_do_post_answers_then_hands_off_to_recycle_when_link_has_moved(monkeypatch):
    """The request thread itself must never exit/exec -- it answers 503 and delegates to
    `server.request_recycle()`, a dedicated non-request thread. `os._exit`/`os.execv` are not
    even reachable from `do_POST` any more; this pins that the moved-link branch never touches
    `serve_binding()`/dispatch and never calls exit itself."""
    monkeypatch.setattr(http_listener, "_engine_link_has_moved", lambda: True)
    calls = []

    handler = http_listener._Handler.__new__(http_listener._Handler)
    monkeypatch.setattr(handler, "_respond", lambda status, body: calls.append(("respond", status)))
    monkeypatch.setattr(
        handler,
        "server",
        type(
            "S",
            (),
            {
                "serve_binding": lambda self: (_ for _ in ()).throw(AssertionError("dispatched")),
                "request_recycle": lambda self: calls.append("recycled"),
            },
        )(),
        raising=False,
    )

    handler.do_POST()

    assert calls == [("respond", 503), "recycled"]


def test_concurrent_in_flight_requests_complete_across_a_recycle(monkeypatch):
    """The concrete EM-directed scenario: two requests already in flight when a recycle is
    triggered. Both in-flight requests must complete normally -- recycling must not exit/exec
    out from under them mid-response."""
    release = threading.Event()
    exit_calls = []
    monkeypatch.setattr(http_listener.os, "_exit", lambda code: exit_calls.append(code))

    def fake_serve_line(raw, *, write, **kwargs):
        release.wait(timeout=10)
        write(b'{"ok":true}')

    srv, port, _t = http_listener.start(lambda: (fake_serve_line, {}))
    results = []

    def worker():
        results.append(_post(port, {"method": "ping"})[0])

    threads = [threading.Thread(target=worker) for _ in range(2)]
    try:
        for t in threads:
            t.start()
        # Wait until both are past the moved-check and inside dispatch (in_flight == 2), and
        # both blocked on `release` -- so the window in which a recycle would race them is wide.
        deadline = time.monotonic() + 5.0
        while srv._in_flight < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert srv._in_flight == 2

        srv.request_recycle()
        time.sleep(0.1)
        assert exit_calls == []  # still draining behind the two in-flight requests

        release.set()
        for t in threads:
            t.join(timeout=15)
    finally:
        try:
            srv.shutdown()
        except Exception:
            pass

    assert results == [200, 200]


def test_request_recycle_drains_in_flight_before_exiting(monkeypatch):
    """`request_recycle` must not exit while a request `enter_request()`ed is still running --
    the concurrent-in-flight case the P1 finding named. Two requests: both complete."""
    exit_calls = []
    monkeypatch.setattr(http_listener.os, "_exit", lambda code: exit_calls.append(code))

    srv, port, _t = http_listener.start(lambda: (lambda raw, *, write, **kw: write(b"{}"), {}))
    try:
        srv.enter_request()
        srv.enter_request()
        srv.request_recycle()
        # Give the recycle worker a moment to reach the drain loop.
        time.sleep(0.1)
        assert exit_calls == []  # still draining -- two requests remain in flight

        srv.exit_request()
        time.sleep(0.1)
        assert exit_calls == []  # one still in flight

        srv.exit_request()
        deadline = time.monotonic() + 5.0
        while not exit_calls and time.monotonic() < deadline:
            time.sleep(0.01)
        assert exit_calls == [1]
    finally:
        try:
            srv.shutdown()
        except Exception:
            pass
