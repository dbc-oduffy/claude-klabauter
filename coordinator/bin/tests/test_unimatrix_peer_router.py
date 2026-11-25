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
import subprocess
import sys
import tempfile
import time
import threading
import types
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


class _AnyRoot(dict):
    """State-DB stand-in in which every thread id is a sendable CLI root."""

    def get(self, key, default=None):
        return ROOT_ROW

    def __contains__(self, key):
        return True

    def __getitem__(self, key):
        return ROOT_ROW


def bind_codex(settings, monkeypatch, *targets):
    """Write the engine address binding discovery requires for each Codex thread; returns target -> name."""
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings))
    directory = Path(settings) / "host-sessions" / "codex"
    directory.mkdir(parents=True, exist_ok=True)
    names = {}
    for n, target in enumerate(targets, 1):
        names[target] = f"w-codex-box-{n}"
        (directory / f"{target}.json").write_text(json.dumps({
            "schema_version": 1, "provider": "codex", "session_id": target, "address_version": 1,
            "name": names[target], "box": socket.gethostname()}), encoding="utf-8")
    return names


@pytest.fixture(autouse=True)
def _codex_state_db_reads_every_thread_as_a_root(monkeypatch):
    """Never read the box's real Codex state DB; tests needing other rows re-patch it."""
    monkeypatch.setattr(pr, "codex_thread_metadata", lambda home: _AnyRoot())


@pytest.fixture
def dirs(tmp_path):
    for sub in ("sessions", "eps", "state"):
        (tmp_path / sub).mkdir()
    return tmp_path


@pytest.fixture(autouse=True)
def _isolated_mirror_markers(tmp_path, monkeypatch):
    # The class default reads the box's live /tmp/cc-socks; real markers there leak foreign rows into every roster.
    monkeypatch.setattr(pr.Router, "mirror_stale_dir", str(tmp_path / "mirror-stale"))


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


def post_frame(endpoint, frame, sessions_dir=None):
    data = (json.dumps(frame) + "\n").encode()
    sdir = sessions_dir or (Path(endpoint).parent.parent / "sessions")
    if sdir.is_dir():
        data = pr._roster().authenticated_data(endpoint, data, sdir)
    s = socket.socket(socket.AF_UNIX)
    s.connect(endpoint)
    s.sendall(data)
    s.close()


async def until(pred, timeout=10):
    for _ in range(int(timeout / 0.05)):
        if pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached")


THR_1 = "01a1b6aa-0000-7000-8000-000000000001"
THR_2 = "01a1b6aa-0000-7000-8000-000000000003"
PEERS = [{"name": "codex-a", "vendor": "codex", "target": THR_1, "codexPid": os.getpid()},
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


def test_peer_to_agy_send_delivers_without_waiting_for_final_reply(box):
    async def go():
        seen = []

        async def agy(peer, text):
            seen.append((peer.get("deliver_only"), text))
            if peer.get("deliver_only"):
                return {"written": True, "msg_id": "agy-msg-1"}
            return "full reply"

        async def codex(peer, text):
            return "codex reply"

        r = make_router(box, {"agy": agy, "codex": codex})
        await r.start()
        try:
            resp = await asyncio.to_thread(pr.request_line, r.ctl,
                                           {"op": "send", "as": "codex-a", "to": "agy-a", "text": "ping"})
            assert resp == {"ok": True, "written": True, "msg_id": "agy-msg-1"}
            cli_resp = await asyncio.to_thread(pr.request_line, r.ctl,
                                               {"op": "send", "as": "cli", "to": "agy-a", "text": "cli ping"})
            assert cli_resp["ok"] is True and cli_resp["reply"] == "[agy-a] full reply"
            assert [flag for flag, _ in seen] == [True, None]
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


@pytest.fixture(autouse=True)
def _agy_state_under_tmp(tmp_path, monkeypatch):
    """No agy session probe reads the box's real presence dir or settings home."""
    monkeypatch.setenv("AGY_BRAIN_DIR", str(tmp_path / "agy" / "brain"))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))


def write_leg(legs, conv, pid=None, proc_start="stamp", **extra):
    legs.mkdir(parents=True, exist_ok=True)
    pid = os.getpid() if pid is None else pid
    rec = {"pid": pid, "conversation": conv, "endpoint": "x", "ls_address": "y", "agy_pid": os.getpid(), **extra}
    if proc_start == "stamp":
        try:
            rec["procStart"] = pr._roster().proc_start(pid)
        except (OSError, subprocess.CalledProcessError):  # a dead pid: no stamp to record
            pass
    elif proc_start is not None:
        rec["procStart"] = proc_start
    (legs / f"{conv}.json").write_text(json.dumps(rec))


@portable
def test_leg_with_live_pid_and_mismatched_proc_start_is_skipped(tmp_path):
    write_leg(tmp_path, "conv-reused", proc_start="not-this-process-start")
    assert pr.load_leg_peers(tmp_path) == []


@portable
def test_leg_record_without_proc_start_is_skipped(tmp_path):
    write_leg(tmp_path, "conv-unstamped", proc_start=None)
    assert pr.load_leg_peers(tmp_path) == []
    write_leg(tmp_path, "conv-stamped")
    assert [p["target"] for p in pr.load_leg_peers(tmp_path)] == ["conv-stamped"]


def test_static_agy_row_is_served_only_while_its_leg_is_live(box):
    async def go():
        legs = box / "legs"
        static = {"name": "agy-static", "vendor": "agy", "target": "conv-static"}
        r = pr.Router([static], sessions_dir=box / "sessions", state_dir=box / "state",
                      endpoint_dir=box / "eps", adapters=echo_adapters([]), legs_dir=legs)
        await r.start()
        try:
            assert "agy-static" in r.dead_holders and "agy-static" not in r.faces  # no leg record
            write_leg(legs, "conv-static", proc_start="stale-boot")
            await r.refresh_discovered()
            assert "agy-static" in r.dead_holders
            write_leg(legs, "conv-static")
            await r.refresh_discovered()
            assert "agy-static" not in r.dead_holders
            await r._repair_faces()
            assert "agy-static" in r.faces
            write_leg(legs, "conv-static", proc_start="stale-boot")
            await r.refresh_discovered()
            assert "agy-static" in r.dead_holders and "agy-static" not in r.faces
        finally:
            await r.stop()
    run(go())


def test_leg_records_become_peers_and_peers_json_wins(tmp_path):
    legs = tmp_path / "legs"
    write_leg(legs, "conv-new")
    write_leg(legs, "conv-1")  # already in peers.json by target
    write_leg(legs, "conv-dead", pid=2 ** 22 + 12345)
    (legs / "junk.json").write_text("{")
    got = pr.load_leg_peers(legs, {p["name"] for p in PEERS}, {p["target"] for p in PEERS})
    assert got == [{"name": pr.readable_peer_name("agy", "conv-new"), "vendor": "agy", "target": "conv-new"}]
    assert pr.load_leg_peers(tmp_path / "missing") == []


DEAD_AGY_PID = 2 ** 22 + 12345


def _past_grace_ms():
    return int((time.time() - pr._liveness().AGY_SESSION_GRACE_SECONDS - 30) * 1000)


def _hold_presence(conv):
    """Hold the conversation's agy presence lock the way agy does; returns the file to close."""
    path = Path(pr.default_presence_dir()) / f"{conv}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0")
    held = open(path, "r+b")
    assert pr._transport().lock_file(held)
    return held


@portable
def test_leg_whose_agy_session_is_dead_gets_no_peer_though_its_leg_lives(tmp_path):
    legs = tmp_path / "legs"
    write_leg(legs, "conv-leaked", agy_pid=DEAD_AGY_PID)
    write_leg(legs, "conv-no-agy-pid", agy_pid=None, startedAt=_past_grace_ms())
    assert pr.load_leg_peers(legs) == []
    write_leg(legs, "conv-young", agy_pid=None, startedAt=int(time.time() * 1000))  # inside the grace: unknown
    write_leg(legs, "conv-live")
    assert sorted(p["target"] for p in pr.load_leg_peers(legs)) == ["conv-live", "conv-young"]


@portable
def test_leg_target_live_requires_the_agy_session_not_dead(tmp_path):
    legs = tmp_path / "legs"
    write_leg(legs, "conv-orphan", agy_pid=DEAD_AGY_PID)  # leg pid live, agy app dead
    assert pr.leg_record_live(json.loads((legs / "conv-orphan.json").read_text()))
    assert not pr.leg_target_live(legs, "conv-orphan")
    write_leg(legs, "conv-orphan")
    assert pr.leg_target_live(legs, "conv-orphan")
    held = _hold_presence("conv-held")
    write_leg(legs, "conv-held", agy_pid=DEAD_AGY_PID)
    try:
        assert pr.leg_target_live(legs, "conv-held")
    finally:
        held.close()
    assert not pr.leg_target_live(legs, "conv-held")


def test_orphaned_leg_with_dead_agy_app_is_not_live_and_its_face_is_not_respawned(box):
    async def go():
        legs = box / "legs"
        static = {"name": "agy-orphan", "vendor": "agy", "target": "conv-orphan"}
        write_leg(legs, "conv-orphan", agy_pid=DEAD_AGY_PID)
        r = pr.Router([static], sessions_dir=box / "sessions", state_dir=box / "state",
                      endpoint_dir=box / "eps", adapters=echo_adapters([]), legs_dir=legs)
        await r.start()
        try:
            for _ in range(3):
                await r.refresh_discovered()
                await r._repair_faces()
                assert "agy-orphan" in r.dead_holders and "agy-orphan" not in r.faces
            write_leg(legs, "conv-orphan")  # a live agy app revives it
            assert pr.leg_target_live(legs, "conv-orphan")
        finally:
            await r.stop()
    run(go())


def test_router_restart_registers_no_face_for_a_row_whose_leg_and_agy_pids_are_both_dead(box):
    async def go():
        legs = box / "legs"
        static = {"name": "agy-both-dead", "vendor": "agy", "target": "conv-both-dead"}
        write_leg(legs, "conv-both-dead", pid=DEAD_AGY_PID, agy_pid=DEAD_AGY_PID)
        r = pr.Router([static], sessions_dir=box / "sessions", state_dir=box / "state",
                      endpoint_dir=box / "eps", adapters=echo_adapters([]), legs_dir=legs)
        await r.start()
        try:
            for _ in range(3):
                await r.refresh_discovered()
                await r._repair_faces()
            assert "agy-both-dead" in r.dead_holders and "agy-both-dead" not in r.faces
            assert list((box / "sessions").glob("*.json")) == []  # no registration, so no fresh startedAt
        finally:
            await r.stop()
    run(go())


