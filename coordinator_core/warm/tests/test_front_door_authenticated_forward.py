"""AC17 at the door, and AC2 clause 1 -- the end-to-end fire->verdict path.

Spec backlink: `docs/plans/2026-08-25-the-bash-guard-stops-paying-for-a-process.md`
AC2/AC6/AC15/AC17, and the credential's own verdict record
`docs/research/spike-verdicts/2026-08-26-front-door-hook-fire-credential.md`.

WHY THIS FILE EXISTS SEPARATELY from `test_front_door.py`: that module's own
C7 meta-test audits every thread-driven test in ITSELF for the AC11
record-and-assert discipline. These tests stand up a real door AND a real
downstream listener -- two thread-backed servers per test -- and belong beside
each other rather than scattered into the election module's suite. The AC11
discipline is followed here identically: every server thread records what it
raises and every test asserts on the record.

AC2 CLAUSE 1 IS WHAT THIS MEASURES, and it could not be measured before: until
`do_POST` forwarded, `GET HEALTH_PATH` was the only round trip a door could
serve, which measures the door's own hop (clause 2, already pinned in
`test_front_door.py`) and NOT a fire reaching a verdict.
"""

from __future__ import annotations

import json
import os
import statistics
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, List, Optional, Tuple

import pytest

from coordinator_core.warm import (
    breadcrumb,
    door_credential,
    front_door,
    front_door_routing,
    skew,
    supervisor,
)

_BRIGHTLINE_MS = 500.0

#: The same 10% margin `test_front_door.py` holds its own hop to. A test that
#: passed at 499ms would be reporting the bar, not the mechanism.
_BRIGHTLINE_MARGIN_FACTOR = 0.1

_VERDICT = {"hookSpecificOutput": {"hookEventName": "PreToolUse"}, "continue": True}


def _fire_payload() -> bytes:
    return json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "git status --porcelain"},
        }
    ).encode("utf-8")


class _Recorder:
    """A thread-backed server plus the exception record AC11 requires."""

    def __init__(self, httpd: Any) -> None:
        self.httpd = httpd
        self.errors: List[BaseException] = []
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        try:
            self.httpd.serve_forever(poll_interval=0.02)
        except BaseException as exc:  # noqa: BLE001 -- AC11: record, never bury
            self.errors.append(exc)

    def __enter__(self) -> "_Recorder":
        self.thread.start()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.httpd.shutdown()
        self.thread.join(timeout=5)


