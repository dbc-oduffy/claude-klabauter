"""Falsifier: one worker held in engine-root IO must neither idle its peers nor cost the pool.

A REAL spawn-started `ProcessPoolExecutor` runs behind the production `_ServerContext._pool_dispatch`.
`latency_fs` parks exactly one worker inside an unbounded `open()` of an engine-root file (the shape of
the `client_cold_count` read in the stall dumps) on a multiprocessing Event. Waits are polls on state, never on elapsed
time. Peer ops must answer while the holder is parked; once the holder's own deadline passes, the pool
must keep the same executor and the same peer processes (no whole-pool swap) and `hang_restart` must
record no restart. Where the peers sit while idle is recorded from each worker's own stack dump.
"""

from __future__ import annotations

import concurrent.futures
import multiprocessing
import os
import threading
import time
from pathlib import Path

import psutil
import pytest

from coordinator_core.warm import dispatch_ack, hang_restart, server, telemetry
from coordinator_core.warm.tests import latency_fs

pytestmark = [pytest.mark.spawns_process]

_OP = "warm.latency_probe"
_WORKERS = 3
_PEERS = _WORKERS - 1
_HOLDER_DEADLINE_SECS = 6.0
_POLL_CAP_SECS = 60.0


class _VersionState:
    server_sha = "deadbeef"

    def is_skewed(self, client_token: str) -> bool:
        return False

    def is_token_skewed(self, client_token: str) -> bool:
        return False


def _body(msg, caller):
    params = msg["params"]
    if params.get("read_cold_count"):
        # An unbounded engine-root read, the shape of the `client_cold_count` open() in the stall dump;
        # raw so the held worker stays held whether or not telemetry bounds its own read.
        with open(telemetry.client_cold_path(Path(params["engine_root"])), "r", encoding="utf-8") as fh:
            fh.read(1)
    return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"pid": os.getpid()}}


def _worker(msg, caller, dispatch_key=None, claim_dir=None, stall_secs=10.0):
    server._pool_dispatch_worker_body = _body
    return server._pool_dispatch_worker(msg, caller, dispatch_key, claim_dir, stall_secs)


def _wait(predicate, secs: float = _POLL_CAP_SECS) -> bool:
    deadline = time.monotonic() + secs
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class _Rig:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
        self.engine_root = tmp_path / "engine"
        self.engine_root.mkdir()
        self.trap = telemetry.client_cold_path(self.engine_root)
        self.trap.parent.mkdir(parents=True, exist_ok=True)
        self.trap.write_text("{}\n", encoding="utf-8")
        self.mp = multiprocessing.get_context("spawn")
        self.spec = latency_fs.make_spec(self.mp, [self.trap], _WORKERS)
        self.stderr_dir = tmp_path / "pool-worker-stderr"
        self.stall_dir = tmp_path / "stall-dumps"
        self.claim_dir = tmp_path / "dispatch-claims"
        for directory in (self.stall_dir, self.claim_dir):
            directory.mkdir()
        self.built: list = []
        monkeypatch.setattr(server, "_pool_dispatch_worker", _worker)
        monkeypatch.setattr(server, "_withdrawal_deadline_secs", lambda *_a, **_k: _HOLDER_DEADLINE_SECS)
        monkeypatch.setattr(server, "DISPATCH_PROCESS_POOL_SIZE", _WORKERS)
        ctx = server._ServerContext(
            name="pipe-latency", sid="sid-latency", version_state=_VersionState(), engine_root=self.engine_root
        )
        ctx._claim_dir = self.claim_dir
        ctx._stall_dir = self.stall_dir
        ctx._worker_stderr_dir = self.stderr_dir
        ctx._ensure_dispatch_pool = self._ensure_pool
        self.ctx = ctx

    def _build(self):
        pool = concurrent.futures.ProcessPoolExecutor(
            max_workers=_WORKERS,
            mp_context=self.mp,
            initializer=latency_fs.worker_init,
            initargs=(self.spec, str(self.stderr_dir), str(self.stall_dir)),
        )
        server._register_stack_pool(pool)
        self.built.append(pool)
        return pool

    def _ensure_pool(self):
        with self.ctx._dispatch_pool_lock:
            if self.ctx._dispatch_pool is None:
                self.ctx._dispatch_pool = self._build()
            return self.ctx._dispatch_pool

    def dispatch(self, *, read_cold_count: bool, out: list) -> threading.Thread:
        key = dispatch_ack.mint_key()
        assert server._ack_store.admit(key, _OP)
        msg = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": _OP,
            "params": {"read_cold_count": read_cold_count, "engine_root": str(self.engine_root)},
        }
        thread = threading.Thread(
            target=lambda: out.append(self.ctx._pool_dispatch(msg, dispatch_key=key)), daemon=True
        )
        thread.start()
        return thread

    def peer_batch(self, count: int) -> list:
        outs = [[] for _ in range(count)]
        threads = [self.dispatch(read_cold_count=False, out=o) for o in outs]
        assert _wait(lambda: all(outs)), "a peer op never answered while one worker was held in engine-root IO"
        for thread in threads:
            thread.join(10.0)
        return [o[0] for o in outs]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    baseline = {c.pid for c in psutil.Process().children(recursive=True)}
    harness = _Rig(tmp_path, monkeypatch)
    try:
        yield harness
    finally:
        harness.spec.release.set()
        for pool in harness.built:
            pool.shutdown(wait=False, cancel_futures=True)
        for child in psutil.Process().children(recursive=True):
            if child.pid not in baseline:
                try:
                    child.kill()
                except psutil.Error:
                    pass


