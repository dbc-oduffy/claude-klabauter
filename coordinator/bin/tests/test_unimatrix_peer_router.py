"""Behavior pins for the unimatrix per-box peer router (real faces, fake adapters)."""
import asyncio
import importlib.util
import json
import os
import re
import shlex
import shutil
import socket
import stat
import sys
import tempfile
import threading
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("unimatrix_peer_router", BIN / "unimatrix-peer-router.py")
pr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pr)

pytestmark = [pytest.mark.complete]


def portable(fn):
    """Marks a socket-free pin that also runs on Windows."""
    fn._portable = True
    return fn


@pytest.fixture(autouse=True)
def _unix_sockets_only(request):
    """Every pin but the socket-free v2 routing ones needs unix sockets."""
    if os.name == "nt" and not getattr(request.function, "_portable", False):
        pytest.skip("unix sockets")


@pytest.fixture
def dirs(tmp_path):
    for sub in ("sessions", "eps", "state"):
        (tmp_path / sub).mkdir()
    return tmp_path


@pytest.fixture
def box():
    d = Path(tempfile.mkdtemp(prefix="pr-", dir="/tmp"))  # AF_UNIX path limit
    (d / "sessions").mkdir()
    (d / "eps").mkdir()
    (d / "state").mkdir()
    yield d
    shutil.rmtree(d, ignore_errors=True)


class FakeSender:
    """A Claude-session stand-in: a unix socket that records every frame written to it."""

    def __init__(self, path):
        self.path = path
        self.frames = []
        self.srv = socket.socket(socket.AF_UNIX)
        self.srv.bind(path)
        self.srv.listen(8)
        self.srv.settimeout(0.2)
        self.stop = False
        self.t = threading.Thread(target=self.run, daemon=True)
        self.t.start()

    def run(self):
        while not self.stop:
            try:
                c, _ = self.srv.accept()
            except (socket.timeout, OSError):
                continue
            buf = b""
            while True:
                chunk = c.recv(65536)
                if not chunk:
                    break
                buf += chunk
            c.close()
            for line in buf.splitlines():
                self.frames.append(json.loads(line))

    def close(self):
        self.stop = True
        self.t.join(2)
        self.srv.close()

    def contents(self):
        return [f["message"]["content"] for f in self.frames]


def frame_to(box_dir, from_ep, from_name, text):
    return pr._contract().build_frame(from_ep, from_name, text)


def post_frame(endpoint, frame):
    s = socket.socket(socket.AF_UNIX)
    s.connect(endpoint)
    s.sendall((json.dumps(frame) + "\n").encode())
    s.close()


async def until(pred, timeout=10):
    for _ in range(int(timeout / 0.05)):
        if pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached")


PEERS = [{"name": "codex-a", "vendor": "codex", "target": "thr-1"},
         {"name": "agy-a", "vendor": "agy", "target": "conv-1"}]


def make_router(box, adapters, peers=PEERS):
    return pr.Router(peers, sessions_dir=box / "sessions", state_dir=box / "state",
                     endpoint_dir=box / "eps", adapters=adapters)


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


def test_registers_one_entry_per_peer_with_private_key_and_harness_shape(box):
    async def go():
        r = make_router(box, echo_adapters())
        await r.start()
        try:
            entries = [json.loads(p.read_text()) for p in (box / "sessions").glob("*.json")]
            assert sorted(e["name"] for e in entries) == ["agy-a", "codex-a"]
            assert len({e["pid"] for e in entries}) == 2  # one pid per peer
            for e in entries:
                assert "version" not in e
                assert e["pidDomain"] == pr._roster().pid_domain()
                assert e["pidDomain"] != "darwin" or os.uname().sysname == "Darwin"
                assert e["procStart"] == pr._roster().proc_start(e["pid"])
                key = box / "sessions" / pr._roster().key_name(e["pid"], e["messagingSocketPath"])
                assert stat.S_IMODE(key.stat().st_mode) == 0o600
                assert stat.S_IMODE(os.stat(e["messagingSocketPath"]).st_mode) == 0o600
            assert stat.S_IMODE(os.stat(r.ctl).st_mode) == 0o600
        finally:
            await r.stop()
    run(go())


def test_two_senders_two_peers_reply_labelled_to_each_sender(box):
    async def go():
        log = []
        r = make_router(box, echo_adapters(log))
        s1 = FakeSender(str(box / "s1.sock"))
        s2 = FakeSender(str(box / "s2.sock"))
        await r.start()
        try:
            for sender, name in ((s1, "em-one"), (s2, "em-two")):
                for peer in ("codex-a", "agy-a"):
                    post_frame(r.endpoint_of(peer), frame_to(box, sender.path, name, f"q-{name}-{peer}"))
            await until(lambda: len(s1.frames) == 2 and len(s2.frames) == 2)
            for sender, name in ((s1, "em-one"), (s2, "em-two")):
                contents = " ".join(sender.contents())
                for peer in ("codex-a", "agy-a"):
                    assert f"[reply] {peer}: " in contents
                    assert f"q-{name}-{peer}" in contents
                for f in sender.frames:
                    assert f["provenance"]["vendor"] in ("codex", "agy")
                    assert f["from"] == f"uds:{r.endpoint_of(f['provenance']['peer'])}"
            seen = " ".join(t for _, t in log)
            assert "[em-one] q-em-one-codex-a" in seen
            assert "From agent em-one, not the PM. " in seen
            assert "Reply to them with:" in seen
        finally:
            s1.close()
            s2.close()
            await r.stop()
    run(go())


def test_concurrent_turns_on_one_peer_serialise(box):
    state = {"active": 0, "max": 0, "calls": 0}

    async def slow(peer, text):
        state["active"] += 1
        state["calls"] += 1
        state["max"] = max(state["max"], state["active"])
        await asyncio.sleep(0.15)
        state["active"] -= 1
        return "done"

    async def go():
        r = make_router(box, {"codex": slow, "agy": slow})
        s = FakeSender(str(box / "s.sock"))
        await r.start()
        try:
            for i in range(3):
                post_frame(r.endpoint_of("codex-a"), frame_to(box, s.path, "em", f"m{i}"))
            await until(lambda: len(s.frames) == 3)
            assert state["calls"] == 3 and state["max"] == 1
        finally:
            s.close()
            await r.stop()
    run(go())


