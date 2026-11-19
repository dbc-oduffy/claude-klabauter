"""A pool worker that dies mid `invoke.from_argv` is replaced and prewarmed off the request path, so the
frames queued behind it are answered by booted workers, not stranded behind a cold worker start.

Real `ProcessPoolExecutor`s (spawn) with a stand-in worker body and a 1s worker initializer that models
the op-registry preload. Budgets are measured around `_pool_dispatch`, the server's own handling time.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from coordinator_core.warm import server

_INIT_SECS = 1.0
_ANSWER_BUDGET_SECS = 0.5
_HEAL_WAIT_SECS = 60.0


def _light_init(stderr_dir=None, stall_dir=None, forward_queue=None) -> None:
    time.sleep(_INIT_SECS)


def _fake_worker(msg, caller, dispatch_key=None, claim_dir=None, stall_secs=0.0):
    params = msg.get("params") or {}
    marker = params.get("die_marker")
    if marker and not os.path.exists(marker):
        Path(marker).write_text("died", encoding="ascii")
        os._exit(1)
    return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"pid": os.getpid()}}


def _frame(argv, i, entrypoint="hook-run", **extra):
    return {
        "jsonrpc": "2.0",
        "id": i,
        "method": "invoke.from_argv",
        "params": {"argv": argv, "entrypoint": entrypoint, **extra},
    }


def _ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_pool_dispatch_worker", _fake_worker)
    monkeypatch.setattr(server, "_worker_process_init", _light_init)
    monkeypatch.setattr(server.breadcrumb, "svc_dir", lambda _root: tmp_path / "svc")
    ctx = server._ServerContext.__new__(server._ServerContext)
    ctx.engine_root = tmp_path
    ctx._dispatch_pool = None
    ctx._guard_pool = None
    ctx._dispatch_pool_lock = threading.Lock()
    ctx._guard_pool_lock = threading.Lock()
    ctx._pool_outstanding = server.InFlightCounter()
    return ctx


def _live(pool) -> int:
    return sum(1 for p in (getattr(pool, "_processes", None) or {}).values() if p.is_alive())


def _shutdown(ctx):
    for pool in (ctx._dispatch_pool, ctx._guard_pool):
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    context = _ctx(tmp_path, monkeypatch)
    yield context
    _shutdown(context)


def test_lost_worker_is_replaced_and_prewarmed_without_another_request(ctx, tmp_path):
    out = ctx._pool_dispatch(_frame(["hooks.preuse_write_dispatch"], 1))
    assert out.get("result") or out.get("error"), out
    marker = tmp_path / "died"
    lost = ctx._guard_pool
    ctx._pool_dispatch(_frame(["hooks.preuse_write_dispatch"], 2, die_marker=str(marker)))
    assert marker.exists(), "the worker never died; the test measured nothing"

    heal = ctx.__dict__["_pool_healing"][True]
    assert heal.wait(_HEAL_WAIT_SECS), "no replacement pool was built after the worker loss"
    assert ctx._guard_pool is not None and ctx._guard_pool is not lost
    assert _live(ctx._guard_pool) >= server._HEAL_PREWARM_WORKERS


@pytest.mark.deliberate_wall_clock(reason="stall bound: a frame after worker loss must not wait on a cold worker start")
def test_frame_after_the_loss_is_answered_by_a_booted_worker(ctx, tmp_path):
    ctx._pool_dispatch(_frame(["hooks.preuse_write_dispatch"], 1, die_marker=str(tmp_path / "died")))
    assert ctx.__dict__["_pool_healing"][True].wait(_HEAL_WAIT_SECS)

    t0 = time.monotonic()
    out = ctx._pool_dispatch(_frame(["hooks.preuse_write_dispatch"], 3))
    elapsed = time.monotonic() - t0

    assert out.get("result", {}).get("pid"), out
    assert elapsed < _ANSWER_BUDGET_SECS, f"{elapsed:.2f}s: the frame met a cold worker start"


def test_compute_only_op_retries_on_the_replacement_not_in_process(ctx, tmp_path, monkeypatch):
    monkeypatch.setattr(
        server, "_run_dispatch", lambda *a, **k: pytest.fail("ran in the accept process after a worker loss")
    )
    out = ctx._pool_dispatch(
        _frame(["ping"], 1, entrypoint="coordinator-invoke", die_marker=str(tmp_path / "died"))
    )
    assert out.get("result", {}).get("pid") not in (None, os.getpid()), out


def test_mutating_op_whose_worker_died_is_not_rerun(ctx, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_run_dispatch", lambda *a, **k: pytest.fail("re-ran a mutating op"))
    marker = tmp_path / "died"
    out = ctx._pool_dispatch(
        _frame(["ceremony.scoped_git_commit"], 1, entrypoint="coordinator-invoke", die_marker=str(marker))
    )
    assert out["error"]["code"] == server.WARM_DISPATCH_INDETERMINATE
    assert marker.exists()


def test_concurrent_losses_share_one_heal(ctx):
    first = ctx._heal_lost_pool(server.concurrent.futures.ProcessPoolExecutor(max_workers=1), True)
    second = ctx._heal_lost_pool(server.concurrent.futures.ProcessPoolExecutor(max_workers=1), True)
    assert first is second
    assert first.wait(_HEAL_WAIT_SECS)


def test_a_stand_in_pool_is_not_healed(ctx):
    assert ctx._heal_lost_pool(object(), True) is None
