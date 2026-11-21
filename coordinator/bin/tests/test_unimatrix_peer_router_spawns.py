"""Behavior pins for the unimatrix per-box peer router (real faces, fake adapters)."""
import asyncio
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("unimatrix_peer_router", BIN / "unimatrix-peer-router.py")
pr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pr)

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]


@pytest.fixture(autouse=True)
def _unix_sockets_only(request):
    """Every pin but the socket-free v2 routing ones needs unix sockets."""
    if os.name == "nt" and not getattr(request.function, "_portable", False):
        pytest.skip("unix sockets")


class _AnyRoot(dict):
    """State-DB stand-in in which every thread id is a sendable CLI root."""

    def get(self, key, default=None):
        return ROOT_ROW

    def __contains__(self, key):
        return True

    def __getitem__(self, key):
        return ROOT_ROW


@pytest.fixture(autouse=True)
def _codex_state_db_reads_every_thread_as_a_root(monkeypatch):
    """Never read the box's real Codex state DB; tests needing other rows re-patch it."""
    monkeypatch.setattr(pr, "codex_thread_metadata", lambda home: _AnyRoot())


@pytest.fixture
def box():
    d = Path(tempfile.mkdtemp(prefix="pr-", dir="/tmp"))  # AF_UNIX path limit
    (d / "sessions").mkdir()
    (d / "eps").mkdir()
    (d / "state").mkdir()
    yield d
    shutil.rmtree(d, ignore_errors=True)


THR_1 = "01a1b6aa-0000-7000-8000-000000000001"
THR_2 = "01a1b6aa-0000-7000-8000-000000000003"
PEERS = [{"name": "codex-a", "vendor": "codex", "target": THR_1, "codexPid": os.getpid()},
         {"name": "agy-a", "vendor": "agy", "target": "conv-1"}]


def run(coro):
    return asyncio.run(coro)


def echo_adapters(log=None):
    async def codex(peer, text):
        if log is not None:
            log.append((peer["name"], text))
        return f"codex saw: {text}"

    async def agy(peer, text):
        if log is not None:
            log.append((peer["name"], text))
        return f"agy saw: {text}"
    return {"codex": codex, "agy": agy}


@pytest.fixture(autouse=True)
def _agy_state_under_tmp(tmp_path, monkeypatch):
    """No agy session probe reads the box's real presence dir or settings home."""
    monkeypatch.setenv("AGY_BRAIN_DIR", str(tmp_path / "agy" / "brain"))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))


DEAD_AGY_PID = 2 ** 22 + 12345


def hold(path):
    """Take the flock a Codex/agy process holds on its lock file; returns the fd to close."""
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


THR_LIVE = "01a1b600-0000-7000-8000-00000000000a"
THR_SUB = "01a1b62c-0000-7000-8000-00000000000b"
THR_NOROLL = "01a1b65b-0000-7000-8000-00000000000c"
THR_STALE = "01a1b6aa-0000-7000-8000-000000000002"
THR_7 = "01a1b6aa-0000-7000-8000-000000000007"
ROOT_ROW = {"cwd": "/w", "source": "cli", "thread_source": "user", "agent_path": None}


def _box_slug():
    """The `-on-<box>` suffix: the hostname with every non-[A-Za-z0-9_-] run collapsed to '-', lowercased."""
    return re.sub(r"[^A-Za-z0-9_-]+", "-", pr._contract().box_id(socket.gethostname())).strip("-")


def _dead_pid():
    import subprocess
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_grok_registry_lists_live_entry_as_peer_and_none_for_dead_pid(box):
    async def go():
        home = box / "grok"
        home.mkdir()
        repo = box / "myrepo"
        (repo / ".git").mkdir(parents=True)
        sid = "0123abcd-1111-2222-3333-444455556666"
        rows = [{"session_id": sid, "cwd": str(repo), "pid": os.getpid()},
                {"session_id": "dead0000-1111", "cwd": str(repo), "pid": _dead_pid()}]
        (home / "active_sessions.json").write_text(json.dumps(rows), encoding="utf-8")
        r = pr.Router([], sessions_dir=box / "sessions", state_dir=box / "state",
                      endpoint_dir=box / "eps", adapters=echo_adapters(), grok_home=home)
        await r.start()
        try:
            want = f"grok-myrepo-0123abcd-on-{_box_slug()}"
            assert list(r.peers) == [want]
            assert r.peers[want]["vendor"] == "grok" and r.peers[want]["target"] == sid
            assert r.peers[want]["cwd"] == str(repo) and want in r.faces
            (home / "active_sessions.json").write_text("[]", encoding="utf-8")
            await r.refresh_discovered()
            assert r.peers == {}
        finally:
            await r.stop()
    run(go())