def test_different_peers_run_concurrently(box):
    state = {"active": 0, "max": 0}

    async def slow(peer, text):
        state["active"] += 1
        state["max"] = max(state["max"], state["active"])
        await asyncio.sleep(0.3)
        state["active"] -= 1
        return "ok"

    async def go():
        r = make_router(box, {"codex": slow, "agy": slow})
        s = FakeSender(str(box / "s.sock"))
        await r.start()
        try:
            post_frame(r.endpoint_of("codex-a"), frame_to(box, s.path, "em", "x"))
            post_frame(r.endpoint_of("agy-a"), frame_to(box, s.path, "em", "y"))
            await until(lambda: len(s.frames) == 2)
            assert state["max"] == 2
        finally:
            s.close()
            await r.stop()
    run(go())


def test_adapter_failure_replies_delivery_failed_to_sender(box):
    async def boom(peer, text):
        raise RuntimeError("socket gone")

    async def go():
        r = make_router(box, {"codex": boom, "agy": boom})
        s = FakeSender(str(box / "s.sock"))
        await r.start()
        try:
            post_frame(r.endpoint_of("codex-a"), frame_to(box, s.path, "em", "hi"))
            await until(lambda: len(s.frames) == 1)
            assert "[reply] codex-a: delivery failed: socket gone" in s.contents()[0]
        finally:
            s.close()
            await r.stop()
    run(go())


def test_peer_to_peer_send_returns_labelled_reply_on_the_connection(box):
    async def go():
        log = []
        r = make_router(box, echo_adapters(log))
        await r.start()
        try:
            resp = await asyncio.to_thread(pr.request_line, r.ctl,
                                           {"op": "send", "as": "agy-a", "to": "codex-a", "text": "ping"})
            assert resp["ok"] is True
            assert resp["reply"].startswith("[codex-a] codex saw: ")
            assert "[agy-a] ping" in resp["reply"]
            assert "--to agy-a '<your reply>'" in resp["reply"]
            bad = await asyncio.to_thread(pr.request_line, r.ctl, {"op": "send", "to": "nobody", "text": "x"})
            assert bad["ok"] is False and bad["peers"] == ["agy-a", "codex-a"]
        finally:
            await r.stop()
    run(go())


def test_frame_from_a_registered_peer_gets_no_reply_back(box):
    async def go():
        log = []
        r = make_router(box, echo_adapters(log))
        s = FakeSender(str(box / "s.sock"))
        await r.start()
        try:
            post_frame(r.endpoint_of("codex-a"), frame_to(box, r.endpoint_of("agy-a"), "agy-a", "from agy"))
            await until(lambda: len(log) == 1)
            post_frame(r.endpoint_of("codex-a"), frame_to(box, s.path, "em", "from claude"))
            await until(lambda: len(s.frames) == 1)
            assert len(log) == 2  # the peer-origin frame ran a turn but produced no echo loop
        finally:
            s.close()
            await r.stop()
    run(go())


def test_stop_leaves_no_entry_key_or_endpoint(box):
    async def go():
        r = make_router(box, echo_adapters())
        await r.start()
        assert list((box / "sessions").iterdir())
        await r.stop()
    run(go())
    assert list((box / "sessions").iterdir()) == []
    assert list((box / "eps").iterdir()) == []


def test_failed_start_cleans_up_partial_registration(box):
    async def go():
        r = make_router(box, echo_adapters())
        r.ready_seconds = 0.0
        with pytest.raises(RuntimeError):
            await r.start()
    run(go())
    assert list((box / "sessions").iterdir()) == []
    assert list((box / "eps").iterdir()) == []


def test_default_invocation_reads_settings_home_peers_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    assert pr.default_peers_file() == tmp_path / "unimatrix" / "peers.json"
    (tmp_path / "unimatrix").mkdir()
    (tmp_path / "unimatrix" / "peers.json").write_text("{not json")
    assert pr.main([]) == 2  # a malformed peers.json refuses with a message, serves nothing
    assert str(tmp_path / "unimatrix" / "peers.json") in capsys.readouterr().err


def write_leg(legs, conv, pid=None):
    legs.mkdir(parents=True, exist_ok=True)
    (legs / f"{conv}.json").write_text(json.dumps(
        {"pid": os.getpid() if pid is None else pid, "conversation": conv, "endpoint": "x",
         "ls_address": "y", "agy_pid": 1}))


def test_leg_records_become_peers_and_peers_json_wins(tmp_path):
    legs = tmp_path / "legs"
    write_leg(legs, "conv-new")
    write_leg(legs, "conv-1")  # already in peers.json by target
    write_leg(legs, "conv-dead", pid=2 ** 22 + 12345)
    (legs / "junk.json").write_text("{")
    got = pr.load_leg_peers(legs, {p["name"] for p in PEERS}, {p["target"] for p in PEERS})
    assert got == [{"name": pr.readable_peer_name("agy", "conv-new"), "vendor": "agy", "target": "conv-new"}]
    assert pr.load_leg_peers(tmp_path / "missing") == []


def test_router_registers_a_leg_that_appears_after_start_without_peers_json(box):
    async def go():
        legs = box / "legs"
        log = []
        r = pr.Router([], sessions_dir=box / "sessions", state_dir=box / "state",
                      endpoint_dir=box / "eps", adapters=echo_adapters(log), legs_dir=legs)
        s = FakeSender(str(box / "s.sock"))
        await r.start()
        try:
            assert r.peers == {}
            write_leg(legs, "conv-9")
            await r.refresh_discovered()
            assert pr.readable_peer_name("agy", "conv-9") in r.peers
            post_frame(r.endpoint_of(pr.readable_peer_name("agy", "conv-9")), frame_to(box, s.path, "em", "hello"))
            await until(lambda: len(s.frames) == 1)
            assert log and log[0][0] == pr.readable_peer_name("agy", "conv-9")
            await r.refresh_discovered()  # idempotent
            assert list(r.peers) == [pr.readable_peer_name("agy", "conv-9")]
        finally:
            s.close()
            await r.stop()
    run(go())


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
ROOT_ROW = {"cwd": "/w", "source": "cli", "thread_source": "user", "agent_path": None}