@portable
def test_leg_with_a_dead_agy_pid_keeps_its_peer_while_the_presence_lock_is_held(tmp_path):
    legs = tmp_path / "legs"
    write_leg(legs, "conv-held", agy_pid=DEAD_AGY_PID)
    held = _hold_presence("conv-held")
    try:
        assert [p["target"] for p in pr.load_leg_peers(legs)] == ["conv-held"]
    finally:
        held.close()
    assert pr.load_leg_peers(legs) == []


@portable
def test_pending_leg_gets_no_peer_and_is_not_target_live_but_live_or_absent_mode_is(tmp_path):
    legs = tmp_path / "legs"
    write_leg(legs, "conv-pending", mode="pending")
    assert not pr.leg_record_live(json.loads((legs / "conv-pending.json").read_text()))
    assert pr.load_leg_peers(legs) == []
    assert not pr.leg_target_live(legs, "conv-pending")
    write_leg(legs, "conv-pending", mode="live")
    write_leg(legs, "conv-legacy")  # no mode field: reads live
    assert sorted(p["target"] for p in pr.load_leg_peers(legs)) == ["conv-legacy", "conv-pending"]
    assert pr.leg_target_live(legs, "conv-pending") and pr.leg_target_live(legs, "conv-legacy")


@portable
def test_pending_leg_with_a_live_process_fills_pending_out_never_the_peer_list(tmp_path):
    legs = tmp_path / "legs"
    write_leg(legs, "conv-pend", mode="pending")
    write_leg(legs, "conv-pend-dead", pid=DEAD_AGY_PID, mode="pending")
    write_leg(legs, "conv-pend-reused", proc_start="not-this-process-start", mode="pending")
    write_leg(legs, "conv-live", mode="live")
    pending = {}
    assert [p["target"] for p in pr.load_leg_peers(legs, pending_out=pending)] == ["conv-live"]
    assert {p["target"] for p in pending.values()} == {"conv-pend"}
    assert [p["target"] for p in pr.load_leg_peers(legs)] == ["conv-live"]  # no pending_out: pending legs skipped as before


def test_list_reports_a_pending_agy_leg_pending_a_live_one_ready_and_a_send_answers_queued(box, capsys, monkeypatch):
    queued = "[unimatrix-peer-agy] queued: the agy leg is not live; the message is spooled"
    seen = []

    async def agy(peer, text):
        seen.append((peer["name"], peer.get("deliver_only")))
        return queued

    async def go():
        legs = box / "legs"
        write_leg(legs, "conv-pend", mode="pending")
        write_leg(legs, "conv-live", mode="live")
        write_leg(legs, "conv-gone", pid=DEAD_AGY_PID, mode="pending")
        r = pr.Router(PEERS[:1], sessions_dir=box / "sessions", state_dir=box / "state",
                      endpoint_dir=box / "eps", adapters={**echo_adapters(), "agy": agy}, legs_dir=legs)
        await r.start()
        try:
            resp = await r._dispatch({"op": "list"})
            pend, live = (pr.readable_peer_name("agy", c) for c in ("conv-pend", "conv-live"))
            assert pend in resp["reachable"] and pend not in resp["peers"] and pend not in resp["ready"]
            assert live in resp["reachable"] and live in resp["peers"]
            rows = {row["name"]: row for row in resp["agents"]}
            assert rows[pend]["leg_mode"] == "pending" and rows[pend]["vendor"] == "agy"
            assert "leg_mode" not in rows[live] and "leg_mode" not in rows["codex-a"]
            assert pr.readable_peer_name("agy", "conv-gone") not in resp["reachable"]
            sent = await r._dispatch({"op": "send", "to": pend, "text": "hello", "as": "cli"})
            assert sent["ok"] is True and "queued" in sent["reply"] and seen == [(pend, True)]
            m = _send_module()
            monkeypatch.setattr(m, "own_target", lambda: "")
            rc = await asyncio.to_thread(m.main, ["--endpoint-dir", str(box / "eps"), "--list"])
            return rc, pend, live
        finally:
            await r.stop()
    rc, pend, live = run(go())
    assert rc == 0
    cells = {line.split("\t")[0]: line.split("\t")[4] for line in capsys.readouterr().out.splitlines() if line.count("\t") >= 4}
    assert cells[pend] == "pending" and cells[live] == "ready" and cells["codex-a"] == "ready"


@portable
def test_unstarted_router_lists_a_pending_leg_pending_and_a_send_to_it_answers_queued(tmp_path):
    queued = "[unimatrix-peer-agy] queued: the agy leg is not live; the message is spooled"
    seen = []

    async def agy(peer, text):
        seen.append((peer["name"], peer.get("deliver_only")))
        return queued

    legs = tmp_path / "legs"
    write_leg(legs, "conv-pend", mode="pending")
    write_leg(legs, "conv-live", mode="live")
    write_leg(legs, "conv-gone", pid=DEAD_AGY_PID, mode="pending")
    (tmp_path / "sessions").mkdir()
    r = pr.Router([], sessions_dir=tmp_path / "sessions", state_dir=tmp_path / "state",
                  adapters={**echo_adapters(), "agy": agy}, legs_dir=legs)
    live = r._live_discovered()
    pend, up, gone = (pr.readable_peer_name("agy", c) for c in ("conv-pend", "conv-live", "conv-gone"))
    assert list(live) == [up] and list(r.pending_legs) == [pend]
    r.peers.update(live)
    reachable, agents = r.roster_rows("cli", {up: True})
    rows = {row["name"]: row for row in agents}
    assert sorted(reachable) == sorted([pend, up]) and gone not in reachable
    assert rows[pend]["leg_mode"] == "pending" and "leg_mode" not in rows[up]
    sent = run(r._dispatch({"op": "send", "to": pend, "text": "hello", "as": "cli"}))
    assert sent["ok"] is True and "queued" in sent["reply"] and seen == [(pend, True)]
    assert run(r._dispatch({"op": "send", "to": pend, "text": "ok", "as": "cli"}))["ok"] is False  # an ack is never delivered


def test_a_pending_leg_that_goes_live_leaves_the_pending_map_and_joins_the_peers(box):
    async def go():
        legs = box / "legs"
        write_leg(legs, "conv-9", mode="pending")
        r = pr.Router([], sessions_dir=box / "sessions", state_dir=box / "state",
                      endpoint_dir=box / "eps", adapters=echo_adapters([]), legs_dir=legs)
        await r.start()
        try:
            name = pr.readable_peer_name("agy", "conv-9")
            assert name in r.pending_legs and name not in r.peers
            write_leg(legs, "conv-9", mode="live")
            await r.refresh_discovered()
            assert name in r.peers and name not in r.pending_legs
        finally:
            await r.stop()
    run(go())


@portable
def test_agy_lock_peers_exist_only_while_the_presence_lock_is_held(tmp_path):
    d = tmp_path / "presence"
    d.mkdir()
    (d / "conv-stale.lock").write_bytes(b"\0")
    assert pr.load_lock_peers(d, "agy") == []
    held = _hold_presence("conv-live")
    try:
        assert [p["target"] for p in pr.load_lock_peers(pr.default_presence_dir(), "agy")] == ["conv-live"]
    finally:
        held.close()
    assert pr.load_lock_peers(pr.default_presence_dir(), "agy") == []


@portable
def test_lock_held_is_the_liveness_presence_probe(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(pr._liveness(), "agy_presence_held",
                        lambda conv, presence_dir=None: seen.append((conv, presence_dir)) or True)
    assert pr.lock_held(tmp_path / "conv-x.lock") is True
    assert seen == [("conv-x", tmp_path)]


def test_router_retires_a_face_once_its_agy_session_goes_dead(box):
    async def go():
        legs = box / "legs"
        r = pr.Router([], sessions_dir=box / "sessions", state_dir=box / "state",
                      endpoint_dir=box / "eps", adapters=echo_adapters([]), legs_dir=legs)
        await r.start()
        try:
            write_leg(legs, "conv-9")
            await r.refresh_discovered()
            name = pr.readable_peer_name("agy", "conv-9")
            assert name in r.peers and name in r.faces
            write_leg(legs, "conv-9", agy_pid=DEAD_AGY_PID)  # the leg leaked on; its agy session is gone
            await r.refresh_discovered()
            assert name not in r.peers and name not in r.faces and name not in r.discovered
            await r.refresh_discovered()
            assert name not in r.peers  # a rescan does not resurrect it
        finally:
            await r.stop()
    run(go())


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


def _queue_files(tmp_path, monkeypatch, **sessions):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    qdir = tmp_path / "unimatrix" / "queue"
    qdir.mkdir(parents=True)
    for sid, body in sessions.items():
        (qdir / f"{sid}.jsonl").write_text(body)
        (qdir / f"{sid}.drain.lock").write_text("")
    relay = pr._sibling("unimatrix-relay.py", "unimatrix_relay")
    monkeypatch.setattr(relay, "_relay_queue", lambda: type("Q", (), {"queue_dir": staticmethod(lambda: qdir)})(), raising=False)
    return relay


@portable
def test_queue_drain_runs_the_relay_drain_only_for_sessions_with_queued_lines(tmp_path, monkeypatch):
    relay = _queue_files(tmp_path, monkeypatch, sidfull='{"id": "1"}\n', sidempty="")
    drained = []
    monkeypatch.setattr(relay, "drain", lambda sid: drained.append(sid) or {"ok": True}, raising=False)
    assert pr._drain_queued_sessions() == []
    assert drained == ["sidfull"]


@portable
def test_queue_drain_error_for_one_session_never_skips_the_next(tmp_path, monkeypatch):
    relay = _queue_files(tmp_path, monkeypatch, sida='{"id": "1"}\n', sidb='{"id": "2"}\n')
    drained = []

    def drain(sid):
        drained.append(sid)
        if sid == "sida":
            raise OSError("boom")
        return {"ok": False, "error": "no unique live local session"}

    monkeypatch.setattr(relay, "drain", drain, raising=False)
    assert pr._drain_queued_sessions() == [("sida", "OSError: boom")]
    assert drained == ["sida", "sidb"]


@portable
def test_scan_tick_drains_queues_and_a_drain_fault_never_stops_the_scan(tmp_path, monkeypatch):
    async def go():
        r = pr.Router([], sessions_dir=tmp_path, state_dir=tmp_path, endpoint_dir=tmp_path, adapters=echo_adapters())
        monkeypatch.setattr(pr, "LEG_SCAN_SECONDS", 0.01)
        ticks = []

        async def noop(*a):
            return None

        def drain():
            ticks.append(1)
            if len(ticks) == 1:
                raise RuntimeError("first tick faults")
            return [("sidx", "OSError: x")]

        monkeypatch.setattr(pr, "_drain_queued_sessions", drain)
        for name in ("check_control_socket", "refresh_discovered", "_repair_faces"):
            monkeypatch.setattr(r, name, noop)
        task = asyncio.ensure_future(r._scan_discovered())
        await until(lambda: len(ticks) >= 3)
        task.cancel()
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
THR_STALE = "01a1b6aa-0000-7000-8000-000000000002"
THR_7 = "01a1b6aa-0000-7000-8000-000000000007"
ROOT_ROW = {"cwd": "/w", "source": "cli", "thread_source": "user", "agent_path": None}


def write_rollout(home, thread_id, source):
    day = home / "sessions" / "2026" / "11" / "08"
    day.mkdir(parents=True, exist_ok=True)
    meta = {"type": "session_meta", "payload": {"id": thread_id, "source": source}}
    (day / f"rollout-2026-11-08T00-00-00-{thread_id}.jsonl").write_text(
        json.dumps(meta) + "\n" + json.dumps({"type": "event_msg", "payload": {}}) + "\n")


def test_lock_peers_need_a_held_lock_and_peers_json_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "codex_thread_metadata", lambda home: {
        t: ROOT_ROW for t in (THR_LIVE, THR_1, THR_STALE)})
    names = bind_codex(tmp_path / "settings", monkeypatch, THR_LIVE, THR_1)
    write_rollout(tmp_path, THR_LIVE, "cli")
    d = tmp_path / "locks"
    fds = [hold(d / f"{THR_LIVE}.lock"), hold(d / f"{THR_1}.lock"), hold(d / ".coordination.lock")]
    (d / f"{THR_STALE}.lock").write_text("")
    (d / "notes.txt").write_text("")
    try:
        got = pr.load_lock_peers(d, "codex", {p["name"] for p in PEERS}, {p["target"] for p in PEERS})
        assert [(p["name"], p["vendor"], p["target"], p["cwd"]) for p in got] == [
            (names[THR_LIVE], "codex", THR_LIVE, "/w")]
        assert pr.load_lock_peers(tmp_path / "missing", "codex") == []
    finally:
        for fd in fds:
            os.close(fd)
    assert pr.load_lock_peers(d, "codex") == []  # released locks are stale