def _listener(seen: List[bytes]) -> Tuple[Any, int]:
    """A stand-in for the resolved clone's own listener.

    Deliberately NOT a real `supervisor` server: what these tests assert is
    what the DOOR does -- whether it forwards at all, and what it forwards --
    and a real listener would put the guard's own evaluation cost inside a
    measurement whose subject is the door.
    """

    class _H(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass

        def do_POST(self) -> None:  # noqa: N802
            seen.append(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            body = json.dumps(_VERDICT).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    return httpd, httpd.server_address[1]


@pytest.fixture()
def door(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A real elected door on an ephemeral port, with a real secret behind it.

    `RUNTIME_BASE_ENV` isolates the whole warm base so the secret this test
    mints never lands in the operator's own directory.
    """
    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(tmp_path / "base"))
    (tmp_path / "base" / "coordinator" / "warm").mkdir(parents=True, exist_ok=True)
    secret = door_credential.ensure_secret()

    root = tmp_path / "clone"
    root.mkdir()
    skew.write_engine_stamp(root, "sha-door-auth-test")
    monkeypatch.setattr(front_door.skew, "compute_client_token", lambda r: "sha-door-auth-test")

    sock = front_door.elect_front_door(engine_root=root, port=0)

    class _NotYetBound:
        pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _NotYetBound, bind_and_activate=False)
    httpd.socket.close()
    httpd.socket = sock
    httpd.server_address = sock.getsockname()
    ctx = front_door._FrontDoorContext(httpd=httpd, engine_root=root)
    httpd.RequestHandlerClass = front_door._make_handler(ctx)
    return httpd, httpd.server_address[1], secret, root, ctx


def _start_epoch() -> int:
    """This process's real birth instant -- the second identity signal
    `stable_pid_alive` compares, so a recycled pid cannot read live."""
    import psutil

    return int(psutil.Process(os.getpid()).create_time())


def _publish(root: Path, port: int) -> None:
    """Vouch for a live listener at `port` for `root`.

    `pid` is this process deliberately: `discovery_is_live` compares against
    `stable_pid_alive`, so a fabricated pid would read dead and every test here
    would pass for the wrong reason -- `no_listener` instead of the state under
    test.
    """
    supervisor.write_discovery(
        port=port,
        pid=os.getpid(),
        stable_pid_start_epoch=_start_epoch(),
        engine_sha="sha-door-auth-test",
        engine_root=root,
    )


def _post(port: int, headers: dict, path: str = "/hook/warm_guard.evaluate") -> dict:
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", path, body=_fire_payload(), headers=headers)
    resp = conn.getresponse()
    payload = json.loads(resp.read().decode("utf-8"))
    conn.close()
    return payload


def _did_not_run(payload: dict) -> str:
    """The loud did-not-run shape's operator-facing sentence."""
    return payload.get("systemMessage", "")


def test_absent_credential_is_refused_and_never_forwarded(door) -> None:
    """AC17's core property: a fire the door cannot authenticate is answered
    as unroutable and the listener is never touched on its behalf."""
    httpd, port, _secret, root, _ctx = door
    seen: List[bytes] = []
    listener, lport = _listener(seen)
    _publish(root, lport)

    with _Recorder(httpd) as door_rec, _Recorder(listener) as lis_rec:
        payload = _post(
            port,
            {
                "Content-Type": "application/json",
                front_door_routing.CLONE_IDENTITY_HEADER: str(root),
            },
        )
    assert seen == [], "an unauthenticated fire reached the listener"
    assert "guard did not run" in _did_not_run(payload)
    assert "no door credential" in _did_not_run(payload)
    assert door_rec.errors == [] and lis_rec.errors == []


def test_wrong_credential_reports_a_DIFFERENT_fact_than_an_absent_one(door) -> None:
    """AC6's discipline applied to AC17: "this session never came through the
    launcher" and "something presented a key that is not ours" have different
    owners and different remediations. One message for both makes a
    misconfigured session indistinguishable from a forged fire."""
    httpd, port, _secret, root, _ctx = door
    seen: List[bytes] = []
    listener, lport = _listener(seen)
    _publish(root, lport)

    with _Recorder(httpd) as door_rec, _Recorder(listener) as lis_rec:
        payload = _post(
            port,
            {
                "Content-Type": "application/json",
                front_door_routing.CLONE_IDENTITY_HEADER: str(root),
                door_credential.CREDENTIAL_HEADER: "0" * 64,
            },
        )
    assert seen == []
    assert "does not match" in _did_not_run(payload)
    assert "no door credential" not in _did_not_run(payload)
    assert door_rec.errors == [] and lis_rec.errors == []


def test_authenticated_fire_reaches_the_listener_and_the_verdict_comes_back(door) -> None:
    """The positive path: authenticate, resolve, forward, and hand the
    listener's own answer back unchanged -- the door fabricates no verdict."""
    httpd, port, secret, root, _ctx = door
    seen: List[bytes] = []
    listener, lport = _listener(seen)
    _publish(root, lport)

    with _Recorder(httpd) as door_rec, _Recorder(listener) as lis_rec:
        payload = _post(
            port,
            {
                "Content-Type": "application/json",
                front_door_routing.CLONE_IDENTITY_HEADER: str(root),
                door_credential.CREDENTIAL_HEADER: secret,
            },
        )
    assert len(seen) == 1
    assert json.loads(seen[0])["tool_name"] == "Bash"
    assert payload == _VERDICT
    assert door_rec.errors == [] and lis_rec.errors == []


def test_a_door_holding_no_secret_authenticates_nobody(door, monkeypatch) -> None:
    """The failure DIRECTION that matters. A door that could not read its own
    secret must refuse everything, never accept everything."""
    httpd, port, secret, root, ctx = door
    ctx.door_key = None
    seen: List[bytes] = []
    listener, lport = _listener(seen)
    _publish(root, lport)

    with _Recorder(httpd) as door_rec, _Recorder(listener) as lis_rec:
        payload = _post(
            port,
            {
                "Content-Type": "application/json",
                front_door_routing.CLONE_IDENTITY_HEADER: str(root),
                door_credential.CREDENTIAL_HEADER: secret,
            },
        )
    assert seen == []
    assert "guard did not run" in _did_not_run(payload)
    assert door_rec.errors == [] and lis_rec.errors == []


def test_authenticated_but_unroutable_still_reports_the_routing_fact(door) -> None:
    """Authentication passing must not flatten AC6's routing states: a valid
    credential naming a clone with no live listener is still `no_listener`,
    not a credential problem and not a success."""
    httpd, port, secret, root, _ctx = door
    with _Recorder(httpd) as door_rec:
        payload = _post(
            port,
            {
                "Content-Type": "application/json",
                front_door_routing.CLONE_IDENTITY_HEADER: str(root),
                door_credential.CREDENTIAL_HEADER: secret,
            },
        )
    assert "no live listener" in _did_not_run(payload)
    assert door_rec.errors == []


def test_end_to_end_fire_to_verdict_under_the_brightline(door) -> None:
    """AC2 CLAUSE 1 -- the half that could not be measured until the door
    forwarded.

    PROCESS TIME, never wall clock (CLAUDE.md § Load norm): wall clock on this
    box measures ~50-70 peer sessions, not this path.

    MEASURED IN AGGREGATE, NOT PER SAMPLE, and that is not a stylistic choice.
    `time.process_time()` advances in scheduler ticks -- 15.625ms on Windows
    (64Hz). Timing ONE sub-millisecond fire against it reads 0.0000ms for
    almost every sample and exactly 15.6250ms for whichever one straddles a
    tick, so a per-sample `max()` reports the QUANTUM and never the mechanism.
    This test was written that way first and measured exactly that: n=20,
    median 0.0000ms, worst 15.6250ms -- a number carrying no information about
    the path under test. Summing across enough fires that the total dwarfs one
    tick is what makes the per-fire figure real, so the assertion is on the
    TOTAL against n budgets, never on a single sample.

    The same flaw is present in `test_front_door.py`'s clause-2 test, which
    predates this one; fixed there in the same commit for the same reason.
    """
    httpd, port, secret, root, _ctx = door
    seen: List[bytes] = []
    listener, lport = _listener(seen)
    _publish(root, lport)

    headers = {
        "Content-Type": "application/json",
        front_door_routing.CLONE_IDENTITY_HEADER: str(root),
        door_credential.CREDENTIAL_HEADER: secret,
    }
    with _Recorder(httpd) as door_rec, _Recorder(listener) as lis_rec:
        warmup = 5
        n = 200
        for _ in range(warmup):
            _post(port, headers)
        start = time.process_time()
        for _ in range(n):
            _post(port, headers)
        total_ms = (time.process_time() - start) * 1000.0

    assert len(seen) == n + warmup
    per_fire_ms = total_ms / n
    budget_ms = _BRIGHTLINE_MS * _BRIGHTLINE_MARGIN_FACTOR
    assert total_ms < budget_ms * n, (
        f"end-to-end fire->verdict process-time {per_fire_ms:.4f}ms per fire "
        f"({total_ms:.2f}ms total, n={n}) exceeds this test's "
        f"{_BRIGHTLINE_MARGIN_FACTOR:.0%} margin of the DR-344 brightline "
        f"({_BRIGHTLINE_MS}ms)"
    )
    assert total_ms > 0.0, (
        f"process time did not advance across n={n} fires -- the sample count "
        "is below this platform's clock tick and the measurement carries no "
        "information (see this test's docstring)"
    )
    assert door_rec.errors == [] and lis_rec.errors == []


def test_every_server_thread_in_this_module_records_and_asserts() -> None:
    """AC11 for this module, the same meta-discipline `test_front_door.py`
    applies to itself: a test whose failure cannot reach its assertion is
    worse than no test, because it is counted. Every server here runs through
    `_Recorder`, which is the single place that record is kept."""
    import inspect

    source = inspect.getsource(_Recorder)
    assert "self.errors.append(exc)" in source
    # Every construction site must sit in `_listener` or the `door` fixture,
    # the two places whose servers reach `_Recorder`. Asserted over each TEST
    # function's own source rather than by counting the module, because a
    # module-wide count would include this assertion's own string literal and
    # would then pass or fail for a reason unrelated to any server.
    for fn_name, fn in sorted(globals().items()):
        if not fn_name.startswith("test_") or not callable(fn):
            continue
        if fn_name == "test_every_server_thread_in_this_module_records_and_asserts":
            # This function carries the literal it is searching for.
            continue
        assert "ThreadingHTTPServer(" not in inspect.getsource(fn), (
            f"{fn_name} stands a server up directly, so its thread is not "
            "recorded by _Recorder"
        )
    assert inspect.getsource(_listener).count("ThreadingHTTPServer(") == 1
