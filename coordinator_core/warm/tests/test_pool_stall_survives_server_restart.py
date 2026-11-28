"""A stalled ack-journal write must not hold up result delivery for the whole dispatch pool.

`_pool_dispatch_untimed` registers `_stamp_pool_future_async` as a done-callback; a
`ProcessPoolExecutor` runs done-callbacks on its single management thread, so the journal write
(the day bucket persists across a server restart and grows by one file per finished op) must run on
the stamp writer thread, or one slow write stops every other future of that pool from resolving.
Drives the real `_pool_dispatch_untimed` over real worker processes (a stand-in body)."""

from __future__ import annotations

import concurrent.futures
import threading
import time

import pytest

from coordinator_core.warm import dispatch_ack, server, telemetry

_HOOK = "hooks.preuse_write_dispatch"
_POOL_DEADLINE_SECS = 6.0


class _VersionState:
    server_sha = "deadbeef"

    def is_skewed(self, client_token: str) -> bool:
        return False

    def is_token_skewed(self, client_token: str) -> bool:
        return False


def _echo_worker(msg, caller, *_rest):
    """Module-level so a spawned pool process can unpickle it: answers at once, touches no disk."""
    return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"ok": msg["method"]}}


def _msg(i):
    return {"jsonrpc": "2.0", "id": i, "method": _HOOK, "params": {}}


def _dispatch(ctx, i, out):
    key = dispatch_ack.mint_key()
    server._ack_store.admit(key, _HOOK)
    out[i] = ctx._pool_dispatch_untimed(_msg(i), dispatch_key=key)


@pytest.mark.parametrize("stalled", [False, True], ids=["journal-healthy", "journal-stalled"])
def test_a_stalled_journal_write_does_not_stop_the_pool_delivering_the_next_result(tmp_path, monkeypatch, stalled):
    monkeypatch.setattr(server, "_pool_dispatch_worker", _echo_worker)
    monkeypatch.setattr(server, "_POOL_RESULT_DEADLINE_SECS", _POOL_DEADLINE_SECS)
    monkeypatch.setattr(telemetry, "flush_requests", lambda *_a, **_k: None)
    ctx = server._ServerContext(
        name="pipe-restart-stall", sid="sid-restart-stall", version_state=_VersionState(), engine_root=tmp_path
    )
    ctx._claim_dir = tmp_path / "claims"
    ctx._claim_dir.mkdir()
    ctx._stall_dir = tmp_path / "stall"
    journal = dispatch_ack.journal_dir_beside(ctx._claim_dir)
    store = dispatch_ack.AckStore(journal_dir=journal)
    monkeypatch.setattr(server, "_ack_store", store)

    release = threading.Event()
    entered = threading.Event()
    writer: list = []

    def _stalled_record_outcome(*_a, **_k):
        writer.append(threading.current_thread())
        entered.set()
        if stalled:
            release.wait(30)
        return True

    monkeypatch.setattr(dispatch_ack, "record_outcome", _stalled_record_outcome)
    pool = concurrent.futures.ProcessPoolExecutor(max_workers=2)
    ctx._guard_pool = pool
    ctx._dispatch_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        out: dict = {}
        first = threading.Thread(target=_dispatch, args=(ctx, 1, out), daemon=True)
        first.start()
        assert entered.wait(20), "the first op never reached its journal write"
        t0 = time.monotonic()
        second = threading.Thread(target=_dispatch, args=(ctx, 2, out), daemon=True)
        second.start()
        second.join(_POOL_DEADLINE_SECS + 4)
        assert out.get(2, {}).get("result") == {"ok": _HOOK}, out
        assert time.monotonic() - t0 < _POOL_DEADLINE_SECS, "the second result waited out the pool deadline"
        manager = getattr(pool, "_executor_manager_thread", None)
        assert not stalled or manager is None or writer[0] is not manager, "the journal write ran on the pool's management thread"
        release.set()
        deadline = time.monotonic() + 10
        while len(writer) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert len(writer) == 2, "the deferred journal stamps never landed"
    finally:
        release.set()
        pool.shutdown(wait=False, cancel_futures=True)
        ctx._dispatch_pool.shutdown(wait=False, cancel_futures=True)
