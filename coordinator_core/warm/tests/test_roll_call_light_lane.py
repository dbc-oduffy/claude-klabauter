"""groupem.roll_call takes the reserved light slot: it runs while a single heavy worker is held."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from coordinator_core.warm import pool_sizing, server
from coordinator_core.warm.tests import test_hook_lane_full_pool as rig
from coordinator_core.warm.tests.test_hook_lane_full_pool import headroom  # noqa: F401

_HEAVY = "dispatch.land_run"
_ROLL_CALL = "groupem.roll_call"


def test_roll_call_ops_are_light_and_the_forwarding_reply_is_not() -> None:
    assert server._is_light_mutating(_ROLL_CALL)
    assert server._is_light_mutating("groupem.roll_call_status")
    assert not server._is_light_mutating("groupem.roll_call_reply")
    assert not server._is_light_mutating(_HEAVY)


def test_roll_call_runs_on_a_light_slot_with_pool_size_one_and_the_worker_held(
    headroom, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()
    headroom(rig._headroom(pool_sizing.RAM_RESERVE_MB // 2))
    ctx, heavy = rig._ctx(tmp_path, monkeypatch, release, hook_io_blocked=False)
    assert heavy == 1 and server._dispatch_pool_size() == 1

    def _body(msg, caller):
        if msg["method"] == _HEAVY:
            release.wait(rig._HOLD_SECS)
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"ok": msg["method"]}}

    monkeypatch.setattr(server, "_pool_dispatch_worker_body", _body)
    holder = rig._FakeIO(_HEAVY, 1)
    threads = [rig._serve_async(ctx, holder)]
    try:
        assert rig._wait(lambda: ctx._admission_gate()._heavy_held >= 1, 5.0), "heavy slot never held"
        queued = rig._FakeIO(_HEAVY, 2)
        threads.append(rig._serve_async(ctx, queued))
        for i in range(4):
            io = rig._FakeIO(_ROLL_CALL, 7000 + i)
            started, cpu0 = time.monotonic(), time.process_time()
            ctx._serve_queued(io, (time.time(), 0), lane="guard")
            assert time.process_time() - cpu0 < rig._BAR_SECS
            assert time.monotonic() - started < server._HOOK_LANE_BUDGET_SECS
            reply = json.loads(io.written[0])
            assert reply.get("result", {}).get("ok") == _ROLL_CALL, reply
        assert not holder.written and not queued.written
    finally:
        rig._shutdown(ctx, release, threads)
