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

    # Spawn and import the worker before the 1.0s result deadline starts, so the
    # claim is taken inside it and the reply is "running", not a withdrawn queue.
    warm = pool.inner.submit(
        _worker_with_probe_body, _msg(tmp_path, "warm", claim=str(tmp_path / "none")), None
    )
    assert warm.result(timeout=_WAIT) == {
        "jsonrpc": "2.0",
        "id": 5,
        "result": {"claim_present": False},
    }

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


def _recurse(depth):
    return 0 if depth == 0 else 1 + _recurse(depth - 1)


def test_stall_dump_is_a_gil_holding_timer_and_survives_a_frame_churning_op(tmp_path, monkeypatch):
    import faulthandler

    log = (tmp_path / "w.log").open("w")
    monkeypatch.setattr(server, "_WORKER_STDERR_FILE", log)

    def _no_c_watchdog(*a, **k):
        raise AssertionError("the C watchdog walks live frames without the GIL")

    monkeypatch.setattr(faulthandler, "dump_traceback_later", _no_c_watchdog)

    disarm = server._arm_stall_dump(0.2)
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        _recurse(150)
    disarm()
    log.flush()

    text = (tmp_path / "w.log").read_text()
    assert "_recurse" in text or "test_stall_dump" in text
    log.close()


def test_stall_dump_names_the_in_flight_sink_before_the_traceback(tmp_path, monkeypatch):
    from coordinator_core import atomic_append

    log = (tmp_path / "w.log").open("w")
    monkeypatch.setattr(server, "_WORKER_STDERR_FILE", log)
    monkeypatch.setattr(atomic_append, "in_flight_sinks", lambda: ("wedged/sink.jsonl",))

    disarm = server._arm_stall_dump(0.1)
    time.sleep(0.6)
    disarm()
    log.flush()

    text = (tmp_path / "w.log").read_text()
    assert "stall-dump sinks: ('wedged/sink.jsonl',)" in text
    assert text.index("stall-dump sinks:") < text.index("Thread")
    log.close()


def test_a_disarmed_stall_dump_writes_nothing(tmp_path, monkeypatch):
    log = (tmp_path / "w.log").open("w")
    monkeypatch.setattr(server, "_WORKER_STDERR_FILE", log)

    server._arm_stall_dump(0.2)()
    time.sleep(0.5)
    log.flush()

    assert (tmp_path / "w.log").read_text() == ""
    log.close()


def test_stall_dump_without_a_log_or_with_zero_secs_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_WORKER_STDERR_FILE", None)
    server._arm_stall_dump(0.2)()
    log = (tmp_path / "w.log").open("w")
    monkeypatch.setattr(server, "_WORKER_STDERR_FILE", log)
    server._arm_stall_dump(0)()
    assert not [t for t in threading.enumerate() if t.name == "stall-dump"]
    log.close()


def _header(record: Path) -> dict:
    head = record.read_text(encoding="utf-8").partition("--- threads ---")[0]
    return dict(line.split(": ", 1) for line in head.strip().split("\n"))


def test_stall_record_carries_fs_probe_and_per_thread_elapsed(tmp_path, monkeypatch):
    import json

    stall_dir = tmp_path / "stall-dumps"
    monkeypatch.setattr(server, "_WORKER_STDERR_FILE", None)
    monkeypatch.setattr(server, "_WORKER_STALL_DIR", stall_dir)
    claim = tmp_path / "k1"
    claim.write_text("1")
    disarm = server._arm_stall_dump(0.1, claim)
    assert _wait_for(lambda: stall_dir.is_dir() and any(stall_dir.glob("*.txt")))
    disarm()

    (record,) = stall_dir.glob("*.txt")
    header = _header(record)
    assert float(header["fs-probe-ms"]) >= 0
    assert float(header["op-elapsed-s"]) >= 0.1
    assert float(header["process-cpu-s"]) >= 0
    elapsed = json.loads(header["thread-elapsed-s"])
    assert f"0x{threading.get_ident():x}" in elapsed
    assert not list(stall_dir.glob(".probe-*"))


def test_fs_probe_reports_an_error_not_a_time_when_the_dir_is_unusable(tmp_path):
    assert server._fs_probe_ms(tmp_path / "missing").startswith("error:")


def test_blocked_leg_names_the_handler_thread_not_the_parked_arming_thread(tmp_path):
    import json

    release = threading.Event()
    entered = threading.Event()

    def _run_handler_recording():
        entered.set()
        release.wait(30)

    worker = threading.Thread(target=_run_handler_recording)
    worker.start()
    try:
        assert entered.wait(10)
        claim = tmp_path / "k1"
        server._write_blocked_leg(claim, threading.get_ident(), 0.0, 1.0)
        leg = json.loads(server._leg_path(claim).read_text(encoding="utf-8"))
        assert any("_run_handler_recording" in f for f in leg["frames"])
    finally:
        release.set()
        worker.join()


def test_leg_trace_run_length_encodes_samples_into_timed_rows():
    trace = server._LegTrace()
    trace.note(0.0, "a.py::f")
    trace.note(0.5, "a.py::f")
    trace.note(1.0, None)
    trace.note(1.5, "b.py::g > c.py::h")
    trace.close(2.5)
    assert trace._rows == [[0, 500, "a.py::f"], [1500, 1000, "b.py::g > c.py::h"]]


