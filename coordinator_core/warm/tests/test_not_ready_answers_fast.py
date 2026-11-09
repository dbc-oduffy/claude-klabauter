"""A connection reaching a bound-but-not-yet-serving engine gets a determinate answer fast."""
from __future__ import annotations

import json
import socket
import sys
import time

import pytest

from coordinator_core.warm import server

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="unix-socket boot window")


def _listener(tmp_path):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    return sock, sock.getsockname()


def _ask(path, frame):
    c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    c.settimeout(3.0)
    c.connect(path)
    c.sendall((json.dumps(frame) + "\n").encode())
    data = c.makefile("rb").readline()
    c.close()
    return json.loads(data)


def test_not_ready_connection_gets_determinate_answer_fast(tmp_path):
    sock, path = _listener(tmp_path)
    answerer = server._NotReadyAnswerer(sock, server_sha="a" * 40)
    answerer.start()
    try:
        t0 = time.monotonic()
        reply = _ask(path, {"jsonrpc": "2.0", "id": 7, "method": "x", "_engine_token": "stale"})
        elapsed = time.monotonic() - t0
    finally:
        answerer.stop()
        sock.close()
    assert reply["id"] == 7
    assert "error" in reply
    assert elapsed < 1.0


def test_after_stop_the_answerer_takes_no_connection(tmp_path):
    sock, path = _listener(tmp_path)
    answerer = server._NotReadyAnswerer(sock, server_sha="a" * 40)
    answerer.start()
    answerer.stop()
    c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    c.connect(path)
    c.sendall(b'{"jsonrpc": "2.0", "id": 1}\n')
    time.sleep(0.3)
    sock.settimeout(1.0)
    conn, _ = sock.accept()
    assert conn.recv(64).startswith(b'{"jsonrpc"')
    conn.close()
    c.close()
    sock.close()
