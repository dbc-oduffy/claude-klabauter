"""Run a coroutine to completion on a reused, idle asyncio event loop instead of a fresh one.

TRAP: on Windows every new event loop builds its self-pipe with `socket.socketpair`, which there
is a loopback TCP listen/connect/accept. Under box load that accept parks for 10 s+ at ~0 CPU,
so a loop per request turned every hook into a multi-second wait: on 2026-11-19 05:30Z all guard
workers sat in `_fallback_socketpair -> accept` and the box's guards failed open. A loop that is not
running can be driven from any thread, so idle loops are pooled process-wide, not per thread: the
hook path's inner run happens on a fresh executor thread each request.

INVARIANT (from `asyncio.run`): each run gets its own copied context, its own default executor
(joined before the loop is returned, so no handler thread outlives its request), and leaves no
pending task or open async generator on the loop.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from typing import Any, Coroutine, TypeVar

T = TypeVar("T")

_idle: list[asyncio.AbstractEventLoop] = []
_idle_lock = threading.Lock()


def _take() -> asyncio.AbstractEventLoop:
    with _idle_lock:
        while _idle:
            loop = _idle.pop()
            if not loop.is_closed():
                return loop
    return asyncio.new_event_loop()


def _settle(loop: asyncio.AbstractEventLoop) -> None:
    """Cancel leftover tasks and close open async generators, as `asyncio.run` does on exit, without
    `shutdown_asyncgens()`: it latches the loop so later generators go untracked."""
    pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
    for task in pending:
        task.cancel()
    if pending:
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
    agens = list(getattr(loop, "_asyncgens", ()))
    if agens:
        getattr(loop, "_asyncgens").clear()
        loop.run_until_complete(asyncio.gather(*(ag.aclose() for ag in agens), return_exceptions=True))


def run(coro: Coroutine[Any, Any, T]) -> T:
    """`asyncio.run(coro)` on a pooled loop; the default executor is joined without a bound."""
    if asyncio._get_running_loop() is not None:
        coro.close()
        raise RuntimeError("loop_pool.run() cannot be called from a running event loop")
    loop = _take()
    executor = concurrent.futures.ThreadPoolExecutor(thread_name_prefix="asyncio")
    loop.set_default_executor(executor)
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        settled = False
        try:
            _settle(loop)
            settled = True
        finally:
            asyncio.set_event_loop(None)
            executor.shutdown(wait=True)
            if settled:
                with _idle_lock:
                    _idle.append(loop)
            else:
                loop.close()