def write_rollout(home, thread_id, source):
    day = home / "sessions" / "2026" / "11" / "08"
    day.mkdir(parents=True, exist_ok=True)
    meta = {"type": "session_meta", "payload": {"id": thread_id, "source": source}}
    (day / f"rollout-2026-11-08T00-00-00-{thread_id}.jsonl").write_text(
        json.dumps(meta) + "\n" + json.dumps({"type": "event_msg", "payload": {}}) + "\n")


def test_lock_peers_need_a_held_lock_and_peers_json_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "codex_thread_metadata", lambda home: {
        t: ROOT_ROW for t in (THR_LIVE, "thr-1", "thr-stale")})
    write_rollout(tmp_path, THR_LIVE, "cli")
    d = tmp_path / "locks"
    fds = [hold(d / f"{THR_LIVE}.lock"), hold(d / "thr-1.lock"), hold(d / ".coordination.lock")]
    (d / "thr-stale.lock").write_text("")
    (d / "notes.txt").write_text("")
    try:
        got = pr.load_lock_peers(d, "codex", {p["name"] for p in PEERS}, {p["target"] for p in PEERS})
        assert got == [{"name": pr.readable_peer_name("codex", THR_LIVE, "/w"), "vendor": "codex",
                        "target": THR_LIVE, "cwd": "/w"}]
        assert pr.load_lock_peers(tmp_path / "missing", "codex") == []
    finally:
        for fd in fds:
            os.close(fd)
    assert pr.load_lock_peers(d, "codex") == []  # released locks are stale


def test_lock_peers_skip_rollout_subagents_and_threads_without_a_rollout(tmp_path, monkeypatch):
    pr._ROLLOUT_VERDICTS.clear()
    monkeypatch.setattr(pr, "codex_thread_metadata", lambda home: {
        t: ROOT_ROW for t in (THR_LIVE, THR_SUB, THR_NOROLL)})  # state DB reads root for all three
    write_rollout(tmp_path, THR_LIVE, "cli")
    write_rollout(tmp_path, THR_SUB, {"subagent": {"thread_spawn": {"parent_thread_id": THR_LIVE}}})
    d = tmp_path / "locks"
    fds = [hold(d / f"{t}.lock") for t in (THR_LIVE, THR_SUB, THR_NOROLL)]
    try:
        got = pr.load_lock_peers(d, "codex")
        assert [p["target"] for p in got] == [THR_LIVE]
        write_rollout(tmp_path, THR_NOROLL, "cli")  # a rollout that appears later admits the thread
        assert sorted(p["target"] for p in pr.load_lock_peers(d, "codex")) == sorted([THR_LIVE, THR_NOROLL])
    finally:
        for fd in fds:
            os.close(fd)


def test_rollout_gate_walks_the_sessions_tree_once_per_scan(tmp_path, monkeypatch):
    pr._ROLLOUT_VERDICTS.clear()
    ids = [f"01a1b600-0000-7000-8000-0000000000{n:02x}" for n in range(5)]
    monkeypatch.setattr(pr, "codex_thread_metadata", lambda home: {t: ROOT_ROW for t in ids})
    for t in ids:
        write_rollout(tmp_path, t, "cli")
    walks = []
    codex = pr._sibling("unimatrix-peer-codex.py", "unimatrix_peer_codex")
    real = codex.rollout_index
    monkeypatch.setattr(codex, "rollout_index", lambda home=None: walks.append(1) or real(home))
    d = tmp_path / "locks"
    fds = [hold(d / f"{t}.lock") for t in ids]
    try:
        assert len(pr.load_lock_peers(d, "codex")) == 5
        assert len(walks) == 1
        pr.load_lock_peers(d, "codex")
        assert len(walks) == 1  # verdicts are cached
    finally:
        for fd in fds:
            os.close(fd)


def test_shared_mode_holder_reads_as_held_and_no_fcntl_reads_as_free(tmp_path, monkeypatch):
    import fcntl
    import sys
    path = tmp_path / "thr-sh.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_SH)
    try:
        assert pr.lock_held(path) is True
        monkeypatch.setitem(sys.modules, "fcntl", None)
        assert pr.lock_held(path) is False
    finally:
        os.close(fd)


def test_default_lock_dirs_resolve_from_env_not_a_literal_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "cx"))
    monkeypatch.setenv("AGY_BRAIN_DIR", str(tmp_path / "ag" / "brain"))
    assert pr.default_codex_locks_dir() == tmp_path / "cx" / "thread-writer-locks"
    assert pr.default_presence_dir() == tmp_path / "ag" / "presence"


def test_router_with_no_peers_json_registers_locks_and_retires_them_when_released(box):
    async def go():
        cx, ag = box / "cx", box / "ag"
        log = []
        r = pr.Router([], sessions_dir=box / "sessions", state_dir=box / "state",
                      endpoint_dir=box / "eps", adapters=echo_adapters(log),
                      codex_locks_dir=cx, presence_dir=ag)
        s = FakeSender(str(box / "s.sock"))
        await r.start()
        fds = {}
        try:
            assert r.peers == {}
            fds["codex"] = hold(cx / "thr-7.lock")
            fds["agy"] = hold(ag / "conv-7.lock")
            await r.refresh_discovered()
            assert sorted(r.peers) == sorted([pr.readable_peer_name("agy", "conv-7"), pr.readable_peer_name("codex", "thr-7")])
            await r.refresh_discovered()  # idempotent
            assert sorted(r.faces) == [pr.readable_peer_name("agy", "conv-7"), pr.readable_peer_name("codex", "thr-7")]
            post_frame(r.endpoint_of(pr.readable_peer_name("codex", "thr-7")), frame_to(box, s.path, "em", "hello"))
            await until(lambda: len(s.frames) == 1)
            assert log[0][0] == pr.readable_peer_name("codex", "thr-7")
            assert r.peers[pr.readable_peer_name("codex", "thr-7")]["tui"] is True and "tui" not in r.peers[pr.readable_peer_name("agy", "conv-7")]
            ep, pid = r.endpoint_of(pr.readable_peer_name("codex", "thr-7")), r.faces[pr.readable_peer_name("codex", "thr-7")].pid
            os.close(fds.pop("codex"))
            await r.refresh_discovered()
            assert sorted(r.peers) == [pr.readable_peer_name("agy", "conv-7")]
            assert pr.readable_peer_name("codex", "thr-7") not in r.faces and pr.readable_peer_name("codex", "thr-7") not in r.locks
            assert not os.path.exists(ep) and not os.path.exists(box / "sessions" / f"{pid}.json")
        finally:
            for fd in fds.values():
                os.close(fd)
            s.close()
            await r.stop()
    run(go())


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
            want = f"grok-myrepo-0123abcd-on-{socket.gethostname().lower()}"
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
    assert list(live) == [f"grok-proj-feedbeef-on-{socket.gethostname().lower()}"]
    peer = next(iter(live.values()))
    assert peer["vendor"] == "grok" and peer["target"] == sid and peer["cwd"] == str(tmp_path / "proj")