def test_lock_peers_skip_rollout_subagents_and_threads_without_a_rollout(tmp_path, monkeypatch):
    pr._ROLLOUT_VERDICTS.clear()
    monkeypatch.setattr(pr, "codex_thread_metadata", lambda home: {
        t: ROOT_ROW for t in (THR_LIVE, THR_SUB, THR_NOROLL)})  # state DB reads root for all three
    bind_codex(tmp_path / "settings", monkeypatch, THR_LIVE, THR_SUB, THR_NOROLL)
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
    bind_codex(tmp_path / "settings", monkeypatch, *ids)
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


def test_router_with_no_peers_json_registers_locks_and_retires_them_when_released(box, monkeypatch):
    cxn = bind_codex(box / "settings", monkeypatch, THR_7)[THR_7]
    monkeypatch.setattr(pr, "codex_rollout_addressable", lambda *a: True)
    monkeypatch.setattr(pr._liveness(), "codex_thread_holder", lambda home, ident: {"codexPid": os.getpid()})

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
            fds["codex"] = hold(cx / f"{THR_7}.lock")
            fds["agy"] = hold(ag / "conv-7.lock")
            await r.refresh_discovered()
            assert sorted(r.peers) == sorted([pr.readable_peer_name("agy", "conv-7"), cxn])
            await r.refresh_discovered()  # idempotent
            assert sorted(r.faces) == [pr.readable_peer_name("agy", "conv-7"), cxn]
            post_frame(r.endpoint_of(cxn), frame_to(box, s.path, "em", "hello"))
            await until(lambda: len(s.frames) == 1)
            assert log[0][0] == cxn
            assert r.peers[cxn]["tui"] is True and "tui" not in r.peers[pr.readable_peer_name("agy", "conv-7")]
            ep, pid = r.endpoint_of(cxn), r.faces[cxn].pid
            os.close(fds.pop("codex"))
            await r.refresh_discovered()
            assert sorted(r.peers) == [pr.readable_peer_name("agy", "conv-7")]
            assert cxn not in r.faces and cxn not in r.locks
            assert not os.path.exists(ep) and not os.path.exists(box / "sessions" / f"{pid}.json")
        finally:
            for fd in fds.values():
                os.close(fd)
            s.close()
            await r.stop()
    run(go())


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
    (tmp_path / "proj" / ".git").mkdir(parents=True)
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


def test_default_adapters_grok_delivers_to_the_agent_inbox(monkeypatch, tmp_path):
    assert "grok" in pr.default_adapters()

    argvs = []
    real = pr._load("unimatrix-peer-grok.py", "unimatrix_peer_grok_probe")
    real.mark_resumable(tmp_path, "sid-1", "/w/repo")
    inbox = real.AgentInbox(tmp_path, "sid-1")
    inbox.start()
    monkeypatch.setattr(real.subprocess, "run", lambda argv, **kw: argvs.append((argv, kw)) or (_ for _ in ()).throw(AssertionError("resume")))
    monkeypatch.setattr(pr, "_load", lambda filename, modname: real)
    peer = {"target": "sid-1", "cwd": "/w/repo", "grok_home": str(tmp_path)}
    try:
        reply = run(pr.default_adapters()["grok"](peer, "ping"))
    finally:
        inbox.stop()
    assert reply["written"] is True and reply["channel"] == "agent-inbox" and argvs == []
    assert inbox.lines == ["UNIMATRIX-AGENT-MAIL ping"]


@portable
def test_default_adapters_reload_an_adapter_whose_source_changed_and_keep_an_unchanged_one(monkeypatch):
    loads = []
    real = pr._load("unimatrix-peer-grok.py", "unimatrix_peer_grok_probe")

    class Proc:
        returncode = 0
        stdout = json.dumps({"text": "ok"})

    monkeypatch.setattr(real.subprocess, "run", lambda argv, **kw: Proc())
    monkeypatch.setattr(pr, "_load", lambda filename, modname: loads.append(filename) or real)
    grok = pr.default_adapters()["grok"]
    peer = {"target": "sid-1", "cwd": "/w/repo"}
    run(grok(peer, "a"))
    run(grok(peer, "b"))
    assert loads == ["unimatrix-peer-grok.py"]  # unchanged source: loaded once
    src = pr.here() / "unimatrix-peer-grok.py"
    real_stat = Path.stat

    def edited_stat(self, *a, **kw):
        st = real_stat(self, *a, **kw)
        if self == src:
            return types.SimpleNamespace(st_mtime_ns=st.st_mtime_ns + 5_000_000_000, st_size=st.st_size)
        return st

    monkeypatch.setattr(Path, "stat", edited_stat)
    run(grok(peer, "c"))
    assert loads == ["unimatrix-peer-grok.py"] * 2  # edited source: reloaded on the next send


def test_peers_json_entry_survives_its_lock_going_and_beats_a_discovered_clash(box):
    async def go():
        cx = box / "cx"
        r = pr.Router(PEERS, sessions_dir=box / "sessions", state_dir=box / "state",
                      endpoint_dir=box / "eps", adapters=echo_adapters(), codex_locks_dir=cx)
        await r.start()
        fd = hold(cx / f"{THR_1}.lock")  # same target as the peers.json codex-a
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


def test_peer_send_frame_is_stamped_with_sender_name_and_box(box):
    async def go():
        s = FakeSender(str(box / "s.sock"))
        register_session(box, "em", s.path)
        r = make_router(box, echo_adapters())
        await r.start()
        try:
            resp = await r._dispatch({"op": "send", "as": "codex-a", "to": "em", "text": "stamp me"})
            assert resp["ok"] is True
            await until(lambda: len(s.frames) == 1)
            here = pr._contract().local_box()
            assert f'from-name="codex-a" from-box="{here}"' in s.contents()[0]
            assert s.frames[0]["provenance"]["box"] == here and s.frames[0]["from_name"] == "codex-a"
        finally:
            s.close()
            await r.stop()
    run(go())


def _frames_sent_to(dirs, monkeypatch, row, sender):
    """The frames a send from `sender` to the session `em` writes, delivery captured (no sockets)."""
    sent = []
    (dirs / "sessions" / "em.json").write_text(json.dumps(
        {"name": "em", "pid": os.getpid(), "messagingSocketPath": str(dirs / "em.sock"), "startedAt": 1,
         **({"unimatrixFarSeen": int(time.time() * 1000)} if row else {}), **row}))
    r = pr.Router([{"name": "codex-a", "vendor": "agy", "target": "conv-1"}], sessions_dir=dirs / "sessions",
                  state_dir=dirs / "state", endpoint_dir=dirs / "eps", adapters={})

    async def capture(peer, endpoint, frame):
        sent.append(frame)
    monkeypatch.setattr(r, "_deliver_as_peer", capture)
    monkeypatch.setattr(r, "_transport_face", lambda source, destination: asyncio.sleep(0, "transport-x"))
    resp = run(r._dispatch({"op": "send", "as": sender, "to": "em", "text": "hello"}))
    assert resp["ok"] is True
    return r, sent


FAR_ROW = {"unimatrixRemote": "user@far-box", "unimatrixFarName": "em"}


@portable
def test_peer_send_to_a_far_session_carries_the_peer_name_as_the_reply_address(dirs, monkeypatch):
    r, sent = _frames_sent_to(dirs, monkeypatch, FAR_ROW, "codex-a")
    assert sent[0]["message"]["content"].startswith(f'<cross-session-message from="codex-a.{pr._contract().local_box()}" from-name="codex-a"')
    assert sent[0]["from"] == f"uds:{r.endpoint_of('codex-a')}"


