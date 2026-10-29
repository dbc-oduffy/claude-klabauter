"""A running-phase -32004 on a default mutating op names the op thread's blocked
leg in `error.data.diagnosis.blocked_leg` and in its op-latency `timeout` row.

Worker entry points are module-level so the spawn child can import them.
"""

from __future__ import annotations

import concurrent.futures
import json
import multiprocessing
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


def _the_blocking_leg(release):
    deadline = time.monotonic() + 60
    while not Path(release).exists() and time.monotonic() < deadline:
        time.sleep(0.05)


def _block_in_a_named_leg(msg, caller):
    _the_blocking_leg(msg["params"]["release"])
    return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"held": True}}


def _noop_body(msg, caller):
    return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {}}


def _worker_blocking(msg, caller, dispatch_key=None, claim_dir=None, stall_secs=10.0):
    server._pool_dispatch_worker_body = _block_in_a_named_leg
    return server._pool_dispatch_worker(msg, caller, dispatch_key, claim_dir, stall_secs)


def _worker_noop(msg, caller, dispatch_key=None, claim_dir=None, stall_secs=10.0):
    server._pool_dispatch_worker_body = _noop_body
    return server._pool_dispatch_worker(msg, caller, dispatch_key, claim_dir, stall_secs)


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
    ctx = server._ServerContext.__new__(server._ServerContext)
    ctx._pool_outstanding = server.InFlightCounter()
    ctx._dispatch_pool = inner
    ctx._dispatch_pool_lock = threading.Lock()
    ctx._claim_dir = claim_dir
    ctx._worker_stderr_dir = stderr_dir
    monkeypatch.setattr(server, "_POOL_RESULT_DEADLINE_SECS", 1.5)
    monkeypatch.setattr(server, "STALL_DUMP_SECS", 0.3)
    release = tmp_path / "release"
    yield ctx, inner, tmp_path, claim_dir, release
    release.write_text("1")
    inner.shutdown(wait=True)


def _msg(tmp_path, method="ceremony.commit_v2"):
    return {
        "jsonrpc": "2.0",
        "id": 7,
        "method": method,
        "params": {"release": str(tmp_path / "release")},
    }


def test_a_default_mutating_op_past_its_deadline_names_its_blocked_leg(rig, monkeypatch):
    ctx, pool, tmp_path, claim_dir, release = rig
    monkeypatch.setattr(server, "_pool_dispatch_worker", _worker_blocking)
    rows = []
    monkeypatch.setattr(
        server.op_latency, "record_op_latency", lambda **kw: rows.append(kw)
    )

    # Boot the worker before the deadline clock starts.
    assert pool.submit(_worker_noop, _msg(tmp_path), None).result(timeout=_WAIT)

    key = dispatch_ack.mint_key()
    assert server._ack_store.admit(key, "ceremony.commit_v2")
    out = ctx._pool_dispatch(_msg(tmp_path), dispatch_key=key)

    assert out["error"]["code"] == server.WARM_DISPATCH_INDETERMINATE
    diagnosis = out["error"]["data"]["diagnosis"]
    assert diagnosis["phase"] == "running"
    leg = diagnosis["blocked_leg"]
    assert leg["leg"].endswith("test_pool_timeout_names_blocked_leg.py::_the_blocking_leg")
    assert leg["kind"] == "wait"
    assert 1 <= len(leg["frames"]) <= 8
    assert any("_the_blocking_leg" in f for f in leg["frames"])

    (row,) = rows
    assert row["outcome"] == "timeout"
    assert row["error_code"] == server.WARM_DISPATCH_INDETERMINATE
    assert row["blocked_leg"] == leg["leg"]

    release.write_text("1")
    assert _wait_for(lambda: not list(claim_dir.iterdir()))


def test_the_leg_is_written_without_a_stderr_file_and_only_with_a_claim(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_WORKER_STDERR_FILE", None)

    claim = tmp_path / "k"
    claim.write_text("1")
    disarm = server._arm_stall_dump(0.1, claim)
    leg_file = server._leg_path(claim)
    assert _wait_for(leg_file.exists, 5.0)
    disarm()
    record = json.loads(leg_file.read_text(encoding="utf-8"))
    assert set(record) == {"leg", "kind", "frames"}
    assert record["kind"] in {"wait", "compute"}

    server._release_claim(claim)
    assert not claim.exists() and not leg_file.exists()

    server._arm_stall_dump(0.1)()
    time.sleep(0.4)
    assert list(tmp_path.iterdir()) == []


def test_a_disarmed_arm_writes_no_leg(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_WORKER_STDERR_FILE", None)
    claim = tmp_path / "k"
    claim.write_text("1")
    server._arm_stall_dump(0.2, claim)()
    time.sleep(0.5)
    assert not server._leg_path(claim).exists()


def test_a_missing_or_torn_leg_leaves_the_diagnosis_without_one(tmp_path):
    claim = tmp_path / "k"
    claim.write_text("4242")
    assert "blocked_leg" not in server._running_phase_diagnosis(claim, None)
    server._leg_path(claim).write_text("{not json")
    assert "blocked_leg" not in server._running_phase_diagnosis(claim, None)
