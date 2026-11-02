"""A pool saturated by stalled holders on two consecutive checks is recycled: the
old pool is swapped out and terminated, exactly the keys tracked on it read
engine-restarted, and the current call runs on a fresh pool."""

from __future__ import annotations

import concurrent.futures
import threading
import time

import pytest

from coordinator_core.warm import dispatch_ack, server


class _FakePool:
    def __init__(self, *_a, **_k):
        self.submitted = []
        self.shutdown_args = None
        self._processes = {1: _FakeProc(), 2: _FakeProc()}

    def submit(self, *a, **k):
        fut = concurrent.futures.Future()
        self.submitted.append(fut)
        return fut

    def shutdown(self, wait=True, cancel_futures=False):
        self.shutdown_args = (wait, cancel_futures)


class _FakeProc:
    terminated = False

    def terminate(self):
        self.terminated = True


def _ctx(tmp_path, pool):
    ctx = server._ServerContext.__new__(server._ServerContext)
    ctx._pool_outstanding = server.InFlightCounter()
    ctx._dispatch_pool = pool
    ctx._dispatch_pool_lock = threading.Lock()
    ctx._claim_dir = tmp_path / "claims"
    ctx._worker_stderr_dir = tmp_path / "stderr"
    ctx._stall_dir = tmp_path / "stall-dumps"
    return ctx


def _msg(method="review.reverify"):
    return {"jsonrpc": "2.0", "id": 9, "method": method, "params": {}}


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DISPATCH_PROCESS_POOL_SIZE", 2)
    monkeypatch.setattr(server, "STALL_DUMP_SECS", 0.05)
    monkeypatch.setattr(server, "_POOL_RESULT_DEADLINE_SECS", 0.05)
    monkeypatch.setattr(server.breadcrumb, "svc_dir", lambda _root: tmp_path / "svc")
    monkeypatch.setattr(server, "_start_telemetry_drain", lambda _q: threading.Event())
    monkeypatch.setattr(server.concurrent.futures, "ProcessPoolExecutor", _FakePool)
    old = _FakePool()
    ctx = _ctx(tmp_path, old)
    ctx.engine_root = tmp_path
    return ctx, old


def _admit():
    key = dispatch_ack.mint_key()
    assert server._ack_store.admit(key, "dispatch.ask_stage")
    return key


def test_second_consecutive_saturated_check_recycles_and_reconciles_tracked_keys(rig, tmp_path):
    ctx, old = rig
    k1, k2, unsubmitted = _admit(), _admit(), _admit()
    futs = [old.submit(), old.submit()]
    ctx._track_pool_task(futs[0], "dispatch.ask_stage", k1)
    ctx._track_pool_task(futs[1], "dispatch.ask_stage", k2)
    time.sleep(0.1)

    first = ctx._pool_dispatch(_msg())
    assert first["error"]["data"]["diagnosis"]["phase"] == "saturated"
    assert ctx._dispatch_pool is old and not old.shutdown_args

    ctx._pool_dispatch(_msg())

    new = ctx._dispatch_pool
    assert isinstance(new, _FakePool) and new is not old
    assert len(new.submitted) == 1
    assert old.shutdown_args == (False, True)
    assert all(p.terminated for p in old._processes.values())
    for key in (k1, k2):
        assert server._ack_store.status(key) == {
            "state": "unknowable",
            "reason": "engine-restarted",
        }
    assert server._ack_store.status(unsubmitted).get("reason") != "engine-restarted"
    assert list((tmp_path / "stall-dumps").glob("*-recycle.txt"))


def test_worker_lost_stamp_from_the_dying_pool_keeps_engine_restarted(rig):
    ctx, old = rig
    key = _admit()
    fut = old.submit()
    ctx._track_pool_task(fut, "dispatch.ask_stage", key)
    fut.add_done_callback(lambda f: server._stamp_pool_future(key, f))
    ctx._track_pool_task(old.submit(), "dispatch.ask_stage")
    time.sleep(0.1)
    ctx._pool_dispatch(_msg())
    ctx._pool_dispatch(_msg())

    fut.set_exception(server.BrokenProcessPool("terminated"))

    status = server._ack_store.status(key)
    assert status == {"state": "unknowable", "reason": "engine-restarted"}


def test_unsaturated_check_resets_the_consecutive_count(rig):
    ctx, old = rig
    futs = [old.submit(), old.submit()]
    for fut in futs:
        ctx._track_pool_task(fut, "review.prep")
    time.sleep(0.1)

    ctx._pool_dispatch(_msg())  # saturated, count 1
    futs[0].set_result({})  # one holder frees
    ctx._pool_dispatch(_msg())  # unsaturated, resets
    assert ctx._dispatch_pool is old

    ctx._track_pool_task(old.submit(), "review.prep")
    time.sleep(0.1)
    out = ctx._pool_dispatch(_msg())  # saturated again, count 1 only
    assert out["error"]["data"]["diagnosis"]["phase"] == "saturated"
    assert ctx._dispatch_pool is old


