"""Pool dispatch against a REAL spawn-started ProcessPoolExecutor: a request
that never started is told from one that is running by the O_EXCL claim file,
and a stalled worker leaves its stacks in its stderr log.

The worker entry points below are module-level so the spawn child can import
them; each swaps `server._pool_dispatch_worker_body` inside the child.
"""

from __future__ import annotations

import concurrent.futures
import multiprocessing
import os
import threading
import time
from pathlib import Path

import pytest

from coordinator_core.warm import dispatch_ack, server

pytestmark = pytest.mark.spawns_process

_WAIT = 90.0


def _wait_for(predicate, timeout=_WAIT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _block_until_released(msg, caller):
    params = msg["params"]
    Path(params["started"]).write_text("1")
    deadline = time.monotonic() + 60
    while not Path(params["release"]).exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"held": True}}


def _probe_claim(msg, caller):
    present = Path(msg["params"]["claim"]).exists()
    return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"claim_present": present}}


def _worker_with_blocking_body(msg, caller, dispatch_key=None, claim_dir=None, stall_secs=10.0):
    server._pool_dispatch_worker_body = _block_until_released
    return server._pool_dispatch_worker(msg, caller, dispatch_key, claim_dir, stall_secs)


def _worker_with_probe_body(msg, caller, dispatch_key=None, claim_dir=None, stall_secs=10.0):
    server._pool_dispatch_worker_body = _probe_claim
    return server._pool_dispatch_worker(msg, caller, dispatch_key, claim_dir, stall_secs)


class _RecordingPool:
    def __init__(self, inner):
        self.inner = inner
        self.futures = []

    def submit(self, *a, **k):
        fut = self.inner.submit(*a, **k)
        self.futures.append(fut)
        return fut


@pytest.fixture
def rig(tmp_path, monkeypatch):
    stderr_dir = tmp_path / "pool-worker-stderr"
    claim_dir = tmp_path / "dispatch-claims"
    claim_dir.mkdir()
    inner = concurrent.futures.ProcessPoolExecutor(
        max_workers=1,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=server._worker_process_init,
        initargs=(str(stderr_dir),),
    )
    pool = _RecordingPool(inner)
    ctx = server._ServerContext.__new__(server._ServerContext)
    ctx._pool_outstanding = server.InFlightCounter()
    ctx._dispatch_pool = pool
    ctx._dispatch_pool_lock = threading.Lock()
    ctx._claim_dir = claim_dir
    ctx._worker_stderr_dir = stderr_dir
    monkeypatch.setattr(server, "_POOL_RESULT_DEADLINE_SECS", 1.0)
    release = tmp_path / "release"
    yield ctx, pool, tmp_path, claim_dir, stderr_dir, release
    release.write_text("1")
    inner.shutdown(wait=True)


def _msg(tmp_path, tag, method="ceremony.commit_v2", **extra):
    params = {"started": str(tmp_path / f"{tag}.started"), "release": str(tmp_path / "release")}
    params.update(extra)
    return {"jsonrpc": "2.0", "id": 5, "method": method, "params": params}


def _admit():
    key = dispatch_ack.mint_key()
    assert server._ack_store.admit(key, "ceremony.commit_v2")
    return key


def test_a_queued_request_is_withdrawn_not_indeterminate_and_never_runs(rig, monkeypatch):
    ctx, pool, tmp_path, claim_dir, _stderr, release = rig
    monkeypatch.setattr(server, "_pool_dispatch_worker", _worker_with_blocking_body)

    holder = pool.inner.submit(_worker_with_blocking_body, _msg(tmp_path, "holder"), None)
    assert _wait_for(lambda: (tmp_path / "holder.started").exists())

    key = _admit()
    out = ctx._pool_dispatch(_msg(tmp_path, "queued"), dispatch_key=key)

    assert out["error"]["code"] == server.INTERNAL_ERROR
    assert out["error"]["data"]["diagnosis"]["phase"] == "queued"
    assert server._ack_store.status(key) == {
        "state": dispatch_ack.STATE_NOT_RECEIVED,
        "outcome": dispatch_ack.OUTCOME_NOT_DISPATCHED,
    }

    release.write_text("1")
    holder.result(timeout=_WAIT)
    queued_future = pool.futures[-1]
    # The done-callback pops the marker, so a bare {} is the withdrawn reply.
    assert queued_future.result(timeout=_WAIT) == {}
    assert not (tmp_path / "queued.started").exists()
    assert _wait_for(lambda: not list(claim_dir.iterdir()))


def test_a_stalled_worker_leaves_its_stacks_and_the_diagnosis_names_them(rig, monkeypatch):
    ctx, pool, tmp_path, _claims, stderr_dir, release = rig
    monkeypatch.setattr(server, "_pool_dispatch_worker", _worker_with_blocking_body)
    monkeypatch.setattr(server, "STALL_DUMP_SECS", 0.3)

    key = _admit()
    out = ctx._pool_dispatch(_msg(tmp_path, "stalled"), dispatch_key=key)

    assert out["error"]["code"] == server.WARM_DISPATCH_INDETERMINATE
    diagnosis = out["error"]["data"]["diagnosis"]
    assert diagnosis["phase"] == "running"
    dump = Path(diagnosis["stall_dump"])
    assert dump.parent == stderr_dir
    assert _wait_for(lambda: dump.exists() and "_block_until_released" in dump.read_text())

    release.write_text("1")
    pool.futures[-1].result(timeout=_WAIT)


def test_happy_path_takes_and_releases_the_claim(rig, monkeypatch):
    ctx, pool, tmp_path, claim_dir, _stderr, _release = rig
    monkeypatch.setattr(server, "_pool_dispatch_worker", _worker_with_probe_body)
    monkeypatch.setattr(server, "_POOL_RESULT_DEADLINE_SECS", _WAIT)

    key = _admit()
    out = ctx._pool_dispatch(
        _msg(tmp_path, "happy", claim=str(claim_dir / key)), dispatch_key=key
    )

    assert out["result"] == {"claim_present": True}
    assert _wait_for(lambda: not (claim_dir / key).exists())


def test_claim_is_exclusive_and_records_its_owner(tmp_path):
    path = tmp_path / "k"
    assert server._take_claim(path, 1234) is True
    assert server._take_claim(path, 99) is False
    assert path.read_text() == "1234"


@pytest.mark.parametrize("key", ["", ".hidden", "a/b", "a\\b", "..", "k k"])
def test_an_unsafe_key_gets_no_claim(tmp_path, key):
    assert server._claim_path(tmp_path, key) is None
    assert server._claim_path(tmp_path, "123-456") == tmp_path / "123-456"


def test_an_unusable_claim_dir_never_claims_withdrawal_on_the_accept_side(tmp_path):
    missing = tmp_path / "no-such-dir" / "k"
    assert server._take_claim(missing, 1) is True
    assert server._take_claim(missing, 1, on_error=False) is False


def test_an_unsafe_key_gets_no_reconcile_command():
    envelope = {"error": {"code": server.WARM_DISPATCH_INDETERMINATE, "data": {}}}
    out = server._with_reconcile_route(envelope, "x'; rm -rf ~; '")
    assert "reconcile_command" not in out["error"]["data"]
    safe = server._with_reconcile_route({"error": {"data": {}}}, "123-456")
    assert "123-456" in safe["error"]["data"]["reconcile_command"]