@portable
def test_reply_to_a_far_sender_carries_the_peer_name_as_the_reply_address_and_targets_that_sender(dirs, monkeypatch):
    sent = []
    em_sock = str(dirs / "em.sock")
    (dirs / "sessions" / "em.json").write_text(json.dumps(
        {"name": "em", "pid": os.getpid(), "messagingSocketPath": em_sock, "startedAt": 1, **FAR_ROW}))
    r = pr.Router([{"name": "codex-a", "vendor": "agy", "target": "conv-1"}], sessions_dir=dirs / "sessions",
                  state_dir=dirs / "state", endpoint_dir=dirs / "eps", adapters={})

    async def capture(peer, endpoint, frame):
        sent.append((endpoint, frame))

    async def turn(peer, text, addressed=None):
        return "answer"
    monkeypatch.setattr(r, "_deliver_as_peer", capture)
    monkeypatch.setattr(r, "turn", turn)
    inbound = pr._contract().build_frame(str(dirs / "stale.sock"), "em", "question")
    run(r.handle_inbound("codex-a", inbound))
    assert len(sent) == 1
    endpoint, frame = sent[0]
    assert endpoint == em_sock
    assert frame["message"]["content"].startswith(f'<cross-session-message from="codex-a.{pr._contract().local_box()}" from-name="codex-a"')


@portable
def test_peer_send_to_a_local_session_keeps_the_endpoint_as_the_reply_address(dirs, monkeypatch):
    r, sent = _frames_sent_to(dirs, monkeypatch, {}, "codex-a")
    assert sent[0]["message"]["content"].startswith(f'<cross-session-message from="uds:{r.endpoint_of("codex-a")}"')


@portable
def test_transport_send_from_an_unregistered_cli_carries_the_transport_face_as_the_reply_address(dirs, monkeypatch):
    # `cli.<box>` names no row, so a receiver's reply could never route back.
    r, sent = _frames_sent_to(dirs, monkeypatch, FAR_ROW, "cli")
    assert sent[0]["message"]["content"].startswith('<cross-session-message from="transport-x" ')


def _transport_router(dirs, monkeypatch):
    """A router with transport `t` bridging source session `em` and destination `other`; deliveries captured."""
    sent = []
    em_sock = str(dirs / "em.sock")
    (dirs / "sessions" / "em.json").write_text(json.dumps(
        {"name": "em", "pid": os.getpid(), "messagingSocketPath": em_sock, "startedAt": 1}))
    (dirs / "sessions" / "other.json").write_text(json.dumps(
        {"name": "other", "pid": os.getpid(), "messagingSocketPath": str(dirs / "other.sock"), "startedAt": 1}))
    r = pr.Router([], sessions_dir=dirs / "sessions", state_dir=dirs / "state", endpoint_dir=dirs / "eps",
                  adapters={})
    r.transport_sources["t"], r.transport_targets["t"] = "em", "other"

    async def capture(peer, endpoint, frame):
        sent.append((endpoint, frame))
    monkeypatch.setattr(r, "_deliver_as_peer", capture)
    return r, sent, em_sock


@portable
def test_transport_return_forwards_a_reply_once_and_drops_a_twice_wrapped_loop(dirs, monkeypatch):
    r, sent, em_sock = _transport_router(dirs, monkeypatch)
    other = str(dirs / "other.sock")
    run(r.handle_transport_inbound("t", pr._contract().build_frame(other, "other", "hello")))
    assert [e for e, _ in sent] == [em_sock]
    looped = "[forwarded message from a] [forwarded message from b] hello"
    run(r.handle_transport_inbound("t", pr._contract().build_frame(other, "other", looped)))
    assert len(sent) == 1


@portable
def test_transport_return_drops_a_frame_that_would_go_back_to_its_own_sender(dirs, monkeypatch):
    r, sent, _ = _transport_router(dirs, monkeypatch)
    run(r.handle_transport_inbound("t", pr._contract().build_frame(str(dirs / "stale.sock"), "em", "echo")))
    assert sent == []


@portable
def test_transport_return_drops_a_body_already_relayed_by_its_destination(dirs, monkeypatch):
    r, sent, _ = _transport_router(dirs, monkeypatch)
    other = str(dirs / "other.sock")
    run(r.handle_transport_inbound("t", pr._contract().build_frame(other, "other", "[em via Unimatrix transport] hello")))
    assert sent == []
    run(r.handle_transport_inbound("t", pr._contract().build_frame(other, "other", "[x via Unimatrix transport] hello")))
    assert len(sent) == 1


@portable
def test_hop_chain_names_the_leading_relays_outermost_first():
    assert pr.hop_chain("[forwarded message from a] [b via Unimatrix transport] x") == ["a", "b"]
    assert pr.hop_chain("x [forwarded message from a]") == []


@portable
def test_hop_count_counts_only_leading_relay_markers():
    assert pr.hop_count("[forwarded message from a] [b via Unimatrix transport] x") == 2
    assert pr.hop_count("x [forwarded message from a]") == 0


@portable
def test_send_receipt_names_the_session_reached_and_its_box(dirs, monkeypatch):
    (dirs / "sessions" / "em.json").write_text(json.dumps(
        {"name": "em", "pid": os.getpid(), "messagingSocketPath": str(dirs / "em.sock"), "startedAt": 1,
         "unimatrixFarSeen": int(time.time() * 1000), **FAR_ROW}))
    r = pr.Router([{"name": "codex-a", "vendor": "agy", "target": "conv-1"}], sessions_dir=dirs / "sessions",
                  state_dir=dirs / "state", endpoint_dir=dirs / "eps", adapters={})

    async def capture(peer, endpoint, frame):
        return None
    monkeypatch.setattr(r, "_deliver_as_peer", capture)
    monkeypatch.setattr(r, "_transport_face", lambda source, destination: asyncio.sleep(0, "transport-x"))
    resp = run(r._dispatch({"op": "send", "as": "cli", "to": "em", "text": "hello"}))
    assert resp["ok"] is True and resp["reached"] == "em" and resp["reached_box"]


@portable
def test_transport_send_to_an_unknown_name_is_refused_not_routed_elsewhere(dirs, monkeypatch):
    r, sent, _ = _transport_router(dirs, monkeypatch)
    resp = run(r._dispatch({"op": "send", "as": "cli", "to": "nobody", "text": "hello"}))
    assert resp["ok"] is False and "reached" not in resp and sent == []


@portable
def test_routable_from_swaps_only_the_wrapper_address_and_keeps_frame_from():
    frame = pr._contract().build_frame(r"\\.\pipe\LOCAL\cc-msg-x", "codex-a", 'body from="uds:keep"')
    out = pr.routable_from(frame, 'na"me')
    assert out["from"] == frame["from"]
    assert out["message"]["content"].startswith('<cross-session-message from="na\'me" from-name="codex-a"')
    assert 'body from="uds:keep"' in out["message"]["content"]
    assert frame["message"]["content"].startswith('<cross-session-message from="uds:')  # input frame untouched


def test_peer_send_to_unknown_session_fails_and_other_senders_turn_still_replies(box):
    async def go():
        r = make_router(box, echo_adapters())
        s = FakeSender(str(box / "s.sock"))
        await r.start()
        try:
            bad = await r._dispatch({"op": "send", "as": "codex-a", "to": "nobody", "text": "hi"})
            assert bad["ok"] is False
            self_send = await r._dispatch({"op": "send", "as": "codex-a", "to": "codex-a", "text": "hi"})
            assert self_send["ok"] is False and "cannot send to self" in self_send["error"]
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
        fd = hold(locks / f"{THR_1}.lock")
        (locks / f"{THR_2}.lock").write_text("")
        peers = PEERS + [{"name": "codex-b", "vendor": "codex", "target": THR_2, "codexPid": os.getppid()}]
        r = pr.Router(peers, sessions_dir=box / "sessions", state_dir=box / "state", endpoint_dir=box / "eps",
                      adapters={"codex": codex, "agy": codex}, codex_locks_dir=locks)
        await r.start()
        try:
            await r.turn("codex-a", "hi")
            await r.turn("codex-b", "hi")
        finally:
            os.close(fd)
            await r.stop()
        assert seen == [(THR_1, True), (THR_2, None)]
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
                stamped[e["name"]] = e.get("vendor")
            assert stamped == {"codex-a": "codex", "agy-a": "agy"}
        finally:
            await r.stop()
    run(go())


SPELLINGS = ["Machine-b.local", "machine-b", "Machine-b", "machine-b-lan.local"]


@pytest.mark.parametrize("host", SPELLINGS)
def test_box_identity_is_one_value_across_hostname_spellings(box, monkeypatch, host):
    monkeypatch.setattr(socket, "gethostname", lambda: host)
    assert pr.readable_peer_name("agy", "conv-1", "/x/repo").endswith("-on-machine-b")
    peer = {"name": "x", "vendor": "codex", "target": THR_1}
    reg = pr.Registration("x", str(box / "eps" / "x.sock"), str(box / "sessions"), vendor="codex", peer=peer)
    assert reg.data["box"] == "machine-b"

    async def go():
        r = make_router(box, echo_adapters())
        await r.start()
        try:
            resp = await r._dispatch({"op": "list"})
            assert {d["box"] for n, d in resp["details"].items() if n in ("agy-a", "codex-a")} == {"machine-b"}
        finally:
            await r.stop()
    run(go())


@pytest.mark.parametrize("far_box,local,expected", [
    ("Machine-b.local", "machine-b", False),
    ("machine-b-lan", "Machine-b.local", False),
    ("mac-mini.local", "Machine-b.local", True),
])
def test_far_row_check_compares_box_ids_not_raw_hostnames(box, monkeypatch, far_box, local, expected):
    monkeypatch.setattr(socket, "gethostname", lambda: local)
    roster = pr._roster()
    entry = {"name": "far", "vendor": "codex", "unimatrixRemote": "me@far", "macSocketPath": "uds:/x.sock",
             "box": far_box, "pid": os.getpid(), "farPid": 812, "procStart": roster.proc_start(os.getpid()),
             "pidDomain": roster.pid_domain(), "messagingSocketPath": "ep"}
    r = make_router(box, echo_adapters())
    assert pr.Router.external_codex_sendable(r, entry) is expected


def test_registration_without_a_vendor_writes_no_vendor_field(box):
    reg = pr.Registration("x", str(box / "eps" / "x.sock"), str(box / "sessions"))
    assert reg.data["vendor"] == "unknown"
    peer = {"name": "x", "vendor": "codex", "target": THR_1}
    assert pr.Registration("x", "e", str(box / "sessions"), vendor="codex", peer=peer).data["vendor"] == "codex"
    with pytest.raises(ValueError):
        pr.Registration("x", "e", str(box / "sessions"), vendor="codex")


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
            assert rows["codex-a"] == {"name": "codex-a", "box": here, "vendor": "codex", "this_box": True}
            assert rows["em"] == {"name": "em", "box": here, "vendor": "claude", "this_box": True}
            assert rows["far-codex"] == {"name": "far-codex", "box": "machine-b", "vendor": "codex",
                                         "this_box": "machine-b" == here}
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
            assert r.roster_rows("cli")[0] == ["codex-a", "em"]  # agy-a has no leg; stale has no socket
            assert r.roster_rows("em")[0] == ["codex-a"]
            resp = await r._dispatch({"op": "list", "as": "cli"})
            assert resp["reachable"] == ["codex-a", "em"] and "stale" not in resp["reachable"]
        finally:
            s.close()
    run(go())