class _RunningPool(_FakePool):
    """Workers that answer instantly except for `blocked_method`, which claims its request
    (as a real worker's first act) and never returns."""

    blocked_method = "dispatch.ask_stage"

    def submit(self, fn, msg, caller, *rest):
        fut = concurrent.futures.Future()
        self.submitted.append(fut)
        fut.set_running_or_notify_cancel()
        if msg.get("method") == self.blocked_method:
            key, claim_dir = rest[0], rest[1]
            if key and claim_dir:
                server.Path(claim_dir).mkdir(parents=True, exist_ok=True)
                (server.Path(claim_dir) / key).write_text("4242")
            return fut
        fut.set_result({"jsonrpc": "2.0", "id": msg["id"], "result": {"ok": True}})
        return fut


def test_blocked_handler_does_not_hold_up_hooks_or_memo_send_and_its_32004_names_a_dump(
    rig, monkeypatch
):
    ctx, _old = rig
    pool = _RunningPool()
    ctx._dispatch_pool = pool
    monkeypatch.setattr(server, "_POOL_RESULT_DEADLINE_SECS", 2.0)
    monkeypatch.setattr(server, "STALL_DUMP_SECS", 30.0)
    blocked_key = _admit()
    blocked_out = {}

    def _blocked():
        blocked_out["reply"] = ctx._pool_dispatch(
            _msg("dispatch.ask_stage"), dispatch_key=blocked_key
        )

    t = threading.Thread(target=_blocked)
    t.start()
    deadline = time.monotonic() + 1.0
    while not pool.submitted and time.monotonic() < deadline:
        time.sleep(0.01)
    assert pool.submitted

    t0 = time.monotonic()
    hook = ctx._pool_dispatch(_msg("hooks.preuse_write_dispatch"))
    memo_key = dispatch_ack.mint_key()
    assert server._ack_store.admit(memo_key, "memo.send")
    memo = ctx._pool_dispatch(_msg("memo.send"), dispatch_key=memo_key)
    assert time.monotonic() - t0 < 1.0
    assert hook["result"] == {"ok": True} and memo["result"] == {"ok": True}

    t.join(timeout=10)
    error = blocked_out["reply"]["error"]
    assert error["code"] == server.WARM_DISPATCH_INDETERMINATE
    dumps = list((ctx._stall_dir).glob("*-overdue-*.txt")) + list(
        (ctx._stall_dir).glob(f"*-{blocked_key}.txt")
    )
    assert dumps
    assert str(error["data"]["diagnosis"]["stall_dump"]) in error["message"]
    assert error["data"]["dispatch_key"] == blocked_key


def test_an_unwritable_stall_dir_says_no_dump_was_written(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_write_stall_record", lambda *a, **k: None)
    diagnosis = {"stall_dump": str(tmp_path / "stderr")}
    note = server._ensure_overdue_stall_dump(diagnosis, tmp_path / "stall", "k1", 3.0)
    assert note.startswith("No stall dump written")
    assert "no stall directory" in server._ensure_overdue_stall_dump({}, None, "k1", 3.0)


def test_a_worker_record_in_the_stall_dir_is_named_not_rewritten(tmp_path):
    record = tmp_path / "20260101T000000Z-1-k1.txt"
    record.write_text("x")
    note = server._ensure_overdue_stall_dump({"stall_dump": str(record)}, tmp_path, "k1", 3.0)
    assert note == f"Stall dump: {record}."
    assert list(tmp_path.glob("*.txt")) == [record]


def test_posix_engine_boost_is_recorded_and_workers_restore_the_base(monkeypatch):
    calls = []
    monkeypatch.setattr(server.sys, "platform", "linux")
    monkeypatch.setattr(server.os, "PRIO_PROCESS", 0, raising=False)
    monkeypatch.setattr(server.os, "getpriority", lambda *_a: 0, raising=False)
    monkeypatch.setattr(server.os, "setpriority", lambda *a: calls.append(a), raising=False)
    monkeypatch.setenv(server._BASE_NICE_ENV, "unset")

    assert server._raise_engine_priority() is True
    assert calls[-1] == (0, 0, -5)
    server._restore_worker_priority()
    assert calls[-1] == (0, 0, 0)


def test_priority_boost_failure_never_raises(monkeypatch):
    monkeypatch.setattr(server.sys, "platform", "linux")
    monkeypatch.setattr(server.os, "getpriority", lambda *_a: 0, raising=False)
    monkeypatch.setattr(server.os, "PRIO_PROCESS", 0, raising=False)

    def _deny(*_a):
        raise PermissionError

    monkeypatch.setattr(server.os, "setpriority", _deny, raising=False)
    assert server._raise_engine_priority() is False
