"""Tests for `coordinator_core.warm.supervisor` -- the http route's
supervisor guarantee (AC10b).

Spec backlink: docs/plans/2026-08-19-the-fired-path-reaches-the-engine.md
§ C11. Mirrors `coordinator_core.warm.tests.test_breadcrumb_and_spawn`'s own
conventions for the sibling breadcrumb module (`tmp_path` as `engine_root`,
`monkeypatch` for injectable seams, no real process ever spawned or killed)
plus `coordinator_core.warm.tests.test_election`'s Windows-only gating for
anything that touches `election.elect`/`current_user_sid` directly.

Nothing here opens a real Windows named pipe or spawns a real detached
process -- `ensure_listener`'s spawn trigger is exercised through the
injectable `spawn_detached` module-level name, mirroring `warm.client`'s own
`test_client_fallback.py` convention. `check_health` IS exercised against a
real loopback socket for the "listener genuinely answers" path (cheap,
deterministic, no external network), and separately against an injected
`opener` for the failure-shape tests.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from coordinator_core.warm import skew, supervisor

pytestmark_win = pytest.mark.skipif(sys.platform != "win32", reason="election.elect is Windows-only")


# ---------------------------------------------------------------------------
# discovery_path / write_discovery / read_discovery / unlink_discovery
# ---------------------------------------------------------------------------


def test_discovery_path_lives_under_svc_dir_not_the_pipe_breadcrumb(tmp_path: Path) -> None:
    from coordinator_core.warm import breadcrumb

    path = supervisor.discovery_path(tmp_path)
    assert path.parent == breadcrumb.svc_dir(tmp_path)
    assert path.name == supervisor.DISCOVERY_FILENAME
    assert path != breadcrumb.breadcrumb_path(tmp_path)


def test_read_discovery_missing_returns_none(tmp_path: Path) -> None:
    assert supervisor.read_discovery(tmp_path) is None


def test_write_then_read_roundtrips(tmp_path: Path) -> None:
    supervisor.write_discovery(
        port=8934,
        pid=4242,
        stable_pid_start_epoch=1234567890,
        engine_sha="deadbeef",
        engine_root=tmp_path,
    )
    record = supervisor.read_discovery(tmp_path)
    assert record is not None
    assert record["port"] == 8934
    assert record["pid"] == 4242
    assert record["stable_pid_start_epoch"] == 1234567890
    assert record["engine_sha"] == "deadbeef"
    assert record["health_path"] == supervisor.HEALTH_PATH
    assert record["hook_path"] == supervisor.HOOK_PATH
    assert isinstance(record["started_at"], str)

    on_disk = json.loads(supervisor.discovery_path(tmp_path).read_text(encoding="utf-8"))
    assert on_disk == record


def test_read_discovery_corrupt_json_returns_none(tmp_path: Path) -> None:
    path = supervisor.discovery_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json{{{", encoding="utf-8")
    assert supervisor.read_discovery(tmp_path) is None


def test_read_discovery_non_object_json_returns_none(tmp_path: Path) -> None:
    path = supervisor.discovery_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert supervisor.read_discovery(tmp_path) is None


def test_unlink_discovery_removes_file(tmp_path: Path) -> None:
    supervisor.write_discovery(
        port=1, pid=1, stable_pid_start_epoch=1, engine_sha=None, engine_root=tmp_path
    )
    assert supervisor.discovery_path(tmp_path).exists()
    supervisor.unlink_discovery(tmp_path)
    assert not supervisor.discovery_path(tmp_path).exists()


def test_unlink_discovery_missing_is_a_noop(tmp_path: Path) -> None:
    supervisor.unlink_discovery(tmp_path)  # must not raise


# ---------------------------------------------------------------------------
# discovery_is_live
# ---------------------------------------------------------------------------


def test_discovery_is_live_true_when_pid_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(supervisor, "stable_pid_alive", lambda pid, stored_start_epoch="": True)
    assert supervisor.discovery_is_live({"pid": 999, "stable_pid_start_epoch": 111}) is True


def test_discovery_is_live_false_when_pid_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(supervisor, "stable_pid_alive", lambda pid, stored_start_epoch="": False)
    assert supervisor.discovery_is_live({"pid": 999, "stable_pid_start_epoch": 111}) is False


def test_discovery_is_live_false_on_missing_pid() -> None:
    assert supervisor.discovery_is_live({}) is False


def test_discovery_is_live_false_on_stable_pid_alive_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(pid, stored_start_epoch=""):
        raise RuntimeError("psutil is not installed")

    monkeypatch.setattr(supervisor, "stable_pid_alive", _raise)
    assert supervisor.discovery_is_live({"pid": 999, "stable_pid_start_epoch": 111}) is False


# ---------------------------------------------------------------------------
# should_spawn
# ---------------------------------------------------------------------------


def test_should_spawn_true_when_no_discovery(tmp_path: Path) -> None:
    assert supervisor.should_spawn(tmp_path) is True


def _write_started_at(tmp_path: Path, *, pid: int, stable_epoch: int, started_at: str) -> None:
    path = supervisor.discovery_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "port": 1,
        "pid": pid,
        "stable_pid_start_epoch": stable_epoch,
        "engine_sha": "x",
        "started_at": started_at,
        "health_path": supervisor.HEALTH_PATH,
        "hook_path": supervisor.HOOK_PATH,
    }
    path.write_text(json.dumps(record), encoding="utf-8")


def test_should_spawn_false_when_young_and_alive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = 2_000_000_000.0
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 0.5))
    _write_started_at(tmp_path, pid=999, stable_epoch=111, started_at=started_at)
    monkeypatch.setattr(supervisor, "stable_pid_alive", lambda pid, stored_start_epoch="": True)
    assert supervisor.should_spawn(tmp_path, now=now) is False


def test_should_spawn_true_when_young_but_dead(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = 2_000_000_000.0
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 0.5))
    _write_started_at(tmp_path, pid=999, stable_epoch=111, started_at=started_at)
    monkeypatch.setattr(supervisor, "stable_pid_alive", lambda pid, stored_start_epoch="": False)
    assert supervisor.should_spawn(tmp_path, now=now) is True


def test_should_spawn_true_when_older_than_debounce_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 2_000_000_000.0
    started_at = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - supervisor.SPAWN_DEBOUNCE_SECS - 1)
    )
    _write_started_at(tmp_path, pid=999, stable_epoch=111, started_at=started_at)
    monkeypatch.setattr(supervisor, "stable_pid_alive", lambda pid, stored_start_epoch="": True)
    assert supervisor.should_spawn(tmp_path, now=now) is True


def test_should_spawn_true_on_malformed_started_at(tmp_path: Path) -> None:
    _write_started_at(tmp_path, pid=999, stable_epoch=111, started_at="not-a-timestamp")
    assert supervisor.should_spawn(tmp_path) is True


# ---------------------------------------------------------------------------
# listener_url
# ---------------------------------------------------------------------------


def test_listener_url_shape() -> None:
    assert supervisor.listener_url({"port": 8934}) == "http://127.0.0.1:8934"


def test_listener_url_none_on_malformed_port() -> None:
    assert supervisor.listener_url({}) is None
    assert supervisor.listener_url({"port": "not-an-int"}) is None


# ---------------------------------------------------------------------------
# check_health
# ---------------------------------------------------------------------------


class _HealthHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002
        pass

    def do_GET(self):  # noqa: N802
        if self.path == supervisor.HEALTH_PATH:
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


def test_check_health_true_against_a_real_listener() -> None:
    httpd = HTTPServer(("127.0.0.1", 0), _HealthHandler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        assert supervisor.check_health(f"http://127.0.0.1:{port}") is True
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


def test_check_health_false_on_connection_refused() -> None:
    # A bound-then-closed socket's port is very likely to still refuse a
    # connection for the duration of this fast check.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert supervisor.check_health(f"http://127.0.0.1:{port}", timeout=0.5) is False


def test_check_health_false_on_non_2xx_status() -> None:
    class _Resp:
        status = 500

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    assert supervisor.check_health("http://127.0.0.1:1", opener=lambda url, timeout: _Resp()) is False


def test_check_health_false_on_opener_exception() -> None:
    def _raise(url, timeout):
        raise OSError("boom")

    assert supervisor.check_health("http://127.0.0.1:1", opener=_raise) is False


# ---------------------------------------------------------------------------
# ensure_listener -- the fail-open, never-waits AC10b entry point
# ---------------------------------------------------------------------------


def test_ensure_listener_returns_url_when_live_and_healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor.write_discovery(
        port=8934, pid=999, stable_pid_start_epoch=111, engine_sha="x", engine_root=tmp_path
    )
    monkeypatch.setattr(supervisor, "discovery_is_live", lambda record: True)
    monkeypatch.setattr(supervisor, "check_health", lambda url, **kw: True)
    spawned = []
    monkeypatch.setattr(supervisor, "spawn_detached", lambda *a, **kw: spawned.append(a) or True)

    assert supervisor.ensure_listener(tmp_path) == "http://127.0.0.1:8934"
    assert spawned == []


def test_ensure_listener_spawns_and_returns_none_when_no_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawned = []
    monkeypatch.setattr(supervisor, "spawn_detached", lambda *a, **kw: spawned.append(a) or True)

    assert supervisor.ensure_listener(tmp_path) is None
    assert len(spawned) == 1


def test_ensure_listener_none_and_no_spawn_when_recent_boot_already_vouched_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor.write_discovery(
        port=8934, pid=999, stable_pid_start_epoch=111, engine_sha="x", engine_root=tmp_path
    )
    # Alive but unhealthy (e.g. mid-boot, socket not accepting yet) -- and
    # young enough that `should_spawn` must not fire a second spawn.
    monkeypatch.setattr(supervisor, "discovery_is_live", lambda record: True)
    monkeypatch.setattr(supervisor, "check_health", lambda url, **kw: False)
    spawned = []
    monkeypatch.setattr(supervisor, "spawn_detached", lambda *a, **kw: spawned.append(a) or True)

    assert supervisor.ensure_listener(tmp_path) is None
    assert spawned == []


def test_ensure_listener_spawns_when_discovery_is_dead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor.write_discovery(
        port=8934, pid=999, stable_pid_start_epoch=111, engine_sha="x", engine_root=tmp_path
    )
    monkeypatch.setattr(supervisor, "discovery_is_live", lambda record: False)
    spawned = []
    monkeypatch.setattr(supervisor, "spawn_detached", lambda *a, **kw: spawned.append(a) or True)

    assert supervisor.ensure_listener(tmp_path) is None
    assert len(spawned) == 1


def test_ensure_listener_never_raises_even_if_a_primitive_blows_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(supervisor, "read_discovery", _raise)
    assert supervisor.ensure_listener(tmp_path) is None


# ---------------------------------------------------------------------------
# supervisor_pipe_name -- per-machine election, distinct from the pipe
# transport server's own election
# ---------------------------------------------------------------------------


@pytestmark_win
def test_supervisor_pipe_name_distinct_from_the_pipe_server_election(tmp_path: Path) -> None:
    from coordinator_core.warm import election

    skew.write_engine_stamp(tmp_path, "sha-supervisor-distinct")
    token = skew.compute_client_token(tmp_path)
    sid = "S-1-5-21-1-2-3-1001"
    http_name = supervisor.supervisor_pipe_name(tmp_path, user_sid=sid)
    server_name = election.pipe_name(token, engine_clone=tmp_path, user_sid=sid)
    assert http_name != server_name
    assert http_name.endswith(f".http.{token}")


@pytestmark_win
def test_supervisor_pipe_name_deterministic(tmp_path: Path) -> None:
    skew.write_engine_stamp(tmp_path, "sha-supervisor-deterministic")
    sid = "S-1-5-21-1-2-3-1001"
    a = supervisor.supervisor_pipe_name(tmp_path, user_sid=sid)
    b = supervisor.supervisor_pipe_name(tmp_path, user_sid=sid)
    assert a == b


# ---------------------------------------------------------------------------
# main() -- per-machine election loss is a quiet no-op, never a crash
# ---------------------------------------------------------------------------


@pytestmark_win
def test_main_exits_zero_and_untouched_when_election_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coordinator_core.warm import election

    skew.write_engine_stamp(tmp_path, "sha-supervisor-election-lost")
    monkeypatch.setattr(supervisor, "_default_engine_clone", lambda: tmp_path)

    def _lose(name, *, user_sid=None):
        raise election.ElectionLost(name)

    monkeypatch.setattr(election, "elect", _lose)
    written = []
    monkeypatch.setattr(supervisor, "write_discovery", lambda **kw: written.append(kw))

    assert supervisor.main() == 0
    assert written == []
    assert supervisor.read_discovery(tmp_path) is None
