"""A REAL pool worker running dispatch.land_run is killed (`os._exit`) at three points; the
reconcile poll (`server._store_poll`, the body of `warm.request_status`) against the default
accept-process `AckStore` answers committed or never-ran from the durable land record, and never
never-ran once the commit spawned.

Worker entry points are module-level so the spawn child can import them; the worker's repo and
kill point reach it through the environment, which the spawn child inherits.
"""

from __future__ import annotations

import concurrent.futures
import multiprocessing
import os
import threading
import time
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import psutil
import pytest

from coordinator_core.ops.dispatch_emit import land_run, run_token
from coordinator_core.ops.dispatch_emit.tests.test_land_run_durable_land_record import (  # noqa: F401 -- fixture
    RUN,
    SCRIPT,
    SELF,
    _fire_land,
    _git,
    land_repo,
)
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.warm import dispatch_ack, server

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_WAIT = 120.0
_REPO_ENV = "LAND_E2E_REPO"
_CASE_ENV = "LAND_E2E_CASE"
_KILL_EXIT = 9


def _land_body(msg, caller):
    from coordinator_core.ops.dispatch_emit import land_intent

    case = os.environ[_CASE_ENV]
    root = Path(os.environ[_REPO_ENV])
    land_run._launch_close_job = lambda *a, **k: True
    if case == "before-commit":
        real_mark = land_intent.mark

        def _die_at_committing(phase, sha=None):
            if phase == land_intent.PHASE_COMMITTING:
                os._exit(_KILL_EXIT)
            real_mark(phase, sha)

        land_intent.mark = _die_at_committing
    elif case == "before-settle":
        real_settle = land_run._settle_token

        def _die_when_landed(token_state, *, landed, receipt=None):
            if landed:
                os._exit(_KILL_EXIT)
            real_settle(token_state, landed=landed, receipt=receipt)

        land_run._settle_token = _die_when_landed
    out = land_run._handler(msg["params"], repo_root=root / ".git")
    if case == "after-commit":
        os._exit(_KILL_EXIT)
    return {"jsonrpc": "2.0", "id": msg.get("id"), "result": out}


def _worker_entry(msg, caller, dispatch_key=None, claim_dir=None, stall_secs=10.0):
    server._pool_dispatch_worker_body = _land_body
    return server._pool_dispatch_worker(msg, caller, dispatch_key, claim_dir, stall_secs)


@pytest.fixture
def pool_ctx(tmp_path, monkeypatch, land_repo):  # noqa: F811 -- the imported fixture
    svc = tmp_path / "svc"
    claim_dir = svc / "dispatch-claims"
    claim_dir.mkdir(parents=True)
    journal = dispatch_ack.journal_dir_beside(claim_dir)
    monkeypatch.setenv(_REPO_ENV, str(land_repo))
    monkeypatch.setattr(server._ack_store, "_journal_dir", journal)
    monkeypatch.setattr(server, "_pool_dispatch_worker", _worker_entry)
    monkeypatch.setattr(server, "_POOL_RESULT_DEADLINE_SECS", _WAIT)
    inner = concurrent.futures.ProcessPoolExecutor(
        max_workers=1,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=server._worker_process_init,
        initargs=(str(svc / "pool-worker-stderr"),),
    )
    ctx = server._ServerContext.__new__(server._ServerContext)
    ctx._pool_outstanding = server.InFlightCounter()
    ctx._dispatch_pool = inner
    ctx._dispatch_pool_lock = threading.Lock()
    ctx._claim_dir = claim_dir
    ctx._worker_stderr_dir = svc / "pool-worker-stderr"
    yield ctx, land_repo
    inner.shutdown(wait=False, cancel_futures=True)


def _die_mid_land(pool_ctx, monkeypatch, case):
    ctx, root = pool_ctx
    monkeypatch.setenv(_CASE_ENV, case)
    token = run_token.mint(root, RUN, SCRIPT, session_id=SELF)
    key = dispatch_ack.mint_key()
    assert server._ack_store.admit(key, "dispatch.land_run")
    msg = {"jsonrpc": "2.0", "id": 7, "method": "dispatch.land_run", "params": {
        "script_path": SCRIPT, "incomplete_chunks": [], "inline_review": dict(reviewed()),
        "session_id": SELF, "run_id": RUN, "run_token": token,
    }}

    out = ctx._pool_dispatch(msg, dispatch_key=key)

    assert out["error"]["code"] == server.WARM_DISPATCH_INDETERMINATE, out
    assert out["error"]["data"]["diagnosis"]["exitcode"] == _KILL_EXIT
    assert server._ack_store._status_in_memory(key) == {
        "state": dispatch_ack.STATE_UNKNOWABLE, "reason": dispatch_ack.OUTCOME_WORKER_LOST,
    }
    return root, token, key


