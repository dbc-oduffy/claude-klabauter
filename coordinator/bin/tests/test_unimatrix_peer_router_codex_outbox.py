"""A Codex peer's outbox keeps one unechoed item in the Codex queue and coalesces what is held behind it."""
import asyncio
import importlib.util
from pathlib import Path

BIN = Path(__file__).resolve().parents[1]


def _load(filename, name):
    spec = importlib.util.spec_from_file_location(name, BIN / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pr = _load("unimatrix-peer-router.py", "unimatrix_peer_router_outbox")
pc = _load("unimatrix-peer-codex.py", "unimatrix_peer_codex_outbox")
THR = "01a1b6aa-0000-7000-8000-0000000000bb"
NAME = "codex-a"


class FakeCodex:
    """A `codex queue` that starts one queued item per simulated turn; the peer's adapter waits for its item's turn."""

    def __init__(self):
        self.queue = []  # (text, started future)
        self.turns = []  # text of each Codex user turn, in order
        self.enqueues = 0
        self.strand_first = False  # report the first enqueue of an item as stranded (never started)

    async def adapter(self, peer, text):
        self.enqueues += 1
        if self.strand_first:
            self.strand_first = False
            if peer.get("on_queued"):
                peer["on_queued"]()
            return pc.STRANDED
        started = asyncio.get_running_loop().create_future()
        self.queue.append((text, started))
        if peer.get("on_queued"):
            peer["on_queued"]()
        await started
        peer["on_echoed"]()
        return f"reply to turn {len(self.turns)}"

    async def run_turn(self):
        text, started = self.queue.pop(0)
        self.turns.append(text)
        started.set_result(None)
        for _ in range(20):
            await asyncio.sleep(0)


def _router(adapter, tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "codex_thread_metadata", lambda home: {THR: {}})
    monkeypatch.setattr(pr, "codex_is_sendable_root", lambda row: True)
    r = pr.Router([{"name": NAME, "vendor": "codex", "target": THR, "codexPid": 1}],
                  sessions_dir=tmp_path, state_dir=tmp_path, endpoint_dir=tmp_path,
                  adapters={"codex": adapter, "agy": adapter})
    monkeypatch.setattr(r, "peer_stale", lambda name: False)
    return r


def _send(r, text, sender="g-em"):
    return r._dispatch({"op": "send", "to": NAME, "text": text, "as": sender})


def test_a_burst_of_ten_sends_surfaces_in_two_turns_in_order_none_lost(tmp_path, monkeypatch):
    async def go():
        fake = FakeCodex()
        r = _router(fake.adapter, tmp_path, monkeypatch)
        sends = [asyncio.create_task(_send(r, f"order-{i}", sender=f"s{i % 3}")) for i in range(10)]
        for _ in range(50):
            await asyncio.sleep(0)
        held = [await t for t in sends[1:]]
        assert all(h["ok"] and h["held"] and "reply" not in h for h in held)
        assert held[-1]["pending"] == 10 and held[-1]["oldest_age_s"] >= 0
        assert len(fake.queue) == 1  # the Codex queue never holds more than one of ours
        listing = await r._dispatch({"op": "list"})
        assert listing["codex_pending"][NAME]["pending"] == 10
        await fake.run_turn()
        for _ in range(50):
            await asyncio.sleep(0)
        assert len(fake.queue) == 1  # the coalesced item
        await fake.run_turn()
        assert len(fake.turns) == 2
        assert (await sends[0])["ok"]
        body = fake.turns[1]
        positions = [body.index(f"order-{i}") for i in range(1, 10)]
        assert positions == sorted(positions) and "order-0" in fake.turns[0] and "order-0" not in body
        assert "from s1" in body and "9 messages" in body
        assert fake.enqueues == 2
        assert (await r._dispatch({"op": "list"}))["codex_pending"][NAME]["pending"] == 0

    asyncio.run(go())


def test_log_carries_one_inbound_and_one_injected_line_per_message(tmp_path, monkeypatch):
    async def go():
        fake = FakeCodex()
        r = _router(fake.adapter, tmp_path, monkeypatch)
        sends = [asyncio.create_task(_send(r, f"m{i}")) for i in range(3)]
        for _ in range(30):
            await asyncio.sleep(0)
        await fake.run_turn()
        for _ in range(30):
            await asyncio.sleep(0)
        await fake.run_turn()
        await asyncio.gather(*sends)
        lines = (tmp_path / "unimatrix-peer-router.log").read_text(encoding="utf-8").splitlines()
        inbound = [l for l in lines if " INBOUND " in l]
        injected = [l for l in lines if " INJECTED " in l]
        assert len(inbound) == 3 and len(injected) == 3
        assert all("from=g-em" in l and "received=" in l for l in inbound)
        assert all("echo=" in l and "latency_s=" in l for l in injected)

    asyncio.run(go())


def test_a_stranded_item_is_enqueued_again_once_and_surfaces_once(tmp_path, monkeypatch):
    async def go():
        fake = FakeCodex()
        fake.strand_first = True
        r = _router(fake.adapter, tmp_path, monkeypatch)
        send = asyncio.create_task(_send(r, "stranded-order"))
        for _ in range(30):
            await asyncio.sleep(0)
        await fake.run_turn()
        assert (await send)["ok"]
        assert fake.enqueues == 2 and len(fake.turns) == 1 and "stranded-order" in fake.turns[0]

    asyncio.run(go())


def test_a_second_strand_is_reported_failed_and_never_silently_dropped(tmp_path, monkeypatch):
    async def go():
        async def always_stranded(peer, text):
            if peer.get("on_queued"):
                peer["on_queued"]()
            return pc.STRANDED

        r = _router(always_stranded, tmp_path, monkeypatch)
        out = await _send(r, "lost-order")
        assert not out["ok"] and "stranded" in out["error"]
        failed = (await r._dispatch({"op": "list"}))["codex_pending"][NAME]["failed"]
        assert len(failed) == 1
        assert "STRAND-FAILED" in (tmp_path / "unimatrix-peer-router.log").read_text(encoding="utf-8")

    asyncio.run(go())


def test_an_adapter_that_never_observes_the_echo_clears_inflight_and_flushes_held(tmp_path, monkeypatch):
    async def go():
        calls, gate = [], asyncio.Event()

        async def socket_turn(peer, text):  # the queue fallback: no echo observation
            calls.append(text)
            if peer.get("on_queued"):
                peer["on_queued"]()
            if len(calls) == 1:
                await gate.wait()
            return "ok"

        r = _router(socket_turn, tmp_path, monkeypatch)
        first = asyncio.create_task(_send(r, "one"))
        for _ in range(20):
            await asyncio.sleep(0)
        assert (await _send(r, "two"))["held"]
        gate.set()
        assert (await first)["ok"]
        await asyncio.gather(*r.tasks)
        assert len(calls) == 2 and "two" in calls[1]
        assert (await r._dispatch({"op": "list"}))["codex_pending"][NAME]["pending"] == 0

    asyncio.run(go())


def test_the_router_and_adapter_agree_on_the_stranded_marker():
    assert pr.STRANDED_REPLY == pc.STRANDED
