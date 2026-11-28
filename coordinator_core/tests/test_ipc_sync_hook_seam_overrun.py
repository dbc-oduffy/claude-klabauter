"""`dispatch_from_hook` inline sync seam: a suspension re-dispatches on the loop only before the handler started.

Once the handler has started, a suspension (an overrun of the inline drive) raises `HookDispatchError` and never
re-dispatches, because the handler may already have written.
"""
import asyncio

import pytest

from coordinator_core import ipc


@pytest.fixture
def seam(monkeypatch):
    """Route `dispatch_from_hook` through the inline seam with a scripted `dispatch_message`."""
    calls = {"dispatch": 0, "loop": 0}
    monkeypatch.setattr(ipc, "_sync_handler_registered", lambda op, env: True)
    monkeypatch.setattr(ipc, "record_op_process_time", lambda **kw: None)
    monkeypatch.setattr(ipc, "_spawn_count_or_none", lambda: None)
    monkeypatch.setattr(ipc, "_telemetry_sid", lambda: None)

    from coordinator_core import loop_pool

    real_run = loop_pool.run

    def counting_run(coro):
        calls["loop"] += 1
        return real_run(coro)

    monkeypatch.setattr(loop_pool, "run", counting_run)
    return calls


def _install(monkeypatch, calls, *, start_handler):
    async def fake_dispatch(envelope, *, caller=None, corr_id=None):
        calls["dispatch"] += 1
        if ipc._INLINE_SYNC_HANDLER.get():
            if start_handler:
                ipc._INLINE_HANDLER_STARTED.set(True)
            await asyncio.sleep(0)
        return {"jsonrpc": "2.0", "id": 1, "result": {"via": "loop"}}

    monkeypatch.setattr(ipc, "dispatch_message", fake_dispatch)


def test_a_suspension_before_the_handler_started_falls_back_to_the_loop(seam, monkeypatch):
    _install(monkeypatch, seam, start_handler=False)
    assert ipc.dispatch_from_hook("hooks.x", {}) == {"via": "loop"}
    assert seam == {"dispatch": 2, "loop": 1}


def test_an_overrun_after_the_handler_started_raises_and_never_redispatches(seam, monkeypatch):
    _install(monkeypatch, seam, start_handler=True)
    with pytest.raises(ipc.HookDispatchError) as raised:
        ipc.dispatch_from_hook("hooks.x", {})
    assert raised.value.code == ipc.INTERNAL_ERROR
    assert seam == {"dispatch": 1, "loop": 0}


def test_the_started_flag_does_not_leak_past_the_call(seam, monkeypatch):
    _install(monkeypatch, seam, start_handler=True)
    with pytest.raises(ipc.HookDispatchError):
        ipc.dispatch_from_hook("hooks.x", {})
    assert ipc._INLINE_HANDLER_STARTED.get() is False
    assert ipc._INLINE_SYNC_HANDLER.get() is False
