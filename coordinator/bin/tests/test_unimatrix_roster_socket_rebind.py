"""A mirror whose bridge process is alive but whose local socket vanished is rebuilt, never left deaf."""
import importlib.util
import time
from argparse import Namespace
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]
FAR = "/tmp/cc-socks/cc-msg-far.sock"
REMOTE = "example-operator@192.168.0.174"


class Bridge:
    pid = 4242

    def poll(self):
        return None


@pytest.fixture
def roster(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("unimatrix_roster_rebind", BIN / "unimatrix-roster.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    socks = tmp_path / "socks"
    socks.mkdir()
    monkeypatch.setattr(mod, "LOOPBACK_SOCKS", str(socks))
    monkeypatch.setattr(mod, "_is_win", lambda platform=None: False)
    monkeypatch.setattr(mod, "live_sessions", lambda: [])
    monkeypatch.setattr(mod, "alias_duplicate", lambda *a, **k: False)
    monkeypatch.setattr(mod, "rename_live_mirror", lambda *a, **k: None)
    monkeypatch.setattr(mod, "crossed_mirrors", lambda *a, **k: [])
    mod.unmirrored, mod.mirrored = [], []
    monkeypatch.setattr(mod, "unmirror", lambda pid: mod.unmirrored.append(pid))

    def fake_mirror(row, cfg, say, scan=None):
        mod.mirrored.append(row)
        return Bridge()

    monkeypatch.setattr(mod, "mirror", fake_mirror)
    monkeypatch.setattr(mod, "mirror_entry_name", lambda *a, **k: "n")
    return mod


def run_sync(roster, bridges, lines):
    cfg = Namespace(remote=REMOTE)
    roster.sync(bridges, {FAR: {"entry": {"name": "n", "messagingSocketPath": FAR}}}, cfg, lines.append)


def test_live_bridge_whose_socket_vanished_is_unmirrored_and_rebuilt(roster):
    b = Bridge()
    bridges = {FAR: b}
    roster.BOUND_SINCE[b.pid] = time.time() - 3600
    lines = []
    run_sync(roster, bridges, lines)
    assert roster.unmirrored == [b.pid]
    assert len(roster.mirrored) == 1
    assert any("socket" in m and "re-mirror" in m for m in lines)


def test_live_bridge_with_its_socket_present_is_left_alone(roster):
    b = Bridge()
    bridges = {FAR: b}
    roster.BOUND_SINCE[b.pid] = time.time() - 3600
    Path(roster.local_endpoint(f"{REMOTE}:{FAR}")).write_text("")
    run_sync(roster, bridges, [])
    assert roster.unmirrored == [] and roster.mirrored == []


def test_a_bridge_still_inside_its_bind_grace_is_not_rebuilt(roster):
    b = Bridge()
    roster.BOUND_SINCE[b.pid] = time.time()
    run_sync(roster, {FAR: b}, [])
    assert roster.unmirrored == [] and roster.mirrored == []

import os
import shutil
import socket
import importlib.util as _ilu
import tempfile as _tempfile
import threading as _threading

_BRIDGE_PATH = Path(__file__).resolve().parents[1] / "unimatrix-bridge.py"
_bspec = _ilu.spec_from_file_location("unimatrix_bridge_inode", _BRIDGE_PATH)
bridge = _ilu.module_from_spec(_bspec)
_bspec.loader.exec_module(bridge)


@pytest.fixture
def box():
    d = Path(_tempfile.mkdtemp(prefix="pri-", dir="/tmp"))  # AF_UNIX path limit
    (d / "sessions").mkdir()
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _bound_bridge_socket(box):
    path = str(box / "b.sock")
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(path)
    srv.listen(1)
    return srv, path, os.stat(path).st_ino


def test_bridge_socket_lost_is_false_while_the_bound_inode_stands(box):
    srv, path, ino = _bound_bridge_socket(box)
    try:
        assert bridge.socket_lost(path, ino) is False
    finally:
        srv.close()


def test_bridge_socket_lost_when_deleted_or_swapped(box):
    srv, path, ino = _bound_bridge_socket(box)
    try:
        os.unlink(path)
        assert bridge.socket_lost(path, ino) is True
        open(path, "w").close()
        assert bridge.socket_lost(path, ino) is True
    finally:
        srv.close()


def test_bridge_socket_lost_is_false_before_anything_is_bound(box):
    assert bridge.socket_lost(str(box / "never.sock"), None) is False


def test_bridge_watch_exits_without_unlinking_a_foreign_socket(box, monkeypatch):
    srv, path, ino = _bound_bridge_socket(box)
    exited = _threading.Event()
    try:
        os.unlink(path)
        open(path, "w").close()  # a successor owns the path
        monkeypatch.setattr(bridge, "SUPERVISOR_POLL_SECONDS", 0.01)
        monkeypatch.setattr(bridge, "bound_ino", ino)
        monkeypatch.setattr(bridge, "cfg", type("C", (), {"endpoint": path, "supervisor": None})())
        monkeypatch.setattr(bridge, "log", open(box / "bridge.log", "a"))
        monkeypatch.setattr(bridge, "SESSIONS", str(box / "sessions"))
        monkeypatch.setattr(bridge.os, "_exit", lambda code: (exited.set(), (_ for _ in ()).throw(SystemExit)))
        with pytest.raises(SystemExit):
            bridge.watch_socket()
        assert exited.is_set()
        assert os.path.exists(path)
    finally:
        srv.close()