def _slow_leg_one(release):
    release.wait(30)


def _slow_leg_two(release):
    release.wait(30)


def test_stall_record_times_each_leg_the_handler_thread_sat_in(tmp_path, monkeypatch):
    import json

    stall_dir = tmp_path / "stall-dumps"
    monkeypatch.setattr(server, "_WORKER_STDERR_FILE", None)
    monkeypatch.setattr(server, "_WORKER_STALL_DIR", stall_dir)
    monkeypatch.setattr(server, "_LEG_SAMPLE_SECS", 0.05)
    leg_one, leg_two = threading.Event(), threading.Event()
    entered = threading.Event()

    def _run_handler_recording():
        entered.set()
        _slow_leg_one(leg_one)
        _slow_leg_two(leg_two)

    worker = threading.Thread(target=_run_handler_recording)
    worker.start()
    try:
        assert entered.wait(10)
        claim = tmp_path / "k1"
        claim.write_text("1")
        disarm = server._arm_stall_dump(0.1, claim)
        assert _wait_for(lambda: stall_dir.is_dir() and any(stall_dir.glob("*.txt")))
        time.sleep(0.4)
        leg_one.set()
        time.sleep(0.4)
        disarm()
        leg_two.set()
        assert _wait_for(lambda: "legs-ms" in next(stall_dir.glob("*.txt")).read_text("utf-8"))
    finally:
        leg_one.set()
        leg_two.set()
        worker.join()

    (record,) = stall_dir.glob("*.txt")
    rows = json.loads(_header(record)["legs-ms"])
    labels = [row[2] for row in rows]
    assert any("_slow_leg_one" in label for label in labels)
    assert any("_slow_leg_two" in label for label in labels)
    first = next(row for row in rows if "_slow_leg_one" in row[2])
    assert first[1] >= 200 and rows == sorted(rows, key=lambda r: r[0])
    assert "--- threads ---" in record.read_text(encoding="utf-8")


def test_a_hang_with_no_claim_still_writes_an_accept_record(tmp_path):
    stall_dir = tmp_path / "stall-dumps"
    phase = ["gate"]
    disarm = server._arm_accept_stall(stall_dir, 0.1, tmp_path / "unclaimed", "k1", phase)
    assert _wait_for(lambda: any(stall_dir.glob("*-accept-k1.txt")) if stall_dir.is_dir() else False)
    disarm()

    (record,) = stall_dir.glob("*-accept-k1.txt")
    header = _header(record)
    assert "_pool_dispatch:gate" in header["leg"]
    assert float(header["fs-probe-ms"]) >= 0


def test_a_claimed_or_disarmed_request_writes_no_accept_record(tmp_path):
    stall_dir = tmp_path / "stall-dumps"
    claim = tmp_path / "claimed"
    claim.write_text("1")
    server._arm_accept_stall(stall_dir, 0.1, claim, "k1", ["result"])
    server._arm_accept_stall(stall_dir, 0.1, tmp_path / "other", "k2", ["gate"])()
    time.sleep(0.6)
    assert not list(stall_dir.glob("*.txt")) if stall_dir.exists() else True


def test_stall_record_carries_a_timed_probe_of_the_op_repo_root(tmp_path, monkeypatch):
    stall_dir = tmp_path / "stall-dumps"
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    monkeypatch.setattr(server, "_WORKER_STDERR_FILE", None)
    monkeypatch.setattr(server, "_WORKER_STALL_DIR", stall_dir)
    claim = tmp_path / "k1"
    claim.write_text("1")
    disarm = server._arm_stall_dump(0.1, claim, repo)
    assert _wait_for(lambda: stall_dir.is_dir() and any(stall_dir.glob("*.txt")))
    disarm()

    (record,) = stall_dir.glob("*.txt")
    header = _header(record)
    assert float(header["repo-drive-probe-ms"]) >= 0
    assert float(header["fs-probe-ms"]) >= 0


def test_repo_drive_probe_is_unavailable_without_a_root_and_error_for_a_missing_one(tmp_path):
    assert server._repo_drive_probe_ms(None) == "unavailable"
    assert server._repo_drive_probe_ms(tmp_path / "missing").startswith("error:")


def test_stall_record_without_a_repo_root_marks_the_probe_unavailable(tmp_path):
    stall_dir = tmp_path / "stall-dumps"
    record = server._write_stall_record(stall_dir, None, key="k", leg="x")
    assert _header(record)["repo-drive-probe-ms"] == "unavailable"


def test_accept_side_stall_record_probes_the_repo_root(tmp_path):
    stall_dir = tmp_path / "stall-dumps"
    repo = tmp_path / "repo"
    repo.mkdir()
    disarm = server._arm_accept_stall(stall_dir, 0.1, tmp_path / "unclaimed", "k1", ["gate"], repo)
    assert _wait_for(lambda: stall_dir.is_dir() and any(stall_dir.glob("*.txt")))
    disarm()
    (record,) = stall_dir.glob("*.txt")
    assert float(_header(record)["repo-drive-probe-ms"]) >= 0


def test_op_repo_root_reads_the_envelope_and_never_raises(tmp_path):
    assert server._op_repo_root({"_caller_cwd": str(tmp_path)}) == tmp_path
    assert server._op_repo_root(None) is None
    assert server._op_repo_root({"_origin_worktree": str(tmp_path), "_caller_cwd": "x"}) == tmp_path