def _stack_of(stacks: str, pid: int) -> str:
    return "".join(b for b in stacks.split("worker-pid: ")[1:] if b.startswith(f"{pid}\n"))


def test_one_worker_held_in_engine_root_io_leaves_peers_serving_and_the_pool_intact(rig, tmp_path):
    ctx = rig.ctx
    pool = ctx._ensure_dispatch_pool()
    worker_pids = latency_fs.warm_pool(pool, _WORKERS)
    assert len(worker_pids) == _WORKERS

    victim: list = []
    with latency_fs.hold_engine_root_io(rig.spec):
        victim_thread = rig.dispatch(read_cold_count=True, out=victim)
        assert rig.spec.entered.wait(_POLL_CAP_SECS), "no worker reached the engine-root read"
        holder = rig.spec.holder.value
        peer_pids = worker_pids - {holder}
        assert holder in worker_pids and len(peer_pids) == _PEERS

        # Mechanism record: every worker's own stack while one is held (peers' idle frame, holder's park).
        stacks = server._collect_worker_stacks(rig.stall_dir)
        (tmp_path / "worker-stacks.txt").write_text(stacks, encoding="utf-8")
        print(f"MECHANISM holder={holder} peers={sorted(peer_pids)}\n{stacks}")
        assert "latency_fs" in _stack_of(stacks, holder), "the holder is not parked in the latency shim"

        # Peers answer through the default dispatch path while the holder is still parked.
        answered = rig.peer_batch(_WORKERS + 1)
        assert all("result" in out for out in answered), answered
        assert {out["result"]["pid"] for out in answered} <= peer_pids
        assert not victim, "the held op answered before its deadline"

        # The holder's own deadline passes: its caller is answered, still without waking the held IO.
        assert _wait(lambda: bool(victim)), "the held op's caller was never answered"
        victim_thread.join(10.0)
        assert not rig.spec.release.is_set()

        # One worker's loss is not the pool's: same executor, same peer processes, still serving.
        assert len(rig.built) == 1, f"the pool was rebuilt {len(rig.built) - 1} time(s) for one held worker"
        assert ctx._dispatch_pool is pool
        assert not getattr(pool, "_broken", False), "the held worker's retirement broke the whole executor"
        assert all(psutil.pid_exists(p) for p in peer_pids), "a peer worker was killed with the holder"
        after = rig.peer_batch(_WORKERS)
        assert all("result" in out for out in after), after
        assert {out["result"]["pid"] for out in after} <= peer_pids

        restarts: list = []
        hang_restart.maybe_restart(rig.stall_dir, threshold=1, exit_fn=restarts.append)
        assert restarts == [], "hang_restart restarted the engine for one held worker"