def _mirror_row(box, file, name, sock_path, **extra):
    (box / "sessions" / f"{file}.json").write_text(json.dumps(
        {"name": name, "pid": os.getpid(), "messagingSocketPath": sock_path, "startedAt": 1, **extra}))


def test_a_relay_pushed_mirror_with_no_bridge_is_listed_mirror_only_and_a_bridged_one_is_not(box):
    async def go():
        r = make_router(box, echo_adapters())
        s1, s2 = FakeSender(str(box / "s1.sock")), FakeSender(str(box / "s2.sock"))
        try:
            _mirror_row(box, "push", "g-em.lan-a", s1.path, unimatrixMirror=True, unimatrixBox="boxa")
            _mirror_row(box, "bridge", "g-em.lan-b", s2.path, unimatrixMirror=True, unimatrixBox="boxb",
                        unimatrixRemote="me@boxb", macSocketPath="/far.sock")
            rows = {a["name"]: a for a in (await r._dispatch({"op": "list", "as": "cli"}))["agents"]}
            assert rows["g-em.lan-a"]["mirror_only"] is True
            assert "mirror_only" not in rows["g-em.lan-b"] and "mirror_only" not in rows["codex-a"]
        finally:
            s1.close()
            s2.close()
    run(go())


def test_a_name_with_a_bridge_row_beside_a_push_row_is_not_mirror_only(box):
    async def go():
        r = make_router(box, echo_adapters())
        s1, s2 = FakeSender(str(box / "s1.sock")), FakeSender(str(box / "s2.sock"))
        try:
            _mirror_row(box, "push", "g-em.lan", s1.path, unimatrixMirror=True)
            _mirror_row(box, "bridge", "g-em.lan", s2.path, unimatrixRemote="me@far", macSocketPath="/far.sock")
            rows = {a["name"]: a for a in r.roster_rows("cli")[1]}
            assert "mirror_only" not in rows["g-em.lan"]
        finally:
            s1.close()
            s2.close()
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
            assert "[cli via Unimatrix transport] hello" in f["message"]["content"]
            assert f["provenance"]["vendor"] == "transport"
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
            assert bad["ok"] is False and "transport delivery to em failed" in bad["error"] and "ENOINBOX" in bad["error"]
            assert "written" not in bad
        finally:
            s.close()
        os.unlink(str(box / "s.sock"))
        gone = await r._dispatch(send)
        assert gone["ok"] is False and "transport delivery to em failed" in gone["error"]
        unknown = await r._dispatch({**send, "to": "nobody"})
        assert unknown["ok"] is False and "no unique live session named nobody" in unknown["error"]
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


def test_grok_explicit_send_is_stamped_with_its_roster_name_and_replies_route_back(box):
    """A send whose as_target is a Grok session id arrives as the grok peer name, not a transport face."""
    async def go():
        home = box / "grok"
        home.mkdir()
        repo = box / "myrepo"
        (repo / ".git").mkdir(parents=True)
        sid = "0123abcd-1111-2222-3333-444455556666"
        (home / "active_sessions.json").write_text(
            json.dumps([{"session_id": sid, "cwd": str(repo), "pid": os.getpid()}]), encoding="utf-8")
        seen = []

        async def grok(peer, text):
            seen.append(text)
            return "grok heard"
        s = FakeSender(str(box / "s.sock"))
        register_session(box, "em", s.path)
        r = pr.Router([], sessions_dir=box / "sessions", state_dir=box / "state", endpoint_dir=box / "eps",
                      adapters={**echo_adapters(), "grok": grok}, grok_home=home)
        await r.start()
        try:
            want = f"grok-myrepo-0123abcd-on-{_box_slug()}"
            resp = await r._dispatch({"op": "send", "as_target": sid, "as": "cli", "to": "em", "text": "answer"})
            assert resp["ok"] is True and resp["written"] is True
            await until(lambda: len(s.frames) == 1)
            assert f"[{want}] answer" in s.contents()[0]
            assert "via Unimatrix transport" not in s.contents()[0] and "transport" not in resp.get("sender", "")
            back = await r._dispatch({"op": "send", "as": "em", "to": want, "text": "please continue"})
            assert back["ok"] is True and "grok heard" in back["reply"] and any("please continue" in t for t in seen)
        finally:
            s.close()
            await r.stop()
    run(go())


def test_transport_send_reports_written_when_delivered(box):
    async def go():
        s = FakeSender(str(box / "s.sock"))
        register_session(box, "em", s.path)
        r = make_router(box, echo_adapters())
        await r.start()
        try:
            resp = await r._dispatch({"op": "send", "as": "cli", "to": "em", "text": "hello there"})
            assert resp["ok"] is True and resp["written"] is True and resp["msg_id"]
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


@portable
@pytest.mark.parametrize("binding", ["live", "missing", "ambiguous", "stale"])
def test_claude_shell_binding_is_verified_before_naming_reply_address(dirs, monkeypatch, binding):
    row = {"name": "claude-em", "sessionId": "native-session", "pid": os.getpid(),
           "messagingSocketPath": "/s/em.sock", "vendor": "claude"}
    if binding != "missing":
        (dirs / "sessions" / "one.json").write_text(json.dumps(row))
    if binding == "ambiguous":
        (dirs / "sessions" / "two.json").write_text(json.dumps({**row, "messagingSocketPath": "/s/other.sock"}))
    if binding == "stale":
        monkeypatch.setattr(pr._liveness(), "entry_live", lambda row: False)
    seen = []
    r = make_router(dirs, echo_adapters(seen))
    result = run(r._dispatch({"op": "send", "as": "cli", "as_target": "native-session",
                              "to": "codex-a", "text": "nonce-shell"}))
    if binding == "live":
        assert result["ok"]
        assert "[claude-em] nonce-shell" in seen[0][1]
        assert "--to claude-em" in seen[0][1]
    else:
        assert not result["ok"]
        assert "sender" in result["error"]
        assert seen == []


@pytest.mark.parametrize("address,expected", [
    ("claude-em", "--to claude-em "),
    ("it's em", "--to 'it''s em' "),
    ("-x", "--to '-x' "),
    ("@x", "--to '@x' "),
])
def test_win32_reply_trailer_quotes_the_address_only_when_unsafe(monkeypatch, address, expected):
    monkeypatch.setattr(pr.sys, "platform", "win32")
    trailer = pr.wrap_inbound("codex-a", "body", reply_to=address)
    assert expected in trailer
    assert f"& '{pr.sys.executable}'" in trailer


@portable
def test_unbound_cli_does_not_advertise_a_reply_address(dirs):
    seen = []
    r = make_router(dirs, echo_adapters(seen))
    result = run(r._dispatch({"op": "send", "as": "cli", "to": "codex-a", "text": "nonce"}))
    assert result["ok"]
    assert "--to cli" not in seen[0][1]
    assert "No verified reply address" in seen[0][1]


def test_claude_shell_recipient_can_reply_to_verified_identity(box):
    async def go():
        receiver = FakeSender(str(box / "claude.sock"))
        row = {"name": "claude-em", "sessionId": "native-session", "pid": os.getpid(),
               "messagingSocketPath": receiver.path, "vendor": "claude"}
        (box / "sessions" / "claude.json").write_text(json.dumps(row))
        seen = []
        r = make_router(box, echo_adapters(seen))
        await r.start()
        try:
            result = await r._dispatch({"op": "send", "as": "cli", "as_target": "native-session",
                                        "to": "agy-a", "text": "nonce-shell"})
            assert result["ok"] and "--to claude-em" in seen[0][1]
            reply = await r._dispatch({"op": "send", "as_target": "conv-1", "as": "cli",
                                       "to": "claude-em", "text": "reply-nonce-shell"})
            assert reply["ok"] and reply["written"]
            await until(lambda: receiver.frames)
            assert "reply-nonce-shell" in receiver.contents()[0]
            assert "agy-a" in json.dumps(receiver.frames[0])
        finally:
            receiver.close()
            await r.stop()
    run(go())


@portable
def test_router_send_cli_carries_claude_session_binding(monkeypatch):
    import types
    requests = []
    monkeypatch.delenv("UNIMATRIX_PEER_NAME", raising=False)
    sibling = pr._sibling
    monkeypatch.setattr(pr, "_sibling", lambda filename, modname:
                        types.SimpleNamespace(own_target=lambda: "native-session")
                        if filename == "unimatrix-send.py" else sibling(filename, modname))
    monkeypatch.setattr(pr, "request_line", lambda endpoint, request:
                        (requests.append(request) or {"ok": True, "reply": "delivered"}))
    assert pr.cmd_send(["agy-a", "nonce"]) == 0
    assert requests[0]["as_target"] == "native-session"


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


def test_not_delivered_send_exits_nonzero_with_reason_and_delivered_exits_zero(box, capsys, monkeypatch):
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
            monkeypatch.setattr(m, "own_target", lambda: "")
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
    assert not any(line.startswith("agy-a\t") for line in lines)  # no leg: not reachable, so not listed
    rows = [line.split("\t") for line in lines if line.count("\t") >= 4]
    assert any([r[0], r[3], r[4]] == ["codex-a", "codex", "ready"] for r in rows)


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


def face_reply(endpoint, frame, sessions_dir=None):
    data = (json.dumps(frame) + "\n").encode()
    sdir = sessions_dir or (Path(endpoint).parent.parent / "sessions")
    if sdir.is_dir():
        data = pr._roster().authenticated_data(endpoint, data, sdir)
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(10)
    s.connect(endpoint)
    s.sendall(data)
    s.shutdown(socket.SHUT_WR)
    buf = b""
    while chunk := s.recv(65536):
        buf += chunk
    s.close()
    return buf