def test_the_default_accept_store_carries_the_land_resolver():
    assert server._ack_store._land_resolver is server._resolve_land_record


@pytest.mark.parametrize("case", ["after-commit", "before-settle"])
def test_a_worker_killed_after_the_commit_polls_committed_with_the_sha(pool_ctx, monkeypatch, case):
    root, token, key = _die_mid_land(pool_ctx, monkeypatch, case)

    head = _git(root, "rev-parse", "HEAD").strip()
    assert head != _git(root, "rev-parse", "HEAD~1").strip()
    if case == "before-settle":
        assert run_token.spent_receipt(root, RUN, token, SCRIPT) is None

    poll = server._store_poll({"key": key})

    assert poll["state"] == dispatch_ack.STATE_FINISHED
    assert poll["land_record"] == {"verdict": "committed", "sha": head}


def test_a_worker_killed_before_the_commit_polls_never_ran_and_a_rerun_lands(pool_ctx, monkeypatch):
    root, token, key = _die_mid_land(pool_ctx, monkeypatch, "before-commit")
    head = _git(root, "rev-parse", "HEAD").strip()

    poll = server._store_poll({"key": key})

    assert poll["state"] == dispatch_ack.STATE_NOT_RECEIVED
    assert poll["outcome"] == dispatch_ack.OUTCOME_NOT_DISPATCHED
    assert poll["land_record"] == {"verdict": "never-ran"}
    assert _git(root, "rev-parse", "HEAD").strip() == head

    rerun = _fire_land(root, token)  # the dead holder's claim is adopted

    assert rerun["committed"] is True, rerun
    assert rerun["sha"] == _git(root, "rev-parse", "HEAD").strip() != head


_GATE_STARTED = "gate-started"
_GATE_RELEASE = "gate-release"


def _stuck_or_gated(msg, caller, dispatch_key=None, claim_dir=None, stall_secs=10.0):
    """`hang` never answers; `gate` is a mutating stand-in that answers once the release file exists."""
    import time

    params = msg["params"]
    claim = server._claim_path(claim_dir, dispatch_key)
    if claim is not None:
        server._take_claim(claim, os.getpid())
    if params.get("hang"):
        time.sleep(120)
    gate = Path(params["gate_dir"])
    (gate / _GATE_STARTED).write_text("1", encoding="ascii")
    deadline = time.monotonic() + 60
    while not (gate / _GATE_RELEASE).exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"landed": True}}


def _gone(pid: int, secs: float = 10.0) -> bool:
    import time

    import psutil

    end = time.monotonic() + secs
    while time.monotonic() < end:
        if not psutil.pid_exists(pid):
            return True
        time.sleep(0.1)
    return False


def test_an_overdue_worker_kill_leaves_an_in_flight_mutating_neighbour_its_real_result(
    tmp_path, monkeypatch
):
    import time

    store = dispatch_ack.AckStore(boot_ns=0)
    monkeypatch.setattr(server, "_ack_store", store)
    claim_dir = tmp_path / "dispatch-claims"
    claim_dir.mkdir()
    gate = tmp_path / "gate"
    gate.mkdir()
    stderr_dir = tmp_path / "pool-worker-stderr"

    pool = concurrent.futures.ProcessPoolExecutor(
        max_workers=2,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=server._worker_process_init,
        initargs=(str(stderr_dir),),
    )
    ctx = server._ServerContext.__new__(server._ServerContext)
    ctx._pool_outstanding = server.InFlightCounter()
    ctx._dispatch_pool = pool
    ctx._dispatch_pool_lock = threading.Lock()
    ctx._claim_dir = claim_dir
    ctx._worker_stderr_dir = stderr_dir
    ctx._stall_dir = tmp_path / "stall"
    monkeypatch.setattr(server, "_pool_dispatch_worker", _stuck_or_gated)
    monkeypatch.setattr(server, "_POOL_RESULT_DEADLINE_SECS", 3.0)
    try:
        neighbour = pool.submit(
            _stuck_or_gated,
            {"method": "dispatch.land_run", "params": {"gate_dir": str(gate)}},
            None,
        )
        end = time.monotonic() + _WAIT
        while not (gate / _GATE_STARTED).exists() and time.monotonic() < end:
            time.sleep(0.05)
        assert (gate / _GATE_STARTED).exists()

        key = dispatch_ack.mint_key()
        assert store.admit(key, "dispatch.land_run")
        out = ctx._pool_dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "dispatch.land_run", "params": {"hang": True},
        }, dispatch_key=key)

        assert out["error"]["code"] == server.WARM_DISPATCH_INDETERMINATE
        assert ctx._dispatch_pool is None
        assert not neighbour.done()
        overdue_pid = int((claim_dir / key).read_text(encoding="ascii"))
        (gate / _GATE_RELEASE).write_text("1", encoding="ascii")

        assert neighbour.result(timeout=_WAIT)["result"] == {"landed": True}
        assert _gone(overdue_pid)
        assert store.status(key) == {
            "state": dispatch_ack.STATE_UNKNOWABLE,
            "reason": dispatch_ack.OUTCOME_KILLED,
            "worker_alive": False,
            "worker_pid": overdue_pid,
        }
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