def test_default_adapters_grok_delivers_by_cwd_pinned_resume(monkeypatch):
    assert "grok" in pr.default_adapters()

    class Proc:
        returncode = 0
        stdout = json.dumps({"text": "grok said hi"})

    argvs = []
    real = pr._load("unimatrix-peer-grok.py", "unimatrix_peer_grok_probe")
    monkeypatch.setattr(real.subprocess, "run", lambda argv, **kw: argvs.append((argv, kw)) or Proc())
    monkeypatch.setattr(pr, "_load", lambda filename, modname: real)
    reply = run(pr.default_adapters()["grok"]({"target": "sid-1", "cwd": "/w/repo"}, "ping"))
    assert reply == "grok said hi"
    argv, kw = argvs[0]
    assert argv[argv.index("--cwd") + 1] == "/w/repo" and "--add-dir" not in argv and kw["cwd"] == "/w/repo"


def test_peers_json_entry_survives_its_lock_going_and_beats_a_discovered_clash(box):
    async def go():
        cx = box / "cx"
        r = pr.Router(PEERS, sessions_dir=box / "sessions", state_dir=box / "state",
                      endpoint_dir=box / "eps", adapters=echo_adapters(), codex_locks_dir=cx)
        await r.start()
        fd = hold(cx / "thr-1.lock")  # same target as the peers.json codex-a
        try:
            await r.refresh_discovered()
            assert sorted(r.peers) == ["agy-a", "codex-a"] and r.discovered == set()
            os.close(fd)
            fd = None
            await r.refresh_discovered()
            assert sorted(r.peers) == ["agy-a", "codex-a"]
        finally:
            if fd is not None:
                os.close(fd)
            await r.stop()
    run(go())


def test_empty_and_ack_frames_are_not_delivered_or_answered(box):
    async def go():
        log = []
        r = make_router(box, echo_adapters(log))
        s = FakeSender(str(box / "s.sock"))
        await r.start()
        try:
            for body in ("   ", "ok", "Thanks!", "Got it."):
                post_frame(r.endpoint_of("codex-a"), frame_to(box, s.path, "em", body))
            post_frame(r.endpoint_of("codex-a"), frame_to(box, s.path, "em", "real question"))
            await until(lambda: len(s.frames) == 1)
            assert [t for _, t in log] == [pr.wrap_inbound("em", "real question")]
            assert (await r._dispatch({"op": "send", "to": "codex-a", "text": "ok"}))["ok"] is False
        finally:
            s.close()
            await r.stop()
    run(go())


def test_reply_labelled_frame_with_content_is_not_delivered_or_answered(box):
    async def go():
        log = []
        r = make_router(box, echo_adapters(log))
        s = FakeSender(str(box / "s.sock"))
        await r.start()
        try:
            post_frame(r.endpoint_of("codex-a"), frame_to(box, s.path, "em", "[reply] The answer is 42"))
            post_frame(r.endpoint_of("codex-a"), frame_to(box, s.path, "em", "real question"))
            await until(lambda: len(s.frames) == 1)
            assert [t for _, t in log] == [pr.wrap_inbound("em", "real question")]
        finally:
            s.close()
            await r.stop()
    run(go())


def test_wrap_inbound_names_sender_and_absolute_send_command():
    w = pr.wrap_inbound("em-one", "build is red")
    first, trailer = w.split("\n\n")
    assert first == "[em-one] build is red"
    assert trailer.startswith("From agent em-one, not the PM. ")
    assert "Never answer them in plain text to the human at your keyboard." in trailer
    assert "Reply to them with: " in trailer
    assert str(pr.here() / "unimatrix-send.py") in trailer
    assert f"Reply to them with: {shlex.quote(sys.executable)} " in trailer
    assert trailer.endswith("--to em-one '<your reply>'")
    assert pr.label_prefix().sub("", first) == "build is red"


def test_wrapped_ack_and_reply_shapes_keep_the_no_auto_answer_guard():
    assert pr.is_ack(pr.wrap_inbound("em", "ok"))
    assert pr.is_ack(pr.wrap_inbound("em", "[reply] Got it."))
    assert not pr.is_ack(pr.wrap_inbound("em", "the build is red"))


def test_labelled_ack_and_reply_frames_are_acks_but_labelled_questions_are_not():
    for t in ("[reply]", "[codex-a] ok", "[reply] Got it.", "[agy-x]"):
        assert pr.is_ack(t), t
    for t in ("[codex-a] the build is red", "is it ok?"):
        assert not pr.is_ack(t), t


def test_empty_adapter_reply_is_not_delivered(box):
    async def go():
        async def silent(peer, text):
            return "  "
        r = make_router(box, {"codex": silent, "agy": silent})
        s = FakeSender(str(box / "s.sock"))
        await r.start()
        try:
            post_frame(r.endpoint_of("codex-a"), frame_to(box, s.path, "em", "question"))
            await asyncio.sleep(1)
            assert s.frames == []
        finally:
            s.close()
            await r.stop()
    run(go())


def register_session(box, name, sock_path):
    (box / "sessions" / f"{name}.json").write_text(json.dumps(
        {"name": name, "pid": os.getpid(), "messagingSocketPath": sock_path, "startedAt": 1}))


