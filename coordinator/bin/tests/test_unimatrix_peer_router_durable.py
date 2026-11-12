"""Durable addressing pins: peers and senders are addressed by stable name, never pid/key/session id."""
import asyncio
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("peer_router_test_base", HERE / "test_unimatrix_peer_router.py")
base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(base)
pr = base.pr
box = base.box
FakeSender, frame_to, post_frame, until, run = base.FakeSender, base.frame_to, base.post_frame, base.until, base.run

pytestmark = [pytest.mark.complete, pytest.mark.skipif(os.name == "nt", reason="unix sockets")]


@pytest.fixture(autouse=True)
def _fixture_codex_threads_are_sendable_roots(monkeypatch):
    """The fixture peers' Codex threads have no state DB; the router drops codex rows without root proof."""
    monkeypatch.setattr(pr, "codex_is_sendable_root", lambda metadata: True)


THR_A = "01a1b600-0000-7000-8000-0000000000d1"
PEERS = [{"name": "codex-a", "vendor": "codex", "target": THR_A, "codexPid": os.getpid()},
         {"name": "agy-a", "vendor": "agy", "target": "conv-1"}]


def make_router(box, adapters):
    return base.make_router(box, adapters, PEERS)


def register_sender(box, name, endpoint, pid, started):
    """Write a roster entry for a fake Claude session called `name`."""
    (box / "sessions" / f"{pid}.json").write_text(json.dumps(
        {"pid": pid, "name": name, "messagingSocketPath": endpoint, "startedAt": started}))


def test_peer_restart_keeps_the_same_address(box):
    async def go():
        r = make_router(box, base.echo_adapters())
        s = FakeSender(str(box / "s.sock"))
        await r.start()
        try:
            old = r.faces["codex-a"].pid
            post_frame(r.endpoint_of("codex-a"), frame_to(box, s.path, "em", "one"))
            await until(lambda: len(s.frames) == 1)
            await r.restart_face("codex-a")
            new = r.faces["codex-a"].pid
            assert new != old
            assert not (box / "sessions" / f"{old}.json").exists()
            post_frame(r.endpoint_of("codex-a"), frame_to(box, s.path, "em", "two"))
            await until(lambda: len(s.frames) == 2)
            assert all("[reply] codex-a: " in c for c in s.contents())
            assert all(f["provenance"]["peer"] == "codex-a" and f["from_name"] == "codex-a" for f in s.frames)
            resp = await asyncio.to_thread(pr.request_line, r.ctl, {"op": "send", "to": "codex-a", "text": "three"})
            assert resp["reply"].startswith("[codex-a] ")
        finally:
            s.close()
            await r.stop()
    run(go())


def test_name_resolves_to_the_restarted_sender_but_a_local_reply_keeps_the_frame_endpoint(box):
    async def go():
        gate = asyncio.Event()

        async def codex(peer, text):
            await gate.wait()
            return "answer"

        r = make_router(box, {"codex": codex, "agy": codex})
        old_sock = FakeSender(str(box / "old.sock"))
        new_sock = FakeSender(str(box / "new.sock"))
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        await r.start()
        try:
            register_sender(box, "em-one", old_sock.path, 2 ** 22 + 1, 1)  # dead pid, older
            post_frame(r.endpoint_of("codex-a"), frame_to(box, old_sock.path, "em-one", "q"))
            await asyncio.sleep(0.3)
            register_sender(box, "em-one", new_sock.path, sleeper.pid, 2)  # restarted: live pid
            assert r.resolve_sender("em-one", old_sock.path) == new_sock.path  # the name still addresses the live session
            gate.set()
            # A local roster row never retargets a frame's own endpoint (fde11ea28b: it may be a far sender's name).
            await until(lambda: len(old_sock.frames) == 1)
            assert new_sock.frames == []
            assert "[reply] codex-a: answer" in old_sock.contents()[0]
        finally:
            sleeper.kill()
            sleeper.wait()
            old_sock.close()
            new_sock.close()
            await r.stop()
    run(go())


def test_unresolvable_sender_name_falls_back_to_frame_endpoint(box):
    async def go():
        r = make_router(box, base.echo_adapters())
        s = FakeSender(str(box / "s.sock"))
        await r.start()
        try:
            assert r.resolve_sender("ghost", s.path) == s.path
            post_frame(r.endpoint_of("agy-a"), frame_to(box, s.path, "ghost", "hi"))
            await until(lambda: len(s.frames) == 1)
        finally:
            s.close()
            await r.stop()
    run(go())


def test_peer_names_never_resolve_to_a_claude_sender(box):
    async def go():
        r = make_router(box, base.echo_adapters())
        await r.start()
        try:
            assert r.resolve_sender("codex-a", "") == ""  # a face entry is not a sender
        finally:
            await r.stop()
    run(go())


def test_no_session_id_pid_or_key_reaches_a_peer(box):
    async def go():
        seen = []

        async def codex(peer, text):
            seen.append(text)
            return "ok"

        r = make_router(box, {"codex": codex, "agy": codex})
        s = FakeSender(str(box / "s.sock"))
        await r.start()
        try:
            post_frame(r.endpoint_of("codex-a"), frame_to(box, s.path, "em-one", "hello"))
            await asyncio.to_thread(pr.request_line, r.ctl, {"op": "send", "as": "em-one", "to": "codex-a", "text": "hi"})
            await until(lambda: len(seen) == 2 and len(s.frames) == 1)
            tokens = {"peer-codex-a", "sessionId"}
            tokens |= {str(p.pid) for p in r.faces.values()}
            for text in seen:
                assert text.startswith("[em-one] ")
                assert not any(t in text for t in tokens)
        finally:
            s.close()
            await r.stop()
    run(go())
