"""Real-wiring seam tests: supervisor leg factories, a blocking serve, and relay.run dispatch."""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

_LIB = str(Path(__file__).resolve().parents[1] / "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from claude_author_control import protocol as P  # noqa: E402
from claude_author_control import supervisor as S  # noqa: E402

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX legs")


def test_both_transport_legs_expose_make_transport():
    import ast
    base = Path(_LIB) / "claude_author_control"
    for leg in ("transport_posix.py", "transport_windows.py"):
        tree = ast.parse((base / leg).read_text(encoding="utf-8"))
        names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
        assert "make_transport" in names, leg
    for leg in ("relay_posix.py", "relay_windows.py"):
        tree = ast.parse((base / leg).read_text(encoding="utf-8"))
        assert "spawn" in {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}, leg


@posix_only
def test_select_legs_resolves_real_factories():
    spawn, make_transport = S._select_legs()
    assert callable(spawn) and callable(make_transport)


@posix_only
def test_make_transport_serve_blocks_and_answers_over_real_socket():
    _spawn, make_transport = S._select_legs()
    with tempfile.TemporaryDirectory(dir="/tmp") as d:
        t = make_transport("abc123abc123", d)

        class H:
            def handle(self, *a, **k):  # pragma: no cover - not reached by a bad line
                raise AssertionError

        th = threading.Thread(target=t.serve, args=(H(),), daemon=True)
        th.start()
        deadline = time.time() + 5
        while not os.path.exists(t.address) and time.time() < deadline:
            time.sleep(0.02)
        assert th.is_alive(), "serve must block while the transport is open"
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(5)
        s.connect(t.address)
        s.sendall(b"not json\n")
        reply = json.loads(s.makefile("rb").readline())
        s.close()
        assert reply["ok"] is False
        t.close()
        th.join(5)
        assert not th.is_alive()
        assert not os.path.exists(t.address)


@posix_only
def test_run_pumps_posix_relay_via_run(monkeypatch, tmp_path):
    calls = []

    class FakeRelay:
        pid = 1

        def run(self):
            calls.append("run")
            return 7

        def wait(self):  # pragma: no cover
            calls.append("wait")
            return 9

        def alive(self):
            return False

    class FakeTransport:
        address = str(tmp_path / "sock")

        def serve(self, h):
            pass

        def close(self):
            pass

    monkeypatch.setattr(S, "_select_legs", lambda: (lambda a, e, o: FakeRelay(),
                                                    lambda i, d: FakeTransport()))
    rc = S.run(["claude"], plugin_dir=str(tmp_path), control_base=str(tmp_path / "base"))
    assert rc == 7 and calls == ["run"]


def test_hook_iso_ts_drives_turn_state_and_supersedes_an_older_override(tmp_path):
    # The hook writes ts as ISO-8601 'Z'; the supervisor once read only numbers, so turn_since
    # stayed null and a supervisor override could never be superseded (C13 concern).
    from datetime import datetime, timezone

    sup = S.Supervisor(str(tmp_path), wallclock=lambda: 1_000.0)
    sup._override = (1_000.0, 0.0)  # as after the supervisor's own interrupt
    later = datetime.fromtimestamp(2_000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    (tmp_path / P.TURN_FILE).write_text(
        json.dumps({"session_id": "s", "turn_active": True, "event": "UserPromptSubmit", "ts": later}),
        encoding="utf-8")
    active, source, since = sup.turn_state()
    assert (active, source, since) == (True, "hook", 2_000.0)