def test_harvested_reply_reaches_the_sender_framed_reply_peer_text(box):
    async def go():
        r = make_router(box, echo_adapters())
        s = FakeSender(str(box / "s.sock"))
        await r.start()
        try:
            for peer in ("codex-a", "agy-a"):
                post_frame(r.endpoint_of(peer), frame_to(box, s.path, "em", "question"))
            await until(lambda: len(s.frames) == 2)
            for peer in ("codex-a", "agy-a"):
                assert sum(f"\n[reply] {peer}: " in c for c in s.contents()) == 1
        finally:
            s.close()
            await r.stop()
    run(go())


def test_peer_addressed_send_during_the_turn_suppresses_the_harvested_reply(box):
    async def go():
        s = FakeSender(str(box / "s.sock"))
        register_session(box, "em", s.path)
        holder = {}

        async def sends(peer, text):
            resp = await holder["r"]._dispatch({"op": "send", "as": peer["name"], "to": "em", "text": "direct answer"})
            assert resp["ok"] is True
            return "final answer printed to the human"
        r = make_router(box, {"codex": sends, "agy": sends})
        holder["r"] = r
        await r.start()
        try:
            post_frame(r.endpoint_of("codex-a"), frame_to(box, s.path, "em", "question"))
            await until(lambda: len(s.frames) == 1)
            await asyncio.sleep(0.7)  # a duplicate would land by now
            assert len(s.contents()) == 1 and "\n[codex-a] direct answer\n" in s.contents()[0]
            assert "final answer" not in s.contents()[0]
        finally:
            s.close()
            await r.stop()
    run(go())


def test_peer_send_to_unknown_session_fails_and_other_senders_turn_still_replies(box):
    async def go():
        r = make_router(box, echo_adapters())
        s = FakeSender(str(box / "s.sock"))
        await r.start()
        try:
            bad = await r._dispatch({"op": "send", "as": "codex-a", "to": "nobody", "text": "hi"})
            assert bad["ok"] is False
            post_frame(r.endpoint_of("codex-a"), frame_to(box, s.path, "em", "question"))
            await until(lambda: len(s.frames) == 1)
            assert "\n[reply] codex-a: " in s.contents()[0]
        finally:
            s.close()
            await r.stop()
    run(go())


def test_peers_json_codex_thread_held_by_a_tui_routes_to_the_queue_not_the_daemon(box):
    """Daemon socket up (not consulted) + a held writer lock: the adapter sees tui=True; a free lock leaves it unset."""
    async def go():
        seen = []

        async def codex(peer, text):
            seen.append((peer["target"], peer.get("tui")))
            return "ok"
        locks = box / "cx"
        fd = hold(locks / "thr-1.lock")
        (locks / "thr-2.lock").write_text("")
        peers = PEERS + [{"name": "codex-b", "vendor": "codex", "target": "thr-2"}]
        r = pr.Router(peers, sessions_dir=box / "sessions", state_dir=box / "state", endpoint_dir=box / "eps",
                      adapters={"codex": codex, "agy": codex}, codex_locks_dir=locks)
        await r.start()
        try:
            await r.turn("codex-a", "hi")
            await r.turn("codex-b", "hi")
        finally:
            os.close(fd)
            await r.stop()
        assert seen == [("thr-1", True), ("thr-2", None)]
        assert "tui" not in r.peers["codex-a"]
    run(go())


def test_list_op_names_peers_and_live_sessions_but_not_the_router_faces(box):
    async def go():
        r = make_router(box, echo_adapters())
        await r.start()
        s = FakeSender(str(box / "s.sock"))
        try:
            register_session(box, "em", s.path)
            resp = await r._dispatch({"op": "list"})
            assert resp["ok"] is True
            assert resp["peers"] == ["agy-a", "codex-a"] and resp["sessions"] == ["em"]
        finally:
            s.close()
            await r.stop()
    run(go())


def test_face_entries_carry_their_peers_vendor(box):
    async def go():
        r = make_router(box, echo_adapters())
        await r.start()
        try:
            stamped = {}
            for f in (box / "sessions").glob("*.json"):
                e = json.loads(f.read_text())
                stamped[e["name"]] = e.get("unimatrixVendor")
            assert stamped == {"codex-a": "codex", "agy-a": "agy"}
        finally:
            await r.stop()
    run(go())


def test_registration_without_a_vendor_writes_no_vendor_field(box):
    reg = pr.Registration("x", str(box / "eps" / "x.sock"), str(box / "sessions"))
    assert "unimatrixVendor" not in reg.data
    assert pr.Registration("x", "e", str(box / "sessions"), vendor="codex").data["unimatrixVendor"] == "codex"


def test_list_agents_has_one_row_per_reachable_name_with_box_and_vendor(box):
    async def go():
        r = make_router(box, echo_adapters())
        await r.start()
        s = FakeSender(str(box / "s.sock"))
        try:
            register_session(box, "em", s.path)
            (box / "sessions" / "far.json").write_text(json.dumps(
                {"name": "far-codex", "pid": os.getpid(), "messagingSocketPath": s.path,
                 "unimatrixBox": "machine-b", "unimatrixVendor": "codex"}))
            resp = await r._dispatch({"op": "list", "as": "cli"})
            rows = {a["name"]: a for a in resp["agents"]}
            assert sorted(rows) == resp["reachable"] == ["codex-a", "em", "far-codex"]
            here = r.roster_rows("cli")[1][0]["box"]
            assert rows["codex-a"] == {"name": "codex-a", "box": here, "vendor": "codex"}
            assert rows["em"] == {"name": "em", "box": here, "vendor": "claude"}
            assert rows["far-codex"] == {"name": "far-codex", "box": "machine-b", "vendor": "codex"}
            assert resp["peers"] and resp["sessions"] == ["em", "far-codex"] and "ready" in resp
        finally:
            s.close()
            await r.stop()
    run(go())


class ReplyServer:
    """A session-socket stand-in that reads one frame, then writes `reply` (or nothing)."""

    def __init__(self, path, reply=b""):
        self.frames = []
        self.reply = reply
        self.srv = socket.socket(socket.AF_UNIX)
        self.srv.bind(path)
        self.srv.listen(4)
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = c.recv(65536)
                if not chunk:
                    break
                buf += chunk
            if buf:
                self.frames.append(json.loads(buf.splitlines()[0]))
            if self.reply:
                c.sendall(self.reply)
            c.close()

    def close(self):
        self.srv.close()


