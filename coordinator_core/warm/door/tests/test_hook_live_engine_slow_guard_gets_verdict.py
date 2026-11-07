"""A live engine whose guard answers after the 500ms probe gets its verdict from the hook door; a dead one fails open fast.

Everything builds into tmp_path and points COORDINATOR_SETTINGS_HOME (and so door-fail-open.log) at tmp_path.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

from coordinator_core.warm.door.tests.test_hook_mode_post_delivery_names_timeout import (  # noqa: F401 -- fixture
    _fail_open_lines,
    _posix_hook_run,
    posix_door,
)
from coordinator_core.warm.tests.test_door_read_deadline_posix import runtime_base  # noqa: F401 -- fixture

_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="drives the POSIX door")
_PROBE_MS = 500

_DENY_REPLY = (
    '{"jsonrpc":"2.0","id":1,"result":{"stdout":"{\\"hookSpecificOutput\\":{\\"hookEventName\\":'
    '\\"PreToolUse\\",\\"permissionDecision\\":\\"deny\\",\\"permissionDecisionReason\\":\\"slow guard says no\\"}}\\n",'
    '"stderr":"","exit_code":0}}\n'
).encode()


class _DelayedReplyServer:
    """Accepts, reads the request frame, waits `delay_s`, then answers `reply`: a live engine with a slow guard."""

    def __init__(self, sock_path: Path, reply: bytes, delay_s: float) -> None:
        self._path = sock_path
        self._reply = reply
        self._delay_s = delay_s
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(sock_path))
        self._sock.listen(1)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        conn = None
        try:
            conn, _ = self._sock.accept()
            seen = b""
            while b"\n" not in seen:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                seen += chunk
            time.sleep(self._delay_s)
            conn.sendall(self._reply)
        except OSError:
            pass
        finally:
            if conn is not None:
                conn.close()

    def close(self) -> None:
        self._thread.join(timeout=15)
        self._sock.close()
        self._path.unlink(missing_ok=True)


@pytest.mark.spawns_process
@pytest.mark.warm_tier
@_POSIX_ONLY
def test_a_guard_answering_after_the_probe_returns_its_deny_and_logs_no_fail_open(posix_door, tmp_path, runtime_base):
    from coordinator_core.warm.tests import test_door_read_deadline_posix as posix

    root = posix._make_stub_engine_root(tmp_path)
    sock = posix._socket_path_for(runtime_base, root)
    posix._make_socket_dir(sock)
    server = _DelayedReplyServer(sock, _DENY_REPLY, 0.9)
    settings = tmp_path / "settings"
    try:
        proc, elapsed_ms = _posix_hook_run(posix_door, root, runtime_base, settings)
    finally:
        server.close()

    decision = json.loads(proc.stdout.decode("utf-8"))["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert decision["permissionDecisionReason"] == "slow guard says no"
    assert elapsed_ms >= 900
    assert _fail_open_lines(settings) == []


@pytest.mark.spawns_process
@pytest.mark.warm_tier
@_POSIX_ONLY
def test_an_engine_that_closes_the_connection_fails_open_inside_the_probe(posix_door, tmp_path, runtime_base):
    from coordinator_core.warm.tests import test_door_read_deadline_posix as posix

    root = posix._make_stub_engine_root(tmp_path)
    sock = posix._socket_path_for(runtime_base, root)
    posix._make_socket_dir(sock)
    server = posix._PeerDeathServer(sock)
    settings = tmp_path / "settings"
    try:
        proc, elapsed_ms = _posix_hook_run(posix_door, root, runtime_base, settings)
    finally:
        server.close()

    assert "command allowed" in json.loads(proc.stdout.decode("utf-8"))["systemMessage"]
    assert elapsed_ms < _PROBE_MS
    (line,) = _fail_open_lines(settings)
    assert "elapsed_ms=" in line and "op=hooks.preuse_bash_dispatch" in line
