"""Falsifier: `stop_warm_pair` with default arguments lets an in-flight request finish and reply.

Each case spawns real warm child processes whose argv carries the tmp engine root's role script
(`coordinator_core/warm/server.py`, `front_door.py`), so `pair_stop.find_pair_processes` finds
them exactly as it finds production ones. The server child runs the real `_ServerContext`
accept/drain/evict path with only `_pool_dispatch` replaced by a slow op. The cases are red until
the stop drains before it terminates.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from coordinator_core import _engine_landing
from coordinator_core.warm import breadcrumb, door_credential, front_door, front_door_routing, pair_stop, supervisor

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SLOW_OP_SECS = 3.0
DOOR_RELAY_SECS = 1.2
_BOOT_DEADLINE_SECS = 90.0
_STOP_BOUND_SECS = 20.0
_REPLY_DEADLINE_SECS = 30.0

assert SLOW_OP_SECS > pair_stop._ASK_READ_DEADLINE_SECS
assert DOOR_RELAY_SECS < front_door_routing.PROXY_TIMEOUT_SECS

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_SERVER_CHILD = r'''
import os, sys, time
from pathlib import Path

repo, root, sleep_secs, stop_token = sys.argv[1], Path(sys.argv[2]), float(sys.argv[3]), sys.argv[4]
sys.path.insert(0, repo)
from coordinator_core.warm import breadcrumb, server


class _Version:
    server_sha = "drain-falsifier"
    last_skew_axes = ()

    def is_skewed(self, client_token):
        return client_token == stop_token

    def is_token_skewed(self, client_token):
        return self.is_skewed(client_token)


def _slow_dispatch(self, msg, *, caller=None, isolated=False):
    (root / "dispatch-entered.flag").write_text("1")
    time.sleep(sleep_secs)
    return {"jsonrpc": "2.0", "id": msg["id"], "result": "served"}


server._ServerContext._pool_dispatch = _slow_dispatch
on_windows = sys.platform == "win32"
token = "drain-falsifier-token"
elected = (
    server._elect_windows_pipe(root, token) if on_windows else server._elect_unix_socket_endpoint(root, token)
)
ctx = server._ServerContext(
    name=elected.endpoint,
    sid=elected.identity,
    version_state=_Version(),
    engine_root=root,
    listen_socket=elected.listen_socket,
    endpoint_path=elected.endpoint_path,
)
breadcrumb.write_breadcrumb(
    pipe=elected.endpoint,
    pid=os.getpid(),
    stable_pid_start_epoch=server._self_stable_pid_start_epoch() or 0,
    engine_sha=_Version.server_sha,
    engine_root=root,
    transport=breadcrumb.TRANSPORT_PIPE if on_windows else breadcrumb.TRANSPORT_UNIX,
)
if elected.first_handle is not None:
    ctx.serve_forever(elected.first_handle)
else:
    ctx.serve_forever_unix(elected.listen_socket)
'''

_DOOR_CHILD = r'''
import os, sys
from http.server import ThreadingHTTPServer
from pathlib import Path

repo, root = sys.argv[1], Path(sys.argv[2])
sys.path.insert(0, repo)
from coordinator_core.warm import front_door


class _Unbound:
    pass


httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Unbound)
ctx = front_door._FrontDoorContext(httpd=httpd, engine_root=root)
httpd.RequestHandlerClass = front_door._make_handler(ctx)
front_door.write_discovery(
    port=httpd.server_address[1],
    pid=os.getpid(),
    stable_pid_start_epoch=front_door._self_stable_pid_start_epoch() or 0,
    engine_sha=None,
    engine_root=root,
)
httpd.serve_forever()
'''


@pytest.fixture()
def engine_root(tmp_path: Path) -> Path:
    root = tmp_path / "eng"
    warm = root / "coordinator_core" / "warm"
    warm.mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "coordinator_core" / "_engine_stamp").write_text("sha:drain-falsifier\n", encoding="utf-8")
    return root


@pytest.fixture()
def spawned(engine_root: Path):
    """Popen handles to kill at teardown, plus a sweep of anything still matching the root."""
    procs: list[subprocess.Popen] = []
    yield procs
    import psutil

    for p in pair_stop.find_pair_processes(engine_root):
        try:
            psutil.Process(p.pid).kill()
        except psutil.Error:
            pass
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


def _repo_dir() -> str:
    import coordinator_core

    return str(Path(coordinator_core.__file__).resolve().parents[1])


def _spawn_role(engine_root: Path, script: str, body: str, *extra: str, spawned: list) -> subprocess.Popen:
    path = engine_root / "coordinator_core" / "warm" / script
    path.write_text(body, encoding="utf-8")
    log = open(engine_root / (script + ".log"), "wb")
    proc = subprocess.Popen(
        [sys.executable, str(path), _repo_dir(), str(engine_root), *extra],
        stdout=log,
        stderr=subprocess.STDOUT,
        creationflags=_NO_WINDOW,
    )
    spawned.append(proc)
    # Production pairs are detached and reaped by init; a child of this process stays a zombie after
    # it exits, and psutil still reports the zombie's birth instant, so the stop would wait out its grace.
    threading.Thread(target=proc.wait, daemon=True).start()
    return proc


def _wait_for(predicate, what: str, *, deadline_secs: float, proc: "subprocess.Popen | None" = None):
    deadline = time.monotonic() + deadline_secs
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        if proc is not None and proc.poll() is not None:
            pytest.fail(f"{what}: child exited with {proc.returncode} before it was ready")
        time.sleep(0.1)
    pytest.fail(f"{what}: not ready within {deadline_secs}s")


def _boot_server(engine_root: Path, spawned: list) -> dict:
    proc = _spawn_role(
        engine_root, "server.py", _SERVER_CHILD, str(SLOW_OP_SECS), pair_stop._STOP_REQUEST_TOKEN, spawned=spawned
    )
    return _wait_for(
        lambda: breadcrumb.read_breadcrumb(engine_root),
        "server breadcrumb",
        deadline_secs=_BOOT_DEADLINE_SECS,
        proc=proc,
    )


def _open_client(endpoint: str):
    if sys.platform == "win32":
        return open(endpoint, "r+b", buffering=0)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(_REPLY_DEADLINE_SECS)
    sock.connect(endpoint)
    return sock.makefile("rwb")


class _InFlightClient:
    """One JSON-RPC request on its own thread; `.line` is the raw reply (b"" on EOF) and `.error` a transport failure."""

    def __init__(self, endpoint: str) -> None:
        self.line: "bytes | None" = None
        self.error: "BaseException | None" = None
        self._endpoint = endpoint
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        frame = {"jsonrpc": "2.0", "id": "in-flight", "method": "ping", "params": {}, "_engine_token": "client-ok"}
        try:
            fh = _open_client(self._endpoint)
            try:
                fh.write(json.dumps(frame).encode("utf-8") + b"\n")
                fh.flush()
                self.line = fh.readline()
            finally:
                fh.close()
        except OSError as exc:
            self.error = exc

    def reply(self) -> dict:
        self._thread.join(_REPLY_DEADLINE_SECS)
        assert not self._thread.is_alive(), "the in-flight client never got an answer or an EOF"
        assert self.error is None, f"the in-flight client was cut by a transport failure: {self.error!r}"
        assert self.line, "the in-flight client read EOF instead of its reply"
        return json.loads(self.line)


class _Stop:
    def __init__(self, engine_root: Path) -> None:
        self.result: "pair_stop.PairStopResult | None" = None
        self.elapsed: "float | None" = None
        self._root = engine_root
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        began = time.monotonic()
        self.result = pair_stop.stop_warm_pair(self._root)
        self.elapsed = time.monotonic() - began

    def finish(self) -> pair_stop.PairStopResult:
        self._thread.join(_STOP_BOUND_SECS)
        assert not self._thread.is_alive(), (
            f"stop_warm_pair still running after {_STOP_BOUND_SECS}s: it waited out a grace "
            "or force-terminates instead of draining"
        )
        assert self.result is not None
        return self.result


def _stop_with_request_in_flight(engine_root: Path, spawned: list, *, swap_window: bool):
    crumb = _boot_server(engine_root, spawned)
    client = _InFlightClient(crumb["pipe"])
    client.start()
    _wait_for(
        lambda: (engine_root / "dispatch-entered.flag").exists(),
        "request reaching dispatch",
        deadline_secs=_REPLY_DEADLINE_SECS,
    )
    if swap_window:
        (engine_root / ".git" / _engine_landing.MARKER_NAME).write_text(
            json.dumps({"deadline_epoch": time.time() + 600}), encoding="utf-8"
        )
        assert _engine_landing.swap_in_progress(engine_root)
    stop = _Stop(engine_root)
    stop.start()
    reply = client.reply()
    result = stop.finish()
    return crumb, reply, result


def _assert_drained(crumb: dict, reply: dict, result: pair_stop.PairStopResult) -> None:
    assert reply == {"jsonrpc": "2.0", "id": "in-flight", "result": "served"}
    assert result.ok(), result.render_survivors()
    assert not pair_stop.stable_pid_alive(crumb["pid"], stored_start_epoch=str(crumb["stable_pid_start_epoch"]))


def test_server_in_flight_request_gets_its_reply_before_the_server_exits(engine_root, spawned) -> None:
    _assert_drained(*_stop_with_request_in_flight(engine_root, spawned, swap_window=False))


def test_server_in_flight_request_survives_stop_inside_a_publish_swap_window(engine_root, spawned) -> None:
    _assert_drained(*_stop_with_request_in_flight(engine_root, spawned, swap_window=True))


def _start_in_flight(engine_root: Path, spawned: list) -> "tuple[dict, _InFlightClient]":
    crumb = _boot_server(engine_root, spawned)
    client = _InFlightClient(crumb["pipe"])
    client.start()
    _wait_for(
        lambda: (engine_root / "dispatch-entered.flag").exists(),
        "request reaching dispatch",
        deadline_secs=_REPLY_DEADLINE_SECS,
    )
    return crumb, client


def _server_alive(crumb: dict) -> bool:
    return pair_stop.stable_pid_alive(crumb["pid"], stored_start_epoch=str(crumb["stable_pid_start_epoch"]))


def test_outstanding_read_counts_the_in_flight_request_and_evicts_nothing(engine_root, spawned) -> None:
    crumb, client = _start_in_flight(engine_root, spawned)

    count = pair_stop.read_server_outstanding(crumb["pipe"])

    assert count is not None and count >= 1
    assert _server_alive(crumb)
    assert client.reply() == {"jsonrpc": "2.0", "id": "in-flight", "result": "served"}
    assert _wait_for(
        lambda: pair_stop.read_server_outstanding(crumb["pipe"]) == 0,
        "outstanding count back to zero",
        deadline_secs=_REPLY_DEADLINE_SECS,
    )
    assert _server_alive(crumb)


def test_stop_reports_in_flight_before_it_signals_anything(engine_root, spawned) -> None:
    crumb, client = _start_in_flight(engine_root, spawned)
    reports: list = []

    def _on_in_flight(count) -> None:
        reports.append((count, _server_alive(crumb)))

    result = pair_stop.stop_warm_pair(engine_root, on_in_flight=_on_in_flight)

    assert len(reports) == 1
    count, alive_at_report = reports[0]
    assert count is not None and count >= 1
    assert alive_at_report
    assert result.in_flight == count
    _assert_drained(crumb, client.reply(), result)


def test_stop_reports_unknown_in_flight_when_no_server_answers(engine_root) -> None:
    reports: list = []

    result = pair_stop.stop_warm_pair(engine_root, on_in_flight=reports.append)

    assert reports == [None]
    assert result.in_flight is None
    assert result.ok()


def test_stop_without_a_report_callback_reads_nothing(engine_root, monkeypatch) -> None:
    def _no_read(_pipe):
        raise AssertionError("the outstanding read runs only for a caller that asked to report it")

    monkeypatch.setattr(pair_stop, "read_server_outstanding", _no_read)

    assert pair_stop.stop_warm_pair(engine_root).in_flight is None


def test_outstanding_read_uses_its_own_short_deadline(monkeypatch) -> None:
    seen: list = []

    def _fake(pipe, params, deadline_secs=None):
        seen.append(deadline_secs)
        return None

    monkeypatch.setattr(pair_stop, "_probe_exchange", _fake)

    assert pair_stop.read_server_outstanding("pipe") is None
    assert seen == [pair_stop._OUTSTANDING_READ_DEADLINE_SECS]
    assert pair_stop._OUTSTANDING_READ_DEADLINE_SECS <= 0.5 < pair_stop._ASK_READ_DEADLINE_SECS


def _stamped_root(tmp_path: Path, stamp: "bytes | None") -> Path:
    root = tmp_path / "restart-root"
    (root / "coordinator_core").mkdir(parents=True)
    if stamp is not None:
        (root / "coordinator_core" / "_engine_stamp").write_bytes(stamp)
    return root


def test_rotated_stamp_keeps_sha_prefix_and_is_idempotent_in_shape() -> None:
    first = pair_stop._rotated_stamp(b"sha:abc123\n")
    second = pair_stop._rotated_stamp(first)

    assert first.startswith(b"sha:abc123+restart-") and first.endswith(b"\n")
    assert second.startswith(b"sha:abc123+restart-") and second.count(b"+restart-") == 1
    assert first != second


def test_restart_without_a_stamp_is_unsupported_and_spawns_nothing(tmp_path) -> None:
    def _boom(*_a, **_k):
        raise AssertionError("no successor may start for an unstamped root")

    result = pair_stop.restart_warm_pair(_stamped_root(tmp_path, None), start_successor=_boom)

    assert result.outcome == pair_stop.RESTART_UNSUPPORTED
    assert not result.ok()


def test_restart_with_nothing_running_spawns_nothing(tmp_path) -> None:
    def _boom(*_a, **_k):
        raise AssertionError("nothing running: the next hook starts the engine")

    result = pair_stop.restart_warm_pair(_stamped_root(tmp_path, b"sha:abc\n"), start_successor=_boom)

    assert result.outcome == pair_stop.RESTART_NO_ENGINE
    assert result.ok()


@pytest.mark.parametrize(
    ("answered", "outcome", "ok"),
    [(True, pair_stop.RESTART_RESTARTED, True), (False, pair_stop.RESTART_NOT_ANSWERING, False)],
)
def test_restart_hands_a_rotated_stamp_to_the_successor(tmp_path, monkeypatch, answered, outcome, ok) -> None:
    root = _stamped_root(tmp_path, b"sha:abc\n")
    monkeypatch.setattr(pair_stop, "find_pair_processes", lambda *_a, **_k: [object()])
    calls: list = []

    def _start(r, *, err=None, old_stamp, new_stamp):
        calls.append((r, old_stamp, new_stamp))
        return answered

    result = pair_stop.restart_warm_pair(root, start_successor=_start)

    assert result.outcome == outcome and result.ok() is ok
    assert len(calls) == 1
    _root, old, new = calls[0]
    assert old == b"sha:abc\n" and new != old and new.startswith(b"sha:abc+restart-")


class _SlowListener(BaseHTTPRequestHandler):
    received: threading.Event

    def log_message(self, format, *args) -> None:  # noqa: A002
        pass

    def do_POST(self) -> None:  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        type(self).received.set()
        time.sleep(DOOR_RELAY_SECS)
        body = b'{"relayed":"ok"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_request_relayed_through_the_front_door_survives_stop(engine_root, spawned) -> None:
    from coordinator_core.session.core import _win_create_time_epoch

    received = threading.Event()
    handler = type("_Listener", (_SlowListener,), {"received": received})
    listener = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=listener.serve_forever, daemon=True).start()
    # The recorded supervisor is a bystander process: stop_warm_pair terminates whatever pid the
    # record names, and the listener must outlive that so only the door's own handling is under test.
    bystander = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"], creationflags=_NO_WINDOW
    )
    spawned.append(bystander)
    try:
        supervisor.write_discovery(
            port=listener.server_address[1],
            pid=bystander.pid,
            stable_pid_start_epoch=_win_create_time_epoch(bystander.pid),
            engine_sha=None,
            engine_root=engine_root,
        )
        door_key = door_credential.ensure_secret()
        door = _spawn_role(engine_root, "front_door.py", _DOOR_CHILD, spawned=spawned)
        record = _wait_for(
            lambda: front_door.read_discovery(engine_root),
            "front door discovery",
            deadline_secs=_BOOT_DEADLINE_SECS,
            proc=door,
        )

        outcome: dict = {}

        def _post() -> None:
            try:
                conn = http.client.HTTPConnection("127.0.0.1", record["port"], timeout=_REPLY_DEADLINE_SECS)
                conn.request(
                    "POST",
                    "/hook",
                    body=b"{}",
                    headers={
                        door_credential.CREDENTIAL_HEADER: door_key,
                        front_door_routing.CLONE_IDENTITY_HEADER: str(engine_root),
                        "Content-Type": "application/json",
                    },
                )
                resp = conn.getresponse()
                outcome["status"] = resp.status
                outcome["body"] = resp.read()
            except (OSError, http.client.HTTPException) as exc:
                outcome["error"] = exc

        client = threading.Thread(target=_post, daemon=True)
        client.start()
        assert received.wait(_REPLY_DEADLINE_SECS), "the relayed request never reached the listener"
        stop = _Stop(engine_root)
        stop.start()
        client.join(_REPLY_DEADLINE_SECS)
        result = stop.finish()
    finally:
        listener.shutdown()

    assert "error" not in outcome, f"the relayed request was cut at the door: {outcome.get('error')!r}"
    assert outcome == {"status": 200, "body": b'{"relayed":"ok"}'}
    assert result.ok(), result.render_survivors()