def test_reachable_is_ready_peers_plus_sessions_with_an_existing_endpoint(box):
    async def go():
        r = make_router(box, echo_adapters())
        s = FakeSender(str(box / "s.sock"))
        try:
            register_session(box, "em", s.path)
            register_session(box, "stale", str(box / "gone.sock"))
            assert r.reachable("cli") == ["codex-a", "em"]  # agy-a has no leg; stale has no socket
            assert r.reachable("em") == ["codex-a"]
            resp = await r._dispatch({"op": "list", "as": "cli"})
            assert resp["reachable"] == ["codex-a", "em"] and "stale" not in resp["sessions"]
        finally:
            s.close()
    run(go())


def test_cli_send_to_a_session_name_writes_the_frame_and_reports_written(box):
    async def go():
        s = ReplyServer(str(box / "s.sock"))
        register_session(box, "em", str(box / "s.sock"))
        r = make_router(box, echo_adapters())
        try:
            resp = await r._dispatch({"op": "send", "as": "cli", "to": "em", "text": "hello"})
            assert resp["ok"] is True and resp["written"] is True and resp["msg_id"]
            await until(lambda: len(s.frames) == 1)
            f = s.frames[0]
            assert 'from-name="cli"' in f["message"]["content"] and "hello" in f["message"]["content"]
            assert "provenance" not in f
        finally:
            s.close()
    run(go())


def test_send_to_a_session_that_refuses_or_resets_or_is_gone_fails_naming_the_reason(box):
    async def go():
        register_session(box, "em", str(box / "s.sock"))
        r = make_router(box, echo_adapters())
        send = {"op": "send", "as": "cli", "to": "em", "text": "hello"}
        s = ReplyServer(str(box / "s.sock"), reply=b"ENOINBOX: no-key\n")
        try:
            bad = await r._dispatch(send)
            assert bad["ok"] is False and "em did not accept" in bad["error"] and "ENOINBOX" in bad["error"]
            assert "written" not in bad
        finally:
            s.close()
        os.unlink(str(box / "s.sock"))
        gone = await r._dispatch(send)
        assert gone["ok"] is False and "em did not accept" in gone["error"]
        unknown = await r._dispatch({**send, "to": "nobody"})
        assert unknown["ok"] is False and "no session named nobody" in unknown["error"]
    run(go())


def test_deliver_raises_when_the_peer_resets_before_close(monkeypatch):
    """macOS unix streams never surface a reset on a real socket, so the reset is injected."""
    class Resetting:
        def __init__(self, *a):
            pass

        def settimeout(self, t):
            pass

        def connect(self, ep):
            pass

        def sendall(self, d):
            pass

        def shutdown(self, how):
            pass

        def recv(self, n):
            raise ConnectionResetError(54, "Connection reset by peer")

        def close(self):
            pass
    monkeypatch.setattr(pr.socket, "socket", Resetting)
    with pytest.raises(ConnectionResetError):
        pr.deliver("/nowhere", b"x\n")

    class ClosedFirst(Resetting):
        def shutdown(self, how):
            raise OSError(pr.errno.ENOTCONN, "Transport endpoint is not connected")

        def recv(self, n):
            return b""
    monkeypatch.setattr(pr.socket, "socket", ClosedFirst)
    pr.deliver("/nowhere", b"x\n")  # the peer read the whole frame and closed before the half-close


def test_deliver_accepts_silence_and_ok_reply_and_raises_on_a_refusal_line(box):
    path = str(box / "d.sock")
    data = b'{"x":1}\n'
    for reply, accepted in ((b"", True), (b'{"ok":true}\n', True), (b'{"ok":false,"error":"no"}\n', False),
                            (b"ENOINBOX: no-key\n", False)):
        s = ReplyServer(path, reply=reply)
        try:
            if accepted:
                pr.deliver(path, data)
            else:
                with pytest.raises(RuntimeError, match="refused"):
                    pr.deliver(path, data)
        finally:
            s.close()
            os.unlink(path)


def test_peer_send_identifies_itself_by_thread_id_not_name(box):
    """unimatrix-send passes the shell's CODEX_THREAD_ID / ANTIGRAVITY_CONVERSATION_ID; the router maps it to the peer."""
    async def go():
        s = FakeSender(str(box / "s.sock"))
        register_session(box, "em", s.path)
        r = make_router(box, echo_adapters())
        await r.start()
        try:
            resp = await r._dispatch({"op": "send", "as_target": "conv-1", "as": "cli", "to": "em", "text": "answer"})
            assert resp["ok"] is True
            await until(lambda: len(s.frames) == 1)
            assert "\n[agy-a] answer\n" in s.contents()[0]
        finally:
            s.close()
            await r.stop()
    run(go())


def test_send_cli_resolves_own_target_from_host_env():
    import importlib.util as iu
    spec = iu.spec_from_file_location("unimatrix_send", Path(__file__).resolve().parents[1] / "unimatrix-send.py")
    m = iu.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.own_target({"CODEX_THREAD_ID": "thr-9"}) == "thr-9"
    assert m.own_target({"ANTIGRAVITY_CONVERSATION_ID": "conv-9"}) == "conv-9"
    assert m.own_target({}) == ""


def test_codex_timeout_marker_is_not_forwarded_as_a_reply(box):
    async def go():
        async def busy(peer, text):
            return "[unimatrix-peer-codex] no reply before timeout"
        r = make_router(box, {"codex": busy, "agy": busy})
        s = FakeSender(str(box / "s.sock"))
        await r.start()
        try:
            post_frame(r.endpoint_of("codex-a"), frame_to(box, s.path, "em", "question"))
            await asyncio.sleep(0.7)
            assert s.frames == []
        finally:
            s.close()
            await r.stop()
    run(go())