# --- an overdue worker's kill spares healthy in-flight neighbours ---


def _overrun_hold(pid_file: str) -> None:
    """Module-level so the spawned worker can unpickle it."""
    Path(pid_file).write_text(str(os.getpid()), encoding="ascii")
    time.sleep(120)


def _mutate_hold(pid_file: str, secs: float) -> str:
    Path(pid_file).write_text(str(os.getpid()), encoding="ascii")
    time.sleep(secs)
    return "landed"


def _wait_pid_file(path: Path, timeout: float = 30.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text(encoding="ascii").strip():
            return int(path.read_text(encoding="ascii"))
        time.sleep(0.02)
    raise AssertionError(f"{path} never written")


def _two_pool_ops(tmp_path, neighbour_secs):
    pool = concurrent.futures.ProcessPoolExecutor(
        max_workers=2, mp_context=multiprocessing.get_context("spawn")
    )
    a_pid, b_pid = tmp_path / "a.pid", tmp_path / "b.pid"
    overdue = pool.submit(_overrun_hold, str(a_pid))
    pid_a = _wait_pid_file(a_pid)
    neighbour = pool.submit(_mutate_hold, str(b_pid), neighbour_secs)
    _wait_pid_file(b_pid)
    return pool, overdue, neighbour, pid_a


def _cleanup_pool(pool):
    for proc in list((getattr(pool, "_processes", None) or {}).values()):
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
    pool.shutdown(wait=False, cancel_futures=True)


def test_premise_a_bare_terminate_fails_the_neighbour(tmp_path):
    pool, _overdue, neighbour, pid_a = _two_pool_ops(tmp_path, 5.0)
    try:
        pool._processes[pid_a].terminate()
        with pytest.raises(BrokenProcessPool):
            neighbour.result(timeout=30)
    finally:
        _cleanup_pool(pool)


def test_reap_after_neighbours_lets_the_mutating_neighbour_finish_then_kills_the_overdue_worker(tmp_path):
    pool, overdue, neighbour, pid_a = _two_pool_ops(tmp_path, 1.5)
    try:
        proc = pool._processes[pid_a]
        neighbours = server._pool_neighbour_futures(pool, overdue)
        assert neighbours == [neighbour]
        server._reap_after_neighbours(pool, proc, neighbours)
        assert proc.is_alive()
        assert neighbour.result(timeout=30) == "landed"
        deadline = time.monotonic() + 10
        while psutil.pid_exists(pid_a) and proc.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not proc.is_alive()
    finally:
        _cleanup_pool(pool)


def test_reap_kills_at_once_when_there_are_no_neighbours(tmp_path):
    pool = concurrent.futures.ProcessPoolExecutor(
        max_workers=1, mp_context=multiprocessing.get_context("spawn")
    )
    try:
        a_pid = tmp_path / "a.pid"
        overdue = pool.submit(_overrun_hold, str(a_pid))
        pid_a = _wait_pid_file(a_pid)
        proc = pool._processes[pid_a]
        assert server._pool_neighbour_futures(pool, overdue) == []
        server._reap_after_neighbours(pool, proc, [])
        proc.join(timeout=10)
        assert not proc.is_alive()
    finally:
        _cleanup_pool(pool)


def test_a_neighbour_that_never_drains_is_reaped_at_the_grace_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_OVERDUE_REAP_GRACE_SECS", 0.5)
    pool, overdue, neighbour, pid_a = _two_pool_ops(tmp_path, 120.0)
    try:
        proc = pool._processes[pid_a]
        server._reap_after_neighbours(pool, proc, server._pool_neighbour_futures(pool, overdue))
        proc.join(timeout=15)
        assert not proc.is_alive()
    finally:
        _cleanup_pool(pool)


def test_terminate_overdue_worker_spares_the_neighbour_and_reaps_the_overdue_worker(tmp_path):
    pool, overdue, neighbour, pid_a = _two_pool_ops(tmp_path, 1.5)
    claim = tmp_path / "claim"
    claim.write_text(str(pid_a), encoding="ascii")
    proc = pool._processes[pid_a]

    class _Ctx:
        def _drop_pool(self, _pool):
            return True

    try:
        assert server._ServerContext._terminate_overdue_worker(_Ctx(), pool, claim, None, overdue)
        assert neighbour.result(timeout=30) == "landed"
        proc.join(timeout=15)
        assert not proc.is_alive()
    finally:
        _cleanup_pool(pool)
