"""A SendMessage PreToolUse hook op never parks on the guard pool result wait after every guard
worker went overdue, even when a worker booted cold costs more than the hook lane budget.

Drives the real `_pool_dispatch` (default arguments) with thread-backed stand-ins for the guard
processes (`ProcessPoolExecutor` subclasses, so the production `isinstance` gate on the standby
holds). A pool built after the wedge charges a boot delay on every task until its own boot
completes, the cost a cold `_worker_process_init` plus imports pays on a loaded box (a no-op
`ping` took 4-8 s in the 2026-11-25 11:28Z episode). The budget the delay exceeds is the repo's own
`_HOOK_LANE_BUDGET_SECS`, scaled down for the test, never changed in production. Mechanism:
docs/research/warm-pool-dispatch-result-wait/2026-11-25-root-cause/synthesis.md.
"""

from __future__ import annotations

import concurrent.futures
import threading
import time

from coordinator_core.warm import server, telemetry

_HOOK = "hooks.preuse_write_dispatch"
_SENDMESSAGE_OP = "hooks.rewrite_sendmessage_peer_name"
_BUDGET = 0.6
_BOOT = 1.5
_FAST = 1.2  # below _BOOT so a parked hook still fails; well above _BUDGET so scheduler jitter does not
_RELEASE = threading.Event()


class _VersionState:
    server_sha = "deadbeef"

    def is_skewed(self, client_token: str) -> bool:
        return False

    def is_token_skewed(self, client_token: str) -> bool:
        return False


class _ThreadBackedPool(concurrent.futures.ProcessPoolExecutor):
    """A guard pool whose tasks run on threads and cannot start until `boot` seconds after the
    pool was built, as workers of a cold process pool cannot."""

    def __init__(self, boot: float, workers: int):
        super().__init__(max_workers=1)
        self._threads = concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="guard")
        self.ready_at = time.monotonic() + boot

    def submit(self, fn, /, *args, **kwargs):
        ready_at = self.ready_at

        def _boot_then_run(*a, **k):
            wait = ready_at - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            return fn(*a, **k)

        return self._threads.submit(_boot_then_run, *args, **kwargs)

    def shutdown(self, wait=True, *, cancel_futures=False):
        self._threads.shutdown(wait=False, cancel_futures=cancel_futures)
        super().shutdown(wait=False, cancel_futures=cancel_futures)


def _body(msg, caller):
    if (msg.get("params") or {}).get("wedge"):
        _RELEASE.wait(30)
    return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"ok": msg["method"]}}


def _wedge_msg(i):
    return {"jsonrpc": "2.0", "id": i, "method": _HOOK, "params": {"wedge": True}}


def _sendmessage_frame():
    """The door frame the SendMessage PreToolUse hook sends: `invoke.from_argv` for `hook-run`."""
    return {
        "jsonrpc": "2.0",
        "id": 900,
        "method": "invoke.from_argv",
        "params": {"entrypoint": "hook-run", "argv": ["hook-run", _SENDMESSAGE_OP]},
    }


def _ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_pool_dispatch_worker_body", _body)
    monkeypatch.setattr(server, "_POOL_RESULT_DEADLINE_SECS", 5.0)
    monkeypatch.setattr(server, "_HOOK_LANE_BUDGET_SECS", _BUDGET)
    monkeypatch.setattr(server, "_prewarm_noop", lambda: None)
    monkeypatch.setattr(telemetry, "flush_requests", lambda *_a, **_k: None)
    _RELEASE.clear()
    ctx = server._ServerContext(
        name="pipe-hook-never-parks", sid="sid-hook-never-parks", version_state=_VersionState(), engine_root=tmp_path
    )
    ctx._claim_dir = tmp_path / "claims"
    ctx._claim_dir.mkdir()
    ctx._stall_dir = tmp_path / "stall"
    ctx._dispatch_pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=server.DISPATCH_PROCESS_POOL_SIZE + server._LIGHT_RESERVE_WORKERS
    )
    workers = server._guard_pool_workers()
    ctx._guard_pool = _ThreadBackedPool(0.0, 3 * workers)
    built: list = []

    def _build(standby=False):
        fresh = _ThreadBackedPool(_BOOT, 3 * workers)
        built.append(fresh)
        return fresh

    ctx._build_guard_pool = _build
    return ctx, built


def _close(ctx, built):
    _RELEASE.set()
    for pool in (ctx._dispatch_pool, ctx._guard_pool, ctx.__dict__.get("_guard_standby"), *built):
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)


def _wedge_guard_workers(ctx, count):
    results: list = []
    threads = [
        threading.Thread(target=lambda i=i: results.append(ctx._pool_dispatch(_wedge_msg(100 + i))), daemon=True)
        for i in range(count)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(results) == count
    assert len(ctx._guard_overdue) == count


def test_sendmessage_hook_answers_after_every_guard_worker_went_overdue(tmp_path, monkeypatch):
    ctx, built = _ctx(tmp_path, monkeypatch)
    try:
        _wedge_guard_workers(ctx, server._guard_pool_workers())
        assert ctx._guard_standby_ready.wait(10), "the standby never booted"

        started = time.monotonic()
        out = ctx._pool_dispatch(_sendmessage_frame())

        assert out.get("result") == {"ok": "invoke.from_argv"}, out
        assert time.monotonic() - started < _FAST
    finally:
        _close(ctx, built)


def test_hook_while_the_standby_boots_is_refused_at_once_and_does_not_recycle(tmp_path, monkeypatch):
    ctx, built = _ctx(tmp_path, monkeypatch)
    old = ctx._guard_pool
    try:
        _wedge_guard_workers(ctx, server._guard_pool_workers())

        started = time.monotonic()
        out = ctx._pool_dispatch(_sendmessage_frame())

        assert "error" in out, out
        assert time.monotonic() - started < _FAST, "a hook parked on the result wait"
        assert ctx._guard_pool is old, "a cold replacement was swapped in"
        assert len(ctx._guard_overdue) == server._guard_pool_workers()
    finally:
        _close(ctx, built)


def test_hook_wedge_with_one_healthy_guard_worker_still_answers(tmp_path, monkeypatch):
    ctx, built = _ctx(tmp_path, monkeypatch)
    try:
        _wedge_guard_workers(ctx, server._guard_pool_workers() - 1)

        out = ctx._pool_dispatch(_sendmessage_frame())

        assert out.get("result") == {"ok": "invoke.from_argv"}, out
        assert not built, "a pool below every worker overdue is not replaced"
    finally:
        _close(ctx, built)


def test_recycle_record_carries_the_worker_stacks_block(tmp_path, monkeypatch):
    ctx, built = _ctx(tmp_path, monkeypatch)
    lines: list = []
    monkeypatch.setattr(server.stall_writer, "submit", lambda path, text: lines.append((path, text)))
    monkeypatch.setattr(server, "_collect_worker_stacks", lambda stall_dir: "--- pool workers ---\nworker-pid: 1\n")
    try:
        _wedge_guard_workers(ctx, server._guard_pool_workers())
        assert ctx._guard_standby_ready.wait(10)
        ctx._pool_dispatch(_sendmessage_frame())

        recycles = [text for path, text in lines if str(path).endswith("-recycle.txt")]
        assert len(recycles) == 1 and "--- pool workers ---" in recycles[0], recycles
    finally:
        _close(ctx, built)
