"""A mutating op parked past its per-dispatch deadline gets a typed timeout for its own request
only: its pool is swapped out (not retired) while a neighbour is unresolved, the neighbour returns
its result, a later dispatch reaches a fresh pool, and no pool-recycle line is written. Drives
`_pool_dispatch_untimed` with thread pools standing in for the worker processes."""

from __future__ import annotations

import concurrent.futures
import threading
import types

from coordinator_core.warm import server, telemetry

_MUTATING = "dispatch.ask_stage"
_EVENTS = {"stuck": threading.Event(), "neighbour": threading.Event()}


class _VersionState:
    server_sha = "deadbeef"

    def is_skewed(self, client_token: str) -> bool:
        return False

    def is_token_skewed(self, client_token: str) -> bool:
        return False


class _TrackedPool(concurrent.futures.ThreadPoolExecutor):
    """Exposes the pending-work table `_pool_neighbour_futures` reads on a process pool."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._pending_work_items: dict = {}

    def submit(self, fn, /, *args, **kwargs):
        fut = super().submit(fn, *args, **kwargs)
        self._pending_work_items[id(fut)] = types.SimpleNamespace(future=fut)
        return fut


def _body(msg, caller):
    park = (msg.get("params") or {}).get("park")
    if park:
        _EVENTS[park].wait(30)
    return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"ok": park or "plain"}}


def _msg(i, park=None):
    return {"jsonrpc": "2.0", "id": i, "method": _MUTATING, "params": {"park": park}}


def _rig(tmp_path, monkeypatch):
    for ev in _EVENTS.values():
        ev.clear()
    monkeypatch.setattr(server, "_pool_dispatch_worker_body", _body)
    monkeypatch.setattr(telemetry, "flush_requests", lambda *_a, **_k: None)
    monkeypatch.setattr(
        server,
        "_withdrawal_deadline_secs",
        lambda _method, msg: 0.6 if (msg.get("params") or {}).get("park") == "stuck" else 20.0,
    )
    retired: list = []

    def _retire(pool, grace_secs=0.0, ledger=None):
        retired.append(pool)
        pool.shutdown(wait=False, cancel_futures=True)

    monkeypatch.setattr(server.pool_retire, "retire_pool", _retire)
    ctx = server._ServerContext(
        name="pipe-deadline", sid="sid-deadline", version_state=_VersionState(), engine_root=tmp_path
    )
    ctx._claim_dir = tmp_path / "claims"
    ctx._claim_dir.mkdir()
    ctx._stall_dir = tmp_path / "stall"
    old = _TrackedPool(max_workers=server.DISPATCH_PROCESS_POOL_SIZE + server._LIGHT_RESERVE_WORKERS)
    ctx._dispatch_pool = old
    fresh: list = []

    def _ensure():
        if ctx._dispatch_pool is None:
            pool = _TrackedPool(max_workers=2)
            fresh.append(pool)
            ctx._dispatch_pool = pool
        return ctx._dispatch_pool

    ctx._ensure_dispatch_pool = _ensure
    return ctx, old, fresh, retired


def _spawn(ctx, msg, results):
    thread = threading.Thread(target=lambda: results.append(ctx._pool_dispatch_untimed(msg)), daemon=True)
    thread.start()
    return thread


def test_a_parked_op_times_out_alone_while_its_neighbour_and_a_later_dispatch_are_served(tmp_path, monkeypatch):
    ctx, old, fresh, retired = _rig(tmp_path, monkeypatch)
    stuck_out: list = []
    neighbour_out: list = []
    try:
        neighbour = _spawn(ctx, _msg(2, "neighbour"), neighbour_out)
        stuck = _spawn(ctx, _msg(1, "stuck"), stuck_out)
        stuck.join(10)

        assert len(stuck_out) == 1 and "error" in stuck_out[0], stuck_out
        assert stuck_out[0].get("id") == 1
        assert neighbour_out == []
        assert ctx._dispatch_pool is None or ctx._dispatch_pool is not old
        assert retired == []

        out = ctx._pool_dispatch_untimed(_msg(3))
        assert out.get("result") == {"ok": "plain"}, out
        assert ctx._dispatch_pool is fresh[0]

        _EVENTS["neighbour"].set()
        neighbour.join(10)
        assert neighbour_out and neighbour_out[0].get("result") == {"ok": "neighbour"}, neighbour_out
        assert old in retired
        assert not list((tmp_path / "stall").glob("*-recycle.txt"))
    finally:
        for ev in _EVENTS.values():
            ev.set()
        for pool in [old, *fresh]:
            pool.shutdown(wait=False, cancel_futures=True)