@pytest.mark.parametrize("replace_at_start", [False, True])
def test_dead_holder_row_gets_no_face_even_with_its_lock_held_and_is_not_listed(box, monkeypatch, replace_at_start):
    async def go():
        cx = box / "cx"
        peers = [{"name": "codex-dead", "vendor": "codex", "target": THR_HELD, "codexPid": _dead_pid()},
                 {"name": "codex-live", "vendor": "codex", "target": THR_OTHER, "codexPid": os.getpid()}]
        fd = hold(cx / f"{THR_HELD}.lock")
        r = holder_router(box, monkeypatch, peers, cx)
        if replace_at_start:
            registry = box / "peers.json"
            registry.write_text(json.dumps([
                {**peers[0], "codexPid": os.getpid()},
                {**peers[1], "codexPid": _dead_pid()},
            ]), encoding="utf-8")
            r.peers_file = registry
        await r.start()
        try:
            await r.refresh_discovered()
            live_name = "codex-dead" if replace_at_start else "codex-live"
            dead_name = "codex-live" if replace_at_start else "codex-dead"
            assert sorted(r.faces) == [live_name] and r.discovered == set()
            resp = await r._dispatch({"op": "list"})
            assert resp["peers"] == [live_name] and dead_name not in resp["ready"]
            assert [a["name"] for a in resp["agents"]] == [live_name] and "stale" not in resp["agents"][0]
            bad = await r._dispatch({"op": "send", "to": dead_name, "text": "hello there"})
            assert bad["ok"] is False
            good = await r._dispatch({"op": "send", "to": live_name, "text": "hello there"})
            assert good["ok"] is True
        finally:
            os.close(fd)
            await r.stop()
    run(go())


def test_holder_dying_after_start_stops_the_face_on_the_next_refresh(box, monkeypatch):
    import subprocess

    async def go():
        cx = box / "cx"
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        peers = [{"name": "codex-a", "vendor": "codex", "target": THR_HELD, "codexPid": holder.pid}]
        registry = box / "peers.json"
        registry.write_text(json.dumps(peers), encoding="utf-8")
        fd = hold(cx / f"{THR_HELD}.lock")
        r = holder_router(box, monkeypatch, peers, cx)
        r.peers_file = registry
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
            registry.write_text(json.dumps([{**peers[0], "codexPid": os.getpid()}]), encoding="utf-8")
            await r.refresh_discovered()
            assert r.endpoint_of("codex-a") == ep and os.path.exists(ep)
            assert "codex-a" in r.faces and "codex-a" not in r.dead_holders
            assert (await r._dispatch({"op": "send", "to": "codex-a", "text": "collaborate"}))["ok"]
        finally:
            holder.kill()
            os.close(fd)
            await r.stop()
    run(go())


def test_lock_only_codex_thread_is_listed_live_while_its_lock_is_held_and_accepts_sends(box, monkeypatch):
    async def go():
        cx = box / "cx"
        name = bind_codex(box / "settings", monkeypatch, THR_HELD)[THR_HELD]
        fd = hold(cx / f"{THR_HELD}.lock")
        r = holder_router(box, monkeypatch, [], cx)
        await r.start()
        try:
            await r.refresh_discovered()
            assert list(r.peers) == [name] and name in r.faces
            resp = await r._dispatch({"op": "list"})
            assert resp["agents"] == [{"name": name, "box": pr._contract().local_box(), "vendor": "codex",
                                       "this_box": True}]
            ok = await r._dispatch({"op": "send", "to": name, "text": "hello there"})
            assert ok["ok"] is True
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


@portable
@pytest.mark.parametrize("initial_live", [False, True])
def test_fixed_codex_holder_refresh_recovers_default_send(tmp_path, monkeypatch, initial_live):
    async def go():
        registry = tmp_path / "peers.json"
        peer = {"name": "codex-a", "vendor": "codex", "target": THR_HELD,
                "codexPid": os.getpid() if initial_live else 99999999,
                "codexProcStart": "old-start"}
        registry.write_text(json.dumps([peer]), encoding="utf-8")
        live_birth = ["old-start" if initial_live else "new-start"]
        monkeypatch.setattr(pr._liveness(), "codex_holder_live", lambda row:
                            None if "codexPid" not in row else row["codexPid"] == os.getpid()
                            and row.get("codexProcStart") == live_birth[0])
        r = pr.Router([dict(peer)], sessions_dir=tmp_path / "sessions", peers_file=registry)
        spawned, stopped, turns = [], [], []

        async def spawn(name):
            spawned.append(name)
            r.faces[name] = object()

        async def stop(name):
            stopped.append(name)
            r.faces.pop(name, None)

        async def turn(name, text, **_kw):
            turns.append((name, text))
            return "collaboration received"

        monkeypatch.setattr(r, "_spawn_face", spawn)
        monkeypatch.setattr(r, "_stop_face", stop)
        monkeypatch.setattr(r, "turn", turn)
        endpoint = r.endpoint_of("codex-a")
        r.faces["codex-a"] = object()
        await r.refresh_discovered()
        assert stopped == ([] if initial_live else ["codex-a"])
        assert r.peer_stale("codex-a") is (not initial_live)
        assert (await r._dispatch({"op": "send", "to": "codex-a", "text": "collaborate"}))["ok"] is initial_live

        fresh = {**peer, "codexPid": os.getpid(), "codexProcStart": "new-start"}
        live_birth[0] = "new-start"
        registry.write_text(json.dumps([fresh]), encoding="utf-8")
        await r.refresh_discovered()
        assert not r.peer_stale("codex-a") and "codex-a" not in r.dead_holders
        assert spawned == ["codex-a"]
        assert r.endpoint_of("codex-a") == endpoint
        assert r.discovered == set() and r.peers["codex-a"]["target"] == THR_HELD
        assert (await r._dispatch({"op": "send", "to": "codex-a", "text": "collaborate"}))["ok"]
        assert len(turns) == 1 + int(initial_live)
        await r.refresh_discovered()
        assert spawned == ["codex-a"]
        registry.write_text("{", encoding="utf-8")
        await r.refresh_discovered()
        assert not r.peer_stale("codex-a")

        registry.write_text("[]", encoding="utf-8")
        await r.refresh_discovered()
        assert r.peer_stale("codex-a")
        assert "codexPid" not in r.peers["codex-a"]
        assert not (await r._dispatch({"op": "send", "to": "codex-a", "text": "absent"}))["ok"]
        registry.write_text(json.dumps([fresh]), encoding="utf-8")
        await r.refresh_discovered()
        assert not r.peer_stale("codex-a")
        assert r.endpoint_of("codex-a") == endpoint
        assert spawned == ["codex-a", "codex-a"]
        assert registry.read_text(encoding="utf-8") == json.dumps([fresh])

        for row in ({**fresh, "target": THR_OTHER},
                    {k: v for k, v in fresh.items() if not k.startswith("codex")},
                    {**fresh, "codexPid": 99999999}):
            registry.write_text(json.dumps([row]), encoding="utf-8")
            await r.refresh_discovered()
            assert r.peer_stale("codex-a")
            assert not (await r._dispatch({"op": "send", "to": "codex-a", "text": "collaborate"}))["ok"]
        assert len(turns) == 1 + int(initial_live)
    run(go())


@portable
@pytest.mark.parametrize("custom", [False, True])
def test_serve_retains_its_peers_registry_for_holder_refresh(tmp_path, monkeypatch, custom):
    registry = tmp_path / "peers.json"
    registry.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(pr, "default_peers_file", lambda: registry)
    seen = []

    async def serve(router):
        seen.append(Path(router.peers_file))

    monkeypatch.setattr(pr, "serve", serve)
    assert pr.cmd_serve(["--peers", str(registry)] if custom else []) == 0
    assert seen == [registry]



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
    bind_codex(tmp_path / "settings", monkeypatch, THR_LIVE)
    write_rollout(tmp_path, THR_LIVE, "cli")
    conn =sqlite3.connect(tmp_path / "logs_1.sqlite")
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


@portable
@pytest.mark.parametrize("resp,shown", [
    ({"ok": True, "msg_id": "m1", "sender": "transport"}, "msg_id=m1"),
    ({"ok": True}, "delivered to coordinator-content-repo-50"),
])
def test_cmd_send_reply_without_reply_field_reports_delivery_and_version_mismatch(monkeypatch, capsys, resp, shown):
    monkeypatch.setattr(pr, "request_line", lambda *a, **k: resp)
    assert pr.cmd_send(["coordinator-content-repo-50", "hello"]) == 0
    out, err = capsys.readouterr()
    assert shown in out
    assert "router-version mismatch" in err and "restart the router" in err


@portable
def test_cmd_send_with_reply_field_prints_it_without_mismatch_notice(monkeypatch, capsys):
    monkeypatch.setattr(pr, "request_line", lambda *a, **k: {"ok": True, "reply": "pong"})
    assert pr.cmd_send(["peer", "hi"]) == 0
    out, err = capsys.readouterr()
    assert out.strip() == "pong" and "mismatch" not in err


@portable
def test_shutdown_op_replies_then_serve_returns_with_lease_free_and_files_gone():
    root = Path(tempfile.mkdtemp(prefix="pr-", dir="/tmp" if os.name != "nt" else None))
    for sub in ("sessions", "eps", "state"):
        (root / sub).mkdir()
    state = root / "state"
    try:
        async def go():
            r = pr.Router([], sessions_dir=root / "sessions", state_dir=state, endpoint_dir=root / "eps")
            served = asyncio.create_task(pr.serve(r))
            await until(lambda: (state / "router.pid.json").exists() and r.server is not None)
            rec = json.loads((state / "router.pid.json").read_text())
            assert rec == {"pid": os.getpid(), "procStart": pr._roster().proc_start(os.getpid()),
                           "codeDigest": pr.code_digest()}
            resp = await asyncio.to_thread(pr.request_line, r.ctl, {"op": "shutdown"})
            assert resp == {"ok": True, "pid": os.getpid(), "stopping": True}
            await asyncio.wait_for(served, 10)
            assert not (state / "router.pid.json").exists()
            probe = open(state / "router.lock", "a+b")
            try:
                assert pr._transport().lock_file(probe)
            finally:
                pr._transport().unlock_file(probe)
                probe.close()
            assert r.server is None and r.lease is None
            if os.name != "nt":
                assert list((root / "eps").iterdir()) == []
        run(go())
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_losing_the_lease_leaves_the_owners_pid_file(box):
    async def go():
        r = make_router(box, echo_adapters())
        await r.start()
        try:
            held = pr.Router([], sessions_dir=box / "sessions", state_dir=box / "state", endpoint_dir=box / "eps")
            with pytest.raises(RuntimeError):
                await held.start()
            assert (box / "state" / "router.pid.json").exists()  # the owner's record survives the loser's stop
            assert not r.stop_requested.is_set()
        finally:
            await r.stop()
        assert not (box / "state" / "router.pid.json").exists()
    run(go())


def _live_session(sessions, name, endpoint):
    (sessions / f"{name}.json").write_text(json.dumps(
        {"name": name, "pid": os.getpid(), "messagingSocketPath": endpoint, "sessionId": f"sid-{name}"}))