def _send_module():
    import importlib.util as iu
    spec = iu.spec_from_file_location("unimatrix_send_c1", Path(__file__).resolve().parents[1] / "unimatrix-send.py")
    m = iu.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_not_delivered_send_exits_nonzero_with_reason_and_delivered_exits_zero(box, capsys):
    no_leg = ("[unimatrix-peer-agy] not delivered: this agy conversation has no leg endpoint.")

    async def agy(peer, text):
        return no_leg

    async def go():
        ad = {**echo_adapters(), "agy": agy}
        r = make_router(box, ad)
        await r.start()
        try:
            resp = await r._dispatch({"op": "send", "to": "agy-a", "text": "hello"})
            assert resp["ok"] is False and "not delivered" in resp["error"]
            m = _send_module()
            base = ["--endpoint-dir", str(box / "eps")]
            bad = await asyncio.to_thread(m.main, base + ["--to", "agy-a", "hello"])
            good = await asyncio.to_thread(m.main, base + ["--to", "codex-a", "hello"])
            return bad, good
        finally:
            await r.stop()
    bad, good = run(go())
    out = capsys.readouterr()
    assert bad == 1 and "not delivered" in out.err
    assert good == 0 and "codex saw" in out.out


def test_list_shows_per_peer_readiness_leg_present_or_not(box, capsys, monkeypatch):
    async def go():
        r = make_router(box, echo_adapters())
        await r.start()
        try:
            resp = await r._dispatch({"op": "list"})
            assert resp["ready"] == {"agy-a": False, "codex-a": True}
            m = _send_module()
            return await asyncio.to_thread(m.main, ["--endpoint-dir", str(box / "eps"), "--list"])
        finally:
            await r.stop()
    assert run(go()) == 0
    lines = capsys.readouterr().out.splitlines()
    assert any(line.startswith("agy-a\tnot-ready (no leg)\t") for line in lines)
    assert any(line.startswith("codex-a\tready\t") for line in lines)


def test_scanner_repair_respawns_missing_face_and_restarts_exited_face_only(box):
    class Proc:
        def __init__(self, rc):
            self.rc = rc

        def poll(self):
            return self.rc

    async def go():
        r = pr.Router([], sessions_dir=box / "sessions", state_dir=box / "state", endpoint_dir=box / "eps")
        r.peers = {"alive": {}, "dead": {}, "missing": {}}
        r.faces = {"alive": Proc(None), "dead": Proc(1)}
        calls = []

        async def spawn(name):
            calls.append(("spawn", name))

        async def restart(name):
            calls.append(("restart", name))

        r._spawn_face, r.restart_face = spawn, restart
        await r._repair_faces()
        assert sorted(calls) == [("restart", "dead"), ("spawn", "missing")]

    asyncio.run(go())


def test_pipe_delivery_peeks_for_a_refusal_within_the_hot_path_budget(monkeypatch):
    seen = []
    monkeypatch.setattr(pr._transport(), "pipe_exchange", lambda *a: seen.append(a) or b"")
    pr.deliver("\\\\.\\pipe\\LOCAL\\cc-msg-" + "a" * 32, b"{}\n")
    assert seen and seen[0][3] <= 0.1


THR_HELD = "01a1b600-0000-7000-8000-0000000000a1"
THR_OTHER = "01a1b600-0000-7000-8000-0000000000a2"


def holder_router(box, monkeypatch, peers, locks=None):
    """A router whose Codex threads read as sendable roots with an addressable rollout."""
    monkeypatch.setattr(pr, "codex_thread_metadata", lambda home: {
        t: ROOT_ROW for t in (THR_HELD, THR_OTHER)})
    monkeypatch.setattr(pr, "codex_rollout_addressable", lambda *a: True)
    return pr.Router(peers, sessions_dir=box / "sessions", state_dir=box / "state", endpoint_dir=box / "eps",
                     adapters=echo_adapters(), codex_locks_dir=locks)


def face_reply(endpoint, frame):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(10)
    s.connect(endpoint)
    s.sendall((json.dumps(frame) + "\n").encode())
    s.shutdown(socket.SHUT_WR)
    buf = b""
    while chunk := s.recv(65536):
        buf += chunk
    s.close()
    return buf


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


def test_holder_dying_after_start_stops_the_face_on_the_next_refresh(box, monkeypatch):
    import subprocess

    async def go():
        cx = box / "cx"
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        peers = [{"name": "codex-a", "vendor": "codex", "target": THR_HELD, "codexPid": holder.pid}]
        fd = hold(cx / f"{THR_HELD}.lock")
        r = holder_router(box, monkeypatch, peers, cx)
        await r.start()
        try:
            assert "codex-a" in r.faces
            ep, pid = r.endpoint_of("codex-a"), r.faces["codex-a"].pid
            holder.kill()
            holder.wait()
            await r.refresh_discovered()
            assert "codex-a" not in r.faces and r.discovered == set()
            assert not os.path.exists(ep) and not os.path.exists(box / "sessions" / f"{pid}.json")
            await r._repair_faces()
            assert "codex-a" not in r.faces
            assert (await r._dispatch({"op": "list"}))["peers"] == []
        finally:
            holder.kill()
            os.close(fd)
            await r.stop()
    run(go())


def test_lock_only_codex_thread_is_listed_stale_and_refuses_sends_and_frames(box, monkeypatch):
    async def go():
        cx = box / "cx"
        fd = hold(cx / f"{THR_HELD}.lock")
        r = holder_router(box, monkeypatch, [], cx)
        await r.start()
        try:
            await r.refresh_discovered()
            name = pr.readable_peer_name("codex", THR_HELD, "/w")
            assert list(r.peers) == [name] and name in r.faces
            resp = await r._dispatch({"op": "list"})
            assert resp["agents"] == [{"name": name, "box": pr._contract().local_box(), "vendor": "codex",
                                       "stale": True}]
            bad = await r._dispatch({"op": "send", "to": name, "text": "hello there"})
            assert bad["ok"] is False and "stale" in bad["error"]
            raw = await asyncio.to_thread(face_reply, r.endpoint_of(name),
                                          frame_to(box, str(box / "s.sock"), "em", "hello there"))
            assert json.loads(raw.splitlines()[0])["ok"] is False
        finally:
            os.close(fd)
            await r.stop()
    run(go())


