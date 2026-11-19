"""`loop_pool.run` is `asyncio.run` minus the per-call event loop: a Windows loop's self-pipe is a
loopback-TCP socketpair whose accept stalls under load, so 100 runs build one loop, not 100."""

from __future__ import annotations

import asyncio
import contextvars
import threading

import pytest

from coordinator_core import loop_pool


@pytest.fixture(autouse=True)
def _fresh_pool(monkeypatch):
    monkeypatch.setattr(loop_pool, "_idle", [])


def test_a_hundred_runs_across_threads_build_one_loop(monkeypatch):
    built = []
    real = asyncio.new_event_loop
    monkeypatch.setattr(loop_pool.asyncio, "new_event_loop", lambda: built.append(1) or real())

    async def one(i):
        await asyncio.sleep(0)
        return i

    out = []
    for i in range(50):
        out.append(loop_pool.run(one(i)))
    t = threading.Thread(target=lambda: [out.append(loop_pool.run(one(i))) for i in range(50, 100)])
    t.start()
    t.join()
    assert out == list(range(100))
    assert len(built) == 1


def test_a_left_running_task_is_cancelled_not_carried_into_the_next_run():
    seen = []

    async def leaky():
        async def forever():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                seen.append("cancelled")
                raise

        asyncio.get_running_loop().create_task(forever())
        await asyncio.sleep(0)

    loop_pool.run(leaky())
    assert seen == ["cancelled"]
    loop = loop_pool._idle[-1]
    assert not [t for t in asyncio.all_tasks(loop) if not t.done()]


def test_the_runs_executor_threads_are_joined_before_it_returns():
    done = threading.Event()

    async def offload():
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, lambda: (threading.Event().wait(0.2), done.set()))

    loop_pool.run(offload())
    assert done.is_set()


def test_context_changes_inside_a_run_do_not_leak_out():
    var = contextvars.ContextVar("v", default="outer")

    async def setit():
        var.set("inner")
        return var.get()

    assert loop_pool.run(setit()) == "inner"
    assert var.get() == "outer"


def test_an_exception_propagates_and_the_loop_stays_reusable():
    async def boom():
        raise ValueError("x")

    with pytest.raises(ValueError):
        loop_pool.run(boom())

    async def ok():
        return 7

    assert loop_pool.run(ok()) == 7
    assert len(loop_pool._idle) == 1


def test_refuses_inside_a_running_loop_like_asyncio_run():
    async def nested():
        async def inner():
            return 1

        with pytest.raises(RuntimeError):
            loop_pool.run(inner())

    asyncio.run(nested())
