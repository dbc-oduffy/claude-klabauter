"""A queued Codex message must not wait behind an earlier turn's reply (2292 relaunched runs on stale stop orders)."""
import asyncio
import importlib.util
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]


def _load(filename, name):
    spec = importlib.util.spec_from_file_location(name, BIN / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pr = _load("unimatrix-peer-router.py", "unimatrix_peer_router_qr")
pc = _load("unimatrix-peer-codex.py", "unimatrix_peer_codex_qr")
THR = "01a1b6aa-0000-7000-8000-0000000000aa"


def _router(adapter, tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "codex_thread_metadata", lambda home: {THR: {}})
    monkeypatch.setattr(pr, "codex_is_sendable_root", lambda row: True)
    r = pr.Router([{"name": "codex-a", "vendor": "codex", "target": THR, "codexPid": 1}],
                  sessions_dir=tmp_path, state_dir=tmp_path, endpoint_dir=tmp_path,
                  adapters={"codex": adapter, "agy": adapter})
    monkeypatch.setattr(r, "peer_stale", lambda name: False)
    return r


def test_second_message_is_held_without_waiting_on_the_first_reply_then_coalesced_on_echo(tmp_path, monkeypatch):
    async def go():
        enqueued, gate = [], asyncio.Event()

        async def codex(peer, text):
            enqueued.append(text)
            peer["on_queued"]()
            await gate.wait()
            peer["on_echoed"]()
            return f"reply {len(enqueued)}"

        r = _router(codex, tmp_path, monkeypatch)
        first = asyncio.create_task(r.turn("codex-a", pr.wrap_inbound("a", "one")))
        second = asyncio.create_task(r.turn("codex-a", pr.wrap_inbound("b", "two")))
        assert await asyncio.wait_for(second, 1) == pr.HELD_REPLY  # the first reply is still pending
        assert len(enqueued) == 1 and "one" in enqueued[0]
        gate.set()
        assert await first == "reply 1"
        await asyncio.gather(*r.tasks)
        assert len(enqueued) == 2 and "from b" in enqueued[1] and "two" in enqueued[1]
        assert not r.locks["codex-a"].locked() and "codex-a" not in r.addressed

    asyncio.run(go())


def test_a_non_queueing_turn_still_serializes_on_the_lock(tmp_path, monkeypatch):
    async def go():
        active, peak = 0, 0

        async def codex(peer, text):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
            return "ok"

        r = _router(codex, tmp_path, monkeypatch)
        await asyncio.gather(r.turn("codex-a", "a"), r.turn("codex-a", "b"))
        assert peak == 1

    asyncio.run(go())


def test_queue_turn_signals_on_queued_after_the_enqueue_before_the_reply_wait(monkeypatch):
    events = []

    class Done:
        returncode, stderr = 0, ""

    monkeypatch.setattr(pc, "codex_binary", lambda: "codex")
    monkeypatch.setattr(pc, "RolloutTail", lambda thread: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(pc.subprocess, "run", lambda *a, **k: events.append("enqueue") or Done())
    reply = asyncio.run(pc.queue_turn(THR, "m", on_queued=lambda: events.append("released")))
    assert events == ["enqueue", "released"] and reply == pc.NO_REPLY


def test_a_failed_enqueue_never_signals_on_queued(monkeypatch):
    class Bad:
        returncode, stderr = 1, "no app-server"

    monkeypatch.setattr(pc, "codex_binary", lambda: "codex")
    monkeypatch.setattr(pc, "RolloutTail", lambda thread: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(pc.subprocess, "run", lambda *a, **k: Bad())
    called = []
    with pytest.raises(pc.QueueFailed):
        asyncio.run(pc.queue_turn(THR, "m", on_queued=lambda: called.append(1)))
    assert not called