def test_live_discovered_scans_grok_registry(tmp_path):
    home = tmp_path / "grok"
    home.mkdir()
    sid = "feedbeef-1111-2222-3333-444455556666"
    rows = [{"session_id": sid, "cwd": str(tmp_path / "proj"), "pid": os.getpid()},
            {"session_id": "dead0000-1111", "cwd": str(tmp_path / "proj"), "pid": _dead_pid()}]
    (home / "active_sessions.json").write_text(json.dumps(rows), encoding="utf-8")

    async def go():
        return pr.Router([], sessions_dir=tmp_path / "s", state_dir=tmp_path / "st",
                         endpoint_dir=tmp_path / "e", grok_home=home)._live_discovered()

    live = run(go())
    assert list(live) == [f"grok-proj-feedbeef-on-{_box_slug()}"]
    peer = next(iter(live.values()))
    assert peer["vendor"] == "grok" and peer["target"] == sid and peer["cwd"] == str(tmp_path / "proj")


FAR_ROW = {"unimatrixRemote": "user@far-box", "unimatrixFarName": "em"}


SPELLINGS = ["Machine-b.local", "machine-b", "Machine-b", "machine-b-lan.local"]


THR_HELD = "01a1b600-0000-7000-8000-0000000000a1"
THR_OTHER = "01a1b600-0000-7000-8000-0000000000a2"


def holder_router(box, monkeypatch, peers, locks=None):
    """A router whose Codex threads read as sendable roots with an addressable rollout."""
    monkeypatch.setattr(pr, "codex_thread_metadata", lambda home: {
        t: ROOT_ROW for t in (THR_HELD, THR_OTHER)})
    monkeypatch.setattr(pr, "codex_rollout_addressable", lambda *a: True)
    return pr.Router(peers, sessions_dir=box / "sessions", state_dir=box / "state", endpoint_dir=box / "eps",
                     adapters=echo_adapters(), codex_locks_dir=locks)


def test_dead_holder_row_gets_no_face_even_with_its_lock_held_and_is_not_listed(box, monkeypatch):
    async def go():
        cx = box / "cx"
        peers = [{"name": "codex-dead", "vendor": "codex", "target": THR_HELD, "codexPid": _dead_pid()},
                 {"name": "codex-live", "vendor": "codex", "target": THR_OTHER, "codexPid": os.getpid()}]
        fd = hold(cx / f"{THR_HELD}.lock")
        r = holder_router(box, monkeypatch, peers, cx)
        await r.start()
        try:
            await r.refresh_discovered()
            assert sorted(r.faces) == ["codex-live"] and r.discovered == set()
            resp = await r._dispatch({"op": "list"})
            assert resp["peers"] == ["codex-live"] and "codex-dead" not in resp["ready"]
            assert [a["name"] for a in resp["agents"]] == ["codex-live"] and "stale" not in resp["agents"][0]
            bad = await r._dispatch({"op": "send", "to": "codex-dead", "text": "hello there"})
            assert bad["ok"] is False
            good = await r._dispatch({"op": "send", "to": "codex-live", "text": "hello there"})
            assert good["ok"] is True
        finally:
            os.close(fd)
            await r.stop()
    run(go())


THR_ROOT = "01a1b629-0934-71f1-aba9-95987bbc9c6b"
THR_V2 = "01a1b62b-c998-7eb1-90fe-a2d18963d8d7"
THR_V2_ORPHAN = "01a1b62b-0000-7000-8000-0000000000ff"
REPLY_TO_ROOT = r"--to '?codex-root'? '<your reply>'$"


def _inode_router(box):
    return pr.Router([], sessions_dir=box / "sessions", state_dir=box / "state", endpoint_dir=box / "eps",
                     adapters={})


def _answers_on(path):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(2)
    try:
        s.connect(path)
        return True
    except OSError:
        return False
    finally:
        s.close()


def test_router_leaves_an_unchanged_control_socket_alone(box):
    async def go():
        r = _inode_router(box)
        await r.start()
        try:
            server, ino = r.server, r.ctl_ino
            await r.check_control_socket()
            assert r.server is server and r.ctl_ino == ino
            assert _answers_on(r.ctl)
        finally:
            await r.stop()
    asyncio.run(go())


def test_router_exits_when_the_control_socket_cannot_be_rebound(box, monkeypatch):
    async def go():
        r = _inode_router(box)
        await r.start()
        try:
            os.unlink(r.ctl)

            async def refuse():
                raise OSError("address in use")
            monkeypatch.setattr(r, "_bind_control", refuse)
            await r.check_control_socket()
            assert r.stop_requested.is_set()
        finally:
            await r.stop()
    asyncio.run(go())
