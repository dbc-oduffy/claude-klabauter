"""A handler blocked forever never stalls peer callers: hooks and memo.send reach the reserved
slots even with every heavy slot wedged, and a budget-crossing stall names its dump in error.data."""

from __future__ import annotations

import concurrent.futures
import threading
import time

from coordinator_core.warm import server

_RELEASE = threading.Event()


def _body(msg, caller):
    if msg["method"] in ("dispatch.ask_stage", "review.prep"):
        _RELEASE.wait(30)
    return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"ok": msg["method"]}}


def _ctx(tmp_path, workers):
    ctx = server._ServerContext.__new__(server._ServerContext)
    ctx._pool_outstanding = server.InFlightCounter()
    ctx._dispatch_pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    ctx._dispatch_pool_lock = threading.Lock()
    ctx._claim_dir = tmp_path / "claims"
    ctx._claim_dir.mkdir()
    ctx._worker_stderr_dir = tmp_path / "stderr"
    ctx._stall_dir = tmp_path / "stall"
    return ctx


def _msg(method, i=1):
    return {"jsonrpc": "2.0", "id": i, "method": method, "params": {}}


def test_light_ops_answer_while_every_heavy_slot_is_wedged(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DISPATCH_PROCESS_POOL_SIZE", 2)
    monkeypatch.setattr(server, "_pool_dispatch_worker_body", _body)
    monkeypatch.setattr(server, "_POOL_RESULT_DEADLINE_SECS", 20.0)
    _RELEASE.clear()
    ctx = _ctx(tmp_path, 2 + server._LIGHT_RESERVE_WORKERS)
    wedged = [
        threading.Thread(target=ctx._pool_dispatch, args=(_msg("dispatch.ask_stage", i),), daemon=True)
        for i in range(2)
    ]
    try:
        for t in wedged:
            t.start()
        time.sleep(0.3)
        for method in ("hooks.preuse_write_dispatch", "memo.send"):
            t0 = time.monotonic()
            out = ctx._pool_dispatch(_msg(method, 7))
            assert out.get("result") == {"ok": method}, out
            assert time.monotonic() - t0 < 2.0
    finally:
        _RELEASE.set()
        for t in wedged:
            t.join(5)
        ctx._dispatch_pool.shutdown(wait=False, cancel_futures=True)


def test_a_heavy_op_still_queues_behind_wedged_heavy_slots():
    gate = server._AdmissionGate(1, 2)
    assert gate.acquire(True, 0.0) == 0.0
    assert gate.acquire(True, 0.05) is None
    assert gate.acquire(False, 0.05, hook=True) == 0.0
    assert gate.acquire(True, 0.05, light=True) == 0.0


def test_budget_crossing_stall_names_dump_or_absent_reason():
    data: dict = {"diagnosis": {"stall_dump": "/x/a.txt"}}
    server._name_stall_dump(data, "Stall dump: /x/a.txt.")
    assert data["stall_dump"] == "/x/a.txt" and "stall_dump_absent_reason" not in data

    data = {"diagnosis": {}}
    server._name_stall_dump(data, "No stall dump written: no stall directory is configured.")
    assert data["stall_dump_absent_reason"] == "no stall directory is configured."
    assert "stall_dump" not in data


def test_stall_dump_arms_inside_a_short_op_budget():
    assert server._stall_arm_secs(6.0) == 3.0
    assert server._stall_arm_secs(28.0) == server.STALL_DUMP_SECS


def test_timed_out_running_op_error_data_names_its_dump(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DISPATCH_PROCESS_POOL_SIZE", 2)
    monkeypatch.setattr(server, "_pool_dispatch_worker_body", _body)
    monkeypatch.setattr(server, "_withdrawal_deadline_secs", lambda *_a, **_k: 0.3)
    _RELEASE.clear()
    ctx = _ctx(tmp_path, 2 + server._LIGHT_RESERVE_WORKERS)
    try:
        out = ctx._pool_dispatch(_msg("dispatch.ask_stage"), dispatch_key="k-1")
    finally:
        _RELEASE.set()
        ctx._dispatch_pool.shutdown(wait=False, cancel_futures=True)
    data = out["error"]["data"]
    assert data.get("stall_dump") or data.get("stall_dump_absent_reason")
    if "stall_dump" in data:
        assert data["stall_dump"] in out["error"]["message"]


def test_thread_stacks_render_without_faulthandler_file_writes(monkeypatch):
    import faulthandler

    monkeypatch.setattr(faulthandler, "dump_traceback", lambda **kw: (_ for _ in ()).throw(AssertionError("GIL-held fd write")))
    text = server._render_all_thread_stacks()
    assert "Current thread 0x" in text and "most recent call first" in text
    assert "_render_all_thread_stacks" in text


def test_stall_record_survives_a_blocking_temp_dir(tmp_path, monkeypatch):
    import tempfile

    monkeypatch.setattr(tempfile, "TemporaryFile", lambda *a, **k: (_ for _ in ()).throw(AssertionError("temp file used")))
    record = server._write_stall_record(tmp_path / "stall", None, key="k")
    assert record is not None and "--- threads ---" in record.read_text(encoding="utf-8")


def _isolated_ctx(tmp_path):
    ctx = _ctx(tmp_path, 1)
    ctx._guard_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    ctx._guard_pool_lock = threading.Lock()
    return ctx


def test_hook_class_op_runs_in_the_guard_pool_and_heavy_op_in_the_main_pool(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_pool_dispatch_worker_body", lambda msg, caller: {"jsonrpc": "2.0", "id": 1, "result": {"pool": threading.current_thread().name}})
    ctx = _isolated_ctx(tmp_path)
    try:
        ctx._dispatch_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="heavy")
        ctx._guard_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="guard")
        assert ctx._pool_dispatch(_msg("hooks.preuse_write_dispatch"))["result"]["pool"].startswith("guard")
        assert ctx._pool_dispatch(_msg("memo.send"))["result"]["pool"].startswith("guard")
        assert ctx._pool_dispatch(_msg("review.prep"))["result"]["pool"].startswith("heavy")
    finally:
        ctx._dispatch_pool.shutdown(wait=False, cancel_futures=True)
        ctx._guard_pool.shutdown(wait=False, cancel_futures=True)


def test_recycling_the_stalled_heavy_pool_leaves_an_in_flight_guard_op_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "STALL_DUMP_SECS", 0.01)
    monkeypatch.setattr(server, "DISPATCH_PROCESS_POOL_SIZE", 1)
    ctx = _isolated_ctx(tmp_path)
    ctx._stall_dir = tmp_path / "stall"
    heavy = concurrent.futures.Future()
    guard = concurrent.futures.Future()
    ctx._track_pool_task(heavy, "dispatch.ask_stage", None)
    ctx._track_pool_task(guard, "hooks.preuse_write_dispatch", None, guard=True)
    time.sleep(0.05)
    assert ctx._stalled_holders() == ["dispatch.ask_stage"]
    old_guard = ctx._guard_pool
    stuck = ctx._dispatch_pool
    ctx._recycle_stalled_pool(stuck, ["dispatch.ask_stage"])
    try:
        assert ctx._dispatch_pool is None
        assert ctx._guard_pool is old_guard
        assert guard in ctx._pool_tasks and heavy not in ctx._pool_tasks
        assert ctx._drop_pool(old_guard) and ctx._guard_pool is None and ctx._dispatch_pool is None
    finally:
        old_guard.shutdown(wait=False, cancel_futures=True)


def test_a_context_without_a_guard_pool_keeps_one_shared_pool(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_pool_dispatch_worker_body", lambda msg, caller: {"jsonrpc": "2.0", "id": 1, "result": {"pool": threading.current_thread().name}})
    ctx = _ctx(tmp_path, 2)
    ctx._dispatch_pool.shutdown(wait=False)
    ctx._dispatch_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="shared")
    try:
        ctx._guard_pool_lock = threading.Lock()
        ctx._guard_pool = None
        assert not server._guard_pool_applies(ctx.__dict__)
        assert ctx._pool_dispatch(_msg("hooks.preuse_write_dispatch"))["result"]["pool"].startswith("shared")
    finally:
        ctx._dispatch_pool.shutdown(wait=False, cancel_futures=True)
