"""Behavior pins for the unimatrix per-box peer router (real faces, fake adapters)."""
import asyncio
import importlib.util
import json
import os
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

pytestmark = [pytest.mark.complete, pytest.mark.skipif(os.name == "nt", reason="unix sockets")]


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


def test_lock_peers_need_a_held_lock_and_peers_json_wins(tmp_path):
    d = tmp_path / "locks"
    fds = [hold(d / "thr-live.lock"), hold(d / "thr-1.lock"), hold(d / ".coordination.lock")]
    (d / "thr-stale.lock").write_text("")
    (d / "notes.txt").write_text("")
    try:
        got = pr.load_lock_peers(d, "codex", {p["name"] for p in PEERS}, {p["target"] for p in PEERS})
        assert got == [{"name": pr.readable_peer_name("codex", "thr-live"), "vendor": "codex", "target": "thr-live"}]
        assert pr.load_lock_peers(tmp_path / "missing", "codex") == []
    finally:
        for fd in fds:
            os.close(fd)
    assert pr.load_lock_peers(d, "codex") == []  # released locks are stale


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