def test_peers_file_codex_row_without_a_holder_keeps_its_face_but_is_stale(box, monkeypatch):
    async def go():
        peers = [{"name": "codex-a", "vendor": "codex", "target": THR_HELD}]
        r = holder_router(box, monkeypatch, peers)
        await r.start()
        try:
            assert "codex-a" in r.faces
            resp = await r._dispatch({"op": "list"})
            assert resp["peers"] == ["codex-a"] and resp["agents"][0]["stale"] is True
            assert (await r._dispatch({"op": "send", "to": "codex-a", "text": "hello there"}))["ok"] is False
        finally:
            await r.stop()
    run(go())


def test_user_frame_to_a_live_holder_face_gets_no_reply_line(box, monkeypatch):
    async def go():
        log = []
        peers = [{"name": "codex-a", "vendor": "codex", "target": THR_HELD, "codexPid": os.getpid()}]
        r = holder_router(box, monkeypatch, peers)
        r.adapters = echo_adapters(log)
        s = FakeSender(str(box / "s.sock"))
        await r.start()
        try:
            raw = await asyncio.to_thread(face_reply, r.endpoint_of("codex-a"),
                                          frame_to(box, s.path, "em", "hello there"))
            assert raw == b""
            await until(lambda: log)
        finally:
            s.close()
            await r.stop()
    run(go())


THR_ROOT = "01a1b629-0934-71f1-aba9-95987bbc9c6b"
THR_V2 = "01a1b62b-c998-7eb1-90fe-a2d18963d8d7"
THR_V2_ORPHAN = "01a1b62b-0000-7000-8000-0000000000ff"
REPLY_TO_ROOT = r"--to '?codex-root'? '<your reply>'$"


def v2_router(dirs, monkeypatch, log):
    spawn = lambda parent: {"cwd": "/w", "source": json.dumps({"subagent": {"thread_spawn": {"parent_thread_id": parent}}}),
                            "thread_source": "subagent", "agent_path": "/root/worker"}
    rows = {THR_ROOT: ROOT_ROW, THR_V2: spawn(THR_ROOT),
            THR_V2_ORPHAN: {"cwd": "/w", "source": "subagent", "thread_source": "subagent", "agent_path": "/root/x"}}
    monkeypatch.setattr(pr, "codex_thread_metadata", lambda home: rows)
    return make_router(dirs, echo_adapters(log), [{"name": "codex-root", "vendor": "codex", "target": THR_ROOT},
                                                 {"name": "agy-a", "vendor": "agy", "target": "conv-1"}])


@portable
def test_v2_subagent_sender_trailer_replies_to_its_root_parent(dirs, monkeypatch):
    async def go():
        log = []
        r = v2_router(dirs, monkeypatch, log)
        resp = await r._dispatch({"op": "send", "as_target": THR_V2, "as": "cli", "to": "agy-a", "text": "status?"})
        assert resp["ok"] is True
        trailer = log[0][1].split("\n\n")[1]
        assert re.search(REPLY_TO_ROOT, trailer)
        assert THR_V2 not in trailer.split("Reply to them with:")[1]
        assert log[0][1].startswith(f"[codex-{THR_V2}] status?")
        plain = await r._dispatch({"op": "send", "as_target": THR_ROOT, "to": "agy-a", "text": "root asks"})
        assert plain["ok"] is True and re.search(REPLY_TO_ROOT, log[1][1])
    run(go())


@portable
def test_send_to_a_v2_subagent_id_is_delivered_to_its_root_parent(dirs, monkeypatch):
    async def go():
        log = []
        r = v2_router(dirs, monkeypatch, log)
        r.peer_stale = lambda name: False
        for address in (THR_V2, f"codex-{THR_V2}"):
            resp = await r._dispatch({"op": "send", "as": "agy-a", "to": address, "text": "please look"})
            assert resp["ok"] is True
        assert [name for name, _ in log] == ["codex-root", "codex-root"]
    run(go())


@portable
def test_v2_subagent_without_a_resolvable_parent_is_refused_both_ways(dirs, monkeypatch):
    async def go():
        log = []
        r = v2_router(dirs, monkeypatch, log)
        as_sender = await r._dispatch({"op": "send", "as_target": THR_V2_ORPHAN, "to": "agy-a", "text": "hi there"})
        to_sub = await r._dispatch({"op": "send", "as": "agy-a", "to": THR_V2_ORPHAN, "text": "hi there"})
        unknown = await r._dispatch({"op": "send", "as": "agy-a", "to": "codex-unknown", "text": "hi there"})
        assert as_sender["ok"] is False and "no resolvable sendable root parent" in as_sender["error"]
        assert to_sub["ok"] is False and "no resolvable sendable root parent" in to_sub["error"]
        assert unknown["ok"] is False and "no resolvable" not in unknown["error"]
        assert log == []
    run(go())


@portable
def test_codex_root_parent_walks_chain_and_stops_on_cycles():
    sub = lambda parent: {"source": {"subagent": {"thread_spawn": {"parent_thread_id": parent}}}}
    rows = {"a": sub("b"), "b": sub("c"), "c": ROOT_ROW, "x": sub("y"), "y": sub("x")}
    assert pr.codex_root_parent("a", rows.get) == "c"
    assert pr.codex_root_parent("x", rows.get) is None
    assert pr.codex_root_parent("c", rows.get) is None


def test_lock_peer_records_the_logging_codex_process_as_holder(tmp_path, monkeypatch):
    import sqlite3
    pr._ROLLOUT_VERDICTS.clear()
    monkeypatch.setattr(pr, "codex_thread_metadata", lambda home: {THR_LIVE: ROOT_ROW})
    write_rollout(tmp_path, THR_LIVE, "cli")
    conn = sqlite3.connect(tmp_path / "logs_1.sqlite")
    conn.execute("CREATE TABLE logs (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, "
                 "ts_nanos INTEGER NOT NULL, thread_id TEXT, process_uuid TEXT)")
    conn.execute("INSERT INTO logs (ts, ts_nanos, thread_id, process_uuid) VALUES (1, 0, ?, ?)",
                 (THR_LIVE, f"pid:{os.getpid()}:u"))
    conn.commit()
    conn.close()
    d = tmp_path / "locks"
    fd = hold(d / f"{THR_LIVE}.lock")
    try:
        (peer,) = pr.load_lock_peers(d, "codex")
    finally:
        os.close(fd)
    assert peer["codexPid"] == os.getpid()
    assert pr._liveness().codex_holder_live(peer) is True