def _relay_router(tmp_path, *rows):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (tmp_path / "state").mkdir()
    for name, ep in rows:
        _live_session(sessions, name, ep)
    return pr.Router([], sessions_dir=sessions, state_dir=tmp_path / "state", endpoint_dir=tmp_path / "eps",
                     adapters={}), tmp_path / "state" / "unimatrix-peer-router.log"


@portable
def test_relay_target_resolves_the_named_session_not_another_live_one(tmp_path):
    r, log = _relay_router(tmp_path, ("example-game-workbench-repo-66", "/s/h66.sock"), ("claude-klabauter-a1", "/s/a1.sock"))
    out = r.resolve_relay_target("example-game-workbench-repo-66")
    assert out["ok"] and out["endpoint"] == "/s/h66.sock" and out["name"] == "example-game-workbench-repo-66"
    assert "RELAY-TARGET" in log.read_text()


@portable
def test_relay_target_unknown_name_is_refused_and_recorded_never_a_fallback(tmp_path):
    r, log = _relay_router(tmp_path, ("claude-klabauter-a1", "/s/a1.sock"))
    out = r.resolve_relay_target("example-game-workbench-repo-66")
    assert out["ok"] is False and out["recorded"] is True and "endpoint" not in out
    assert "RELAY-REFUSED example-game-workbench-repo-66" in log.read_text()
    assert r.resolve_relay_target("")["ok"] is False


