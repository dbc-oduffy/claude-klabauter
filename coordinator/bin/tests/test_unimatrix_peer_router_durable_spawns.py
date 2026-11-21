"""Durable addressing pins: peers and senders are addressed by stable name, never pid/key/session id."""
import asyncio
import importlib.util
import os
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("peer_router_test_base", HERE / "test_unimatrix_peer_router.py")
base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(base)
pr = base.pr
box = base.box
FakeSender, frame_to, post_frame, until, run = base.FakeSender, base.frame_to, base.post_frame, base.until, base.run

pytestmark = [pytest.mark.complete, pytest.mark.skipif(os.name == "nt", reason="unix sockets"), pytest.mark.spawns_process, pytest.mark.cadence]


@pytest.fixture(autouse=True)
def _fixture_codex_threads_are_sendable_roots(monkeypatch):
    """The fixture peers' Codex threads have no state DB; the router drops codex rows without root proof."""
    monkeypatch.setattr(pr, "codex_is_sendable_root", lambda metadata: True)


THR_A = "01a1b600-0000-7000-8000-0000000000d1"
PEERS = [{"name": "codex-a", "vendor": "codex", "target": THR_A, "codexPid": os.getpid()},
         {"name": "agy-a", "vendor": "agy", "target": "conv-1"}]


def make_router(box, adapters):
    return base.make_router(box, adapters, PEERS)


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
