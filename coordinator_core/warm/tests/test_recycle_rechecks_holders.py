"""A saturation sample can go stale before the pool swap lock is acquired."""

from concurrent.futures import Future
from threading import Lock
from time import monotonic
from types import SimpleNamespace

from coordinator_core.warm import server


def test_recycle_spares_fresh_work_after_a_stalled_future_finishes(monkeypatch):
    pool = object()
    stale, fresh = Future(), Future()
    retired = []
    ctx = SimpleNamespace(
        _dispatch_pool=pool,
        _dispatch_pool_lock=Lock(),
        _pool_tasks={stale: ("stale", 0, None, None), fresh: ("fresh", monotonic(), None, None)},
        _admission_gate=lambda: SimpleNamespace(note_recycle=lambda: None),
        _write_recycle_line=lambda *_: None,
    )
    monkeypatch.setattr(server, "_dispatch_pool_size", lambda: 1)
    monkeypatch.setattr(server.pool_retire, "retire_pool", lambda pool, **_: retired.append(pool))
    stale.set_result(None)

    server._ServerContext._recycle_stalled_pool(ctx, pool, ["stale"])

    assert ctx._dispatch_pool is pool
    assert not retired
    assert fresh in ctx._pool_tasks