@portable
def test_relay_target_ambiguous_name_is_refused(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (tmp_path / "state").mkdir()
    for n, ep in enumerate(("/s/x.sock", "/s/y.sock")):
        (sessions / f"{n}.json").write_text(json.dumps({"name": "dup", "pid": os.getpid(), "messagingSocketPath": ep}))
    r = pr.Router([], sessions_dir=sessions, state_dir=tmp_path / "state", endpoint_dir=tmp_path / "eps", adapters={})
    out = r.resolve_relay_target("dup")
    assert out["ok"] is False and "ambiguous" in out["error"]


@portable
def test_resolve_op_answers_through_dispatch(tmp_path):
    r, _ = _relay_router(tmp_path, ("example-game-workbench-repo-66", "/s/h66.sock"))
    assert run(r._dispatch({"op": "resolve", "name": "example-game-workbench-repo-66"}))["endpoint"] == "/s/h66.sock"
    assert run(r._dispatch({"op": "resolve", "name": "nope"}))["ok"] is False


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


def test_router_rebinds_its_control_socket_after_the_path_is_deleted(box):
    async def go():
        r = _inode_router(box)
        await r.start()
        try:
            os.unlink(r.ctl)
            assert not os.path.exists(r.ctl)
            await r.check_control_socket()
            assert os.path.exists(r.ctl)
            assert _answers_on(r.ctl)
            assert not r.stop_requested.is_set()
        finally:
            await r.stop()
    asyncio.run(go())


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_router_rebinds_when_the_path_holds_a_different_inode(box):
    async def go():
        r = _inode_router(box)
        await r.start()
        try:
            os.unlink(r.ctl)
            open(r.ctl, "w").close()  # a plain file now owns the path
            await r.check_control_socket()
            assert _answers_on(r.ctl)
            assert os.stat(r.ctl).st_ino == r.ctl_ino
        finally:
            await r.stop()
    asyncio.run(go())


@pytest.mark.spawns_process
@pytest.mark.cadence
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


@portable
def test_send_to_an_unbacked_mirror_row_is_refused_before_any_bridge_write(dirs, monkeypatch):
    (dirs / "sessions" / "em.json").write_text(json.dumps(
        {"name": "em", "pid": os.getpid(), "messagingSocketPath": str(dirs / "em.sock"), "startedAt": 1, **FAR_ROW}))
    r = pr.Router([], sessions_dir=dirs / "sessions", state_dir=dirs / "state", endpoint_dir=dirs / "eps", adapters={})
    sent = []

    async def capture(peer, endpoint, frame):
        sent.append(frame)
    monkeypatch.setattr(r, "_deliver_as_peer", capture)
    monkeypatch.setattr(r, "_transport_face", lambda source, destination: asyncio.sleep(0, "transport-x"))
    resp = run(r._dispatch({"op": "send", "as": "cli", "to": "em", "text": "hello"}))
    assert resp["ok"] is False and "em" in resp["error"] and not sent


def _two_null_target_rows_on_one_endpoint(tmp_path, sid_a, sid_b):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (tmp_path / "state").mkdir()
    for n, sid in enumerate((sid_a, sid_b)):
        (sessions / f"{n}.json").write_text(json.dumps(
            {"name": "dup", "pid": os.getpid(), "messagingSocketPath": "/s/one.sock", "sessionId": sid, "target": None}))
    return pr.Router([], sessions_dir=sessions, state_dir=tmp_path / "state", endpoint_dir=tmp_path / "eps", adapters={})


@portable
def test_two_null_target_rows_for_different_sessions_on_one_endpoint_are_ambiguous_never_resolved(tmp_path):
    r = _two_null_target_rows_on_one_endpoint(tmp_path, "sid-live", "sid-stale")
    assert r.resolve_sender("dup") == ""
    assert r.last_refusal.startswith("ambiguous")
    out = r.resolve_relay_target("dup")
    assert out["ok"] is False and "ambiguous" in out["error"] and "endpoint" not in out


@portable
def test_short_machine_a_alias_resolves_current_mirror_and_refuses_collision(dirs):
    row = {
        **FAR_ROW, "name": "agent-a.machine-a.local", "unimatrixFarName": "agent-a",
        "unimatrixRemote": "machine-a.local", "pid": os.getpid(),
        "messagingSocketPath": "/s/machine-a.sock", "sessionId": "first",
    }
    (dirs / "sessions" / "far.json").write_text(json.dumps(row))
    router = pr.Router([], sessions_dir=dirs / "sessions", state_dir=dirs / "state", endpoint_dir=dirs / "eps", adapters={})
    assert router.resolve_sender("agent-a.machine-a") == "/s/machine-a.sock"
    local = {"name": "agent-a.machine-a", "pid": os.getpid(), "messagingSocketPath": "/s/other.sock", "sessionId": "second"}
    (dirs / "sessions" / "local.json").write_text(json.dumps(local))
    assert router.resolve_sender("agent-a.machine-a") == ""
    assert "ambiguous" in router.last_refusal


@portable
def test_name_dot_box_id_resolves_a_mirror_labelled_by_address_and_stays_ambiguous_on_collision(dirs):
    row = {"name": "agent-a.192-168-0-192", "unimatrixBox": "Machine-a", "unimatrixRemote": "192.168.0.192",
           "pid": os.getpid(), "messagingSocketPath": "/s/machine-a.sock", "sessionId": "first"}
    (dirs / "sessions" / "far.json").write_text(json.dumps(row))
    router = pr.Router([], sessions_dir=dirs / "sessions", state_dir=dirs / "state", endpoint_dir=dirs / "eps", adapters={})
    assert router.resolve_sender("agent-a.machine-a") == "/s/machine-a.sock"
    assert router.resolve_sender("agent-a.192-168-0-192") == "/s/machine-a.sock"
    assert router.resolve_sender("agent-a.otherbox") == ""
    second = {**row, "name": "agent-a.192-168-0-192", "messagingSocketPath": "/s/other.sock", "sessionId": "second"}
    (dirs / "sessions" / "far2.json").write_text(json.dumps(second))
    assert router.resolve_sender("agent-a.machine-a") == ""
    assert "ambiguous" in router.last_refusal


@portable
def test_two_null_target_rows_for_one_session_on_one_endpoint_still_resolve(tmp_path):
    r = _two_null_target_rows_on_one_endpoint(tmp_path, "sid-live", "sid-live")
    assert r.resolve_sender("dup") == "/s/one.sock"


@portable
def test_owned_alias_colliding_with_another_sessions_exact_name_is_refused_never_resolved(tmp_path):
    tid = "11111111-2222-3333-4444-555555555555"
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (tmp_path / "state").mkdir()
    owned = {"name": "box-codex-abc-1", "pid": os.getpid(), "messagingSocketPath": "/s/codex.sock", "vendor": "codex",
             "target": tid, "sessionId": tid, "session_id": tid, "schema_version": 1, "provider": "codex",
             "address_version": 1, "box": "box", "aliases": ["shared"]}
    other = {"name": "shared", "pid": os.getpid(), "messagingSocketPath": "/s/other.sock", "sessionId": "sid-other"}
    for n, row in enumerate((owned, other)):
        (sessions / f"{n}.json").write_text(json.dumps(row))
    r = pr.Router([], sessions_dir=sessions, state_dir=tmp_path / "state", endpoint_dir=tmp_path / "eps", adapters={})
    assert "shared" in pr._contract().projected_addresses(owned)
    assert r.resolve_sender("shared") == ""
    assert r.last_refusal == "ambiguous: owned alias collides with another target"


class _LiveFace:
    def poll(self):
        return None


@portable
def test_relay_target_resolves_a_codex_peer_face_like_a_claude_session(tmp_path):
    # An inbound cross-box relay must reach a local Codex peer; resolve_sender skips router faces.
    r, log = _relay_router(tmp_path)
    r.peers["codex-x-1"] = {"name": "codex-x-1", "vendor": "codex", "target": "t1"}
    r.faces["codex-x-1"] = _LiveFace()
    r.peer_stale = lambda name: False
    out = r.resolve_relay_target("codex-x-1")
    assert out["ok"] and out["endpoint"] == r.endpoint_of("codex-x-1") and out["name"] == "codex-x-1"
    assert "RELAY-TARGET" in log.read_text()


@portable
def test_relay_target_refuses_a_stale_or_faceless_codex_peer(tmp_path):
    r, _ = _relay_router(tmp_path)
    r.peers["codex-x-1"] = {"name": "codex-x-1", "vendor": "codex", "target": "t1"}
    r.peer_stale = lambda name: True
    assert r.resolve_relay_target("codex-x-1")["ok"] is False
    r.peer_stale = lambda name: False
    assert r.resolve_relay_target("codex-x-1")["ok"] is False  # no live face process


@portable
def test_daemon_started_thread_metadata_is_a_sendable_root_and_children_still_are_not():
    # Observed state_5.sqlite row for a thread/start over the app-server daemon: source vscode, thread_source NULL, agent_path NULL.
    daemon_row = {"source": "vscode", "thread_source": None, "agent_path": None}
    assert pr.codex_is_sendable_root(daemon_row) is True
    assert pr.codex_is_sendable_root({**daemon_row, "agent_path": "/root/child"}) is False
    assert pr.codex_is_sendable_root({**daemon_row, "thread_source": "subagent"}) is False


@portable
def test_daemon_started_thread_with_a_held_lock_and_rollout_is_listed_as_a_codex_face(tmp_path, monkeypatch):
    from types import SimpleNamespace
    tid = "01a1fecd-d5e2-7c92-b750-72e20f6dc978"
    locks = tmp_path / "thread-writer-locks"
    locks.mkdir()
    (locks / f"{tid}.lock").write_text("held by the daemon")
    rollout = tmp_path / "sessions" / f"rollout-2026-11-21T15-51-05-{tid}.jsonl"
    rollout.parent.mkdir()
    rollout.write_text(json.dumps({"type": "session_meta", "payload": {"source": "vscode"}}) + "\n")
    row = {"id": tid, "cwd": str(tmp_path), "source": "vscode", "thread_source": None, "agent_path": None}
    monkeypatch.setattr(pr, "codex_thread_metadata", lambda home: {tid: row})
    monkeypatch.setattr(pr, "lock_held", lambda path: True)
    monkeypatch.setattr(pr, "_contract", lambda: SimpleNamespace(
        read_codex_address=lambda home, ident, box: {"name": "codex-daemon-face", "box": box},
        safe_alias=lambda a: True, local_box=lambda: "fixture-box", box_id=lambda b: b))
    monkeypatch.setattr(pr, "_roster", lambda: SimpleNamespace(delivery_settings_home=lambda: tmp_path))
    monkeypatch.setattr(pr, "_liveness", lambda: SimpleNamespace(codex_thread_holder=lambda home, ident: {}))
    got = pr.load_lock_peers(locks, "codex")
    assert [(p["name"], p["target"]) for p in got] == [("codex-daemon-face", tid)]


def _late_router(tmp_path, monkeypatch, peers=(), with_locks=False):
    """A router with a peers file and stubbed faces: adoption, staleness and delivery, no sockets."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "cx-home"))
    monkeypatch.setattr(pr, "codex_thread_metadata", lambda home: {THR_HELD: ROOT_ROW, THR_OTHER: ROOT_ROW})
    (tmp_path / "sessions").mkdir(exist_ok=True)
    r = pr.Router(peers, sessions_dir=tmp_path / "sessions", state_dir=tmp_path / "state",
                  endpoint_dir=tmp_path / "eps", adapters=echo_adapters(),
                  codex_locks_dir=(tmp_path / "cx" if with_locks else None), peers_file=tmp_path / "peers.json")

    async def spawn(name):
        r.faces[name] = object()

    async def stop(name):
        r.faces.pop(name)
    r._spawn_face, r._stop_face = spawn, stop
    return r


def _write_peers(tmp_path, *rows):
    path = tmp_path / "peers.json"
    path.write_text(json.dumps({"peers": list(rows)}), encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))  # a rewrite inside one mtime tick still changes the stamp
    return path


def _dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


@portable
def test_a_peers_row_written_after_start_is_adopted_and_delivered_to(tmp_path, monkeypatch):
    async def go():
        r = _late_router(tmp_path, monkeypatch)
        await r.refresh_discovered()
        assert r.peers == {}
        _write_peers(tmp_path, {"name": "codex-face", "vendor": "codex", "target": THR_HELD, "codexPid": os.getpid()})
        await r.refresh_discovered()
        assert "codex-face" in r.faces and "tui" not in r.peers["codex-face"]
        assert r.peers["codex-face"]["codexPid"] == os.getpid() and r.discovered == set()
        assert not r.peer_stale("codex-face")
        assert await r.turn("codex-face", "hello") == "codex saw: hello"
    run(go())


@portable
def test_a_lock_discovered_codex_peer_is_not_stale_while_its_lock_is_held(tmp_path, monkeypatch):
    async def go():
        r = _late_router(tmp_path, monkeypatch, with_locks=True)
        held = {"on": True}
        monkeypatch.setattr(pr, "lock_held", lambda path: held["on"])
        r._live_discovered = lambda: {"codex-lock": {"name": "codex-lock", "vendor": "codex", "target": THR_HELD}}
        await r.refresh_discovered()
        assert r.discovered == {"codex-lock"} and not r.peer_stale("codex-lock")
        assert await r.turn("codex-lock", "hi") == "codex saw: hi"
        held["on"] = False
        assert r.peer_stale("codex-lock")
        assert (await r.turn("codex-lock", "hi")).startswith("delivery failed")
    run(go())


@portable
def test_a_peers_row_whose_recorded_holder_is_dead_stays_stale_until_the_row_changes(tmp_path, monkeypatch):
    async def go():
        r = _late_router(tmp_path, monkeypatch)
        _write_peers(tmp_path, {"name": "codex-face", "vendor": "codex", "target": THR_HELD, "codexPid": _dead_pid()})
        await r.refresh_discovered()
        assert "codex-face" in r.dead_holders and "codex-face" not in r.faces and r.peer_stale("codex-face")
        assert (await r.turn("codex-face", "hi")).startswith("delivery failed")
        _write_peers(tmp_path, {"name": "codex-face", "vendor": "codex", "target": THR_HELD, "codexPid": os.getpid()})
        await r.refresh_discovered()
        assert "codex-face" not in r.dead_holders and "codex-face" in r.faces and not r.peer_stale("codex-face")
    run(go())


@portable
def test_an_adopted_row_replaces_the_lock_discovered_peer_of_its_thread_and_carries_no_tui(tmp_path, monkeypatch):
    async def go():
        r = _late_router(tmp_path, monkeypatch, with_locks=True)
        monkeypatch.setattr(pr, "lock_held", lambda path: True)
        r._live_discovered = lambda: {"codex-lock": {"name": "codex-lock", "vendor": "codex", "target": THR_HELD}}
        await r.refresh_discovered()
        assert r.peers["codex-lock"]["tui"] is True
        _write_peers(tmp_path, {"name": "codex-face", "vendor": "codex", "target": THR_HELD, "codexPid": os.getpid()})
        r._live_discovered = lambda: {}
        await r.refresh_discovered()
        assert list(r.peers) == ["codex-face"] and r.discovered == set() and list(r.faces) == ["codex-face"]
        assert "tui" not in r.peers["codex-face"] and not r.peer_stale("codex-face")
    run(go())


@portable
def test_an_unreadable_peers_file_adopts_nothing_and_keeps_existing_peers(tmp_path, monkeypatch):
    async def go():
        row = {"name": "codex-a", "vendor": "codex", "target": THR_HELD, "codexPid": os.getpid()}
        r = _late_router(tmp_path, monkeypatch, peers=[row])
        (tmp_path / "peers.json").write_text("{not json", encoding="utf-8")
        await r.refresh_discovered()
        assert list(r.peers) == ["codex-a"]
    run(go())


def test_shutdown_drains_an_in_flight_send_before_the_router_stops(box):
    release = asyncio.Event()

    async def slow_codex(peer, text):
        await release.wait()
        return "late reply"

    async def go():
        r = make_router(box, {"codex": slow_codex, "agy": echo_adapters()["agy"]})
        served = asyncio.create_task(pr.serve(r))
        await until(lambda: r.server is not None and (box / "state" / "router.pid.json").exists())
        send = asyncio.create_task(asyncio.to_thread(
            pr.request_line, r.ctl, {"op": "send", "as": "cli", "to": "codex-a", "text": "hello"}))
        await until(lambda: r.inflight > 0)
        stop = await asyncio.to_thread(pr.request_line, r.ctl, {"op": "shutdown"})
        assert stop["ok"] is True
        await asyncio.sleep(0.3)
        assert not served.done() and not send.done()
        release.set()
        resp = await asyncio.wait_for(send, 10)
        await asyncio.wait_for(served, 10)
        return resp
    resp = run(go())
    assert resp["ok"] is True and "late reply" in resp["reply"]


def test_restart_verb_refreshes_installed_copies_from_the_stamp_then_restarts(tmp_path, capsys):
    stamp, dest = tmp_path / "stamp", tmp_path / "dest"
    stamp.mkdir()
    (stamp / "unimatrix-peer-router.py").write_text("# stamp router bytes\n", encoding="utf-8")
    (stamp / "unimatrix-install.py").write_text(
        "import shutil\n"
        "def install_scripts(src, dest):\n"
        "    dest.mkdir(parents=True, exist_ok=True)\n"
        "    shutil.copy2(src / 'unimatrix-peer-router.py', dest / 'unimatrix-peer-router.py')\n"
        "    return ['unimatrix-peer-router.py']\n"
        "def restart_installed_router(dest, say):\n"
        "    assert (dest / 'unimatrix-peer-router.py').read_bytes() == (dest.parent / 'stamp' / 'unimatrix-peer-router.py').read_bytes()\n"
        "    return {'stopped': 'handshake', 'old_pid': 1, 'new_pid': 2}\n", encoding="utf-8")
    assert pr.main(["--restart", "--from", str(stamp), "--dest", str(dest)]) == 0
    assert "old pid 1 new pid 2" in capsys.readouterr().out
    assert (dest / "unimatrix-peer-router.py").read_bytes() == (stamp / "unimatrix-peer-router.py").read_bytes()


def test_restart_verb_without_an_installer_beside_it_refuses_and_names_from(tmp_path, capsys):
    assert pr.main(["--restart", "--from", str(tmp_path)]) == 2
    assert "--from" in capsys.readouterr().err


@portable
def test_serve_waits_for_a_pending_delivery_task_after_shutdown():
    root = Path(tempfile.mkdtemp(prefix="pr-", dir="/tmp" if os.name != "nt" else None))
    for sub in ("sessions", "eps", "state"):
        (root / sub).mkdir()
    try:
        async def go():
            r = pr.Router([], sessions_dir=root / "sessions", state_dir=root / "state", endpoint_dir=root / "eps")
            served = asyncio.create_task(pr.serve(r))
            await until(lambda: r.server is not None and (root / "state" / "router.pid.json").exists())
            release = asyncio.Event()
            t = asyncio.create_task(release.wait())
            r.tasks.add(t)
            t.add_done_callback(r.tasks.discard)
            await asyncio.to_thread(pr.request_line, r.ctl, {"op": "shutdown"})
            await asyncio.sleep(0.3)
            assert not served.done() and not t.done()
            release.set()
            await asyncio.wait_for(served, 10)
            assert t.done() and not t.cancelled()
        run(go())
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_code_digest_equals_the_installers_router_closure_digest():
    installer = pr._sibling("unimatrix-install.py", "unimatrix_install")
    assert pr.code_digest() == installer.router_closure_digest(pr.here())
