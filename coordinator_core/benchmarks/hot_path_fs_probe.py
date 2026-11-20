"""Warm-engine hot-path filesystem probe: per-request FS work on the calling thread, and pool isolation from one blocked FS call.

Two legs, each in a tmp dir this script creates and removes (never the live engine):

- Calling-thread leg: drives ``ENVELOPES`` ``ipc.resolve_request_repo`` envelopes over ``DISTINCT_ROOTS`` roots plus one
  ``_advisory_dedupe._rotate_known_logs``, counting the ``os.stat`` / ``Path.resolve`` / ``os.path.realpath`` calls made on the
  calling thread. ``inline_rotation_stats`` is the ``os.stat`` calls on a ``_KNOWN_SINK_NAMES`` sink;
  ``realpath_calls`` is ``Path.resolve`` calls plus bare ``os.path.realpath`` calls (a realpath inside a resolve counts once).
- Pool leg: builds the front-thread pool ``warm.server._ServerContext`` builds, holds ``HELD_WORKERS`` worker(s) in a patched
  ``os.stat`` that waits on an Event, submits ``PEER_OPS`` peer ops and counts the answered ones while the hold persists.

The isolation spike re-runs the pool leg with every worker held, once with the blocked call bare and once with it under
``bounded_io.run_bounded``; ``isolation_spike`` records both outcomes and the verdict.

JSON keys are the contract of the after-measurement: ``inline_rotation_stats``, ``realpath_calls``, ``distinct_roots``,
``process_time_s`` (calling-thread leg, process-wide CPU), ``spawns`` (both legs), ``peer_ops_submitted``,
``peer_ops_completed_while_blocked``. Budgets are process time and spawn count, never wall clock; the only waits are
bounded observation windows.

Run: ``python coordinator_core/benchmarks/hot_path_fs_probe.py [--json] [--peer-ops N] [--pool-size N] [--spike-secs S]``.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import threading
import time
from pathlib import Path

_ENGINE_ROOT = Path(__file__).resolve().parents[2]
if str(_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_ENGINE_ROOT))

import pytest  # noqa: E402

from coordinator_core.benchmarks.engine_stall_repro import (  # noqa: E402
    _GuardFakeIO,
    _GuardVersionState,
    _wait_until,
)
from coordinator_core.benchmarks.isolated_clone import rmtree_or_raise  # noqa: E402
from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed  # noqa: E402
from coordinator_core.temp_layout import make_managed_temp_dir  # noqa: E402

ENVELOPES = 200
DISTINCT_ROOTS = 3
HELD_WORKERS = 1
PEER_OPS = 8
#: Small front-thread pool: one held worker leaves healthy ones, and the saturated spike needs few holders.
POOL_SIZE = 4
#: Bounded observation window for "did the peer answer"; never asserted as a duration.
SPIKE_SECS = 1.0
#: The run_bounded deadline the bounded spike variant gives each blocked call.
BOUNDED_DEADLINE_SECS = 0.2
#: Longest a blocked probe call waits for release, so an abandoned thread always ends.
_GATE_MAX_SECS = 15.0
_OP = "dispatch.land_run"
_PEER_ID_BASE = 10_000


class _Counters:
    """Calling-thread FS call counts; a call on any other thread is ignored."""

    def __init__(self, thread_ident: int) -> None:
        self.thread_ident = thread_ident
        self.stat_calls = 0
        self.sink_stats = 0
        self.resolve_calls = 0
        self.bare_realpath_calls = 0
        self._local = threading.local()

    def mine(self) -> bool:
        return threading.get_ident() == self.thread_ident


def _install_counters(mp: pytest.MonkeyPatch, c: _Counters, sink_names: "frozenset[str]") -> None:
    real_stat = os.stat
    real_resolve = pathlib.Path.resolve
    real_realpath = os.path.realpath

    def stat(path, *a, **k) -> object:
        if c.mine():
            c.stat_calls += 1
            try:
                if os.path.basename(os.fspath(path)) in sink_names:
                    c.sink_stats += 1
            except TypeError:
                pass
        return real_stat(path, *a, **k)

    def resolve(self, *a, **k) -> object:
        if not c.mine():
            return real_resolve(self, *a, **k)
        c.resolve_calls += 1
        c._local.in_resolve = getattr(c._local, "in_resolve", 0) + 1
        try:
            return real_resolve(self, *a, **k)
        finally:
            c._local.in_resolve -= 1

    def realpath(path, *a, **k) -> object:
        if c.mine() and not getattr(c._local, "in_resolve", 0):
            c.bare_realpath_calls += 1
        return real_realpath(path, *a, **k)

    mp.setattr(os, "stat", stat)
    mp.setattr(pathlib.Path, "resolve", resolve)
    mp.setattr(os.path, "realpath", realpath)


def _join_detached_rotation(secs: float = 3.0) -> None:
    deadline = time.monotonic() + secs
    for t in threading.enumerate():
        if t.name == "log-rotation":
            t.join(max(0.0, deadline - time.monotonic()))


def _calling_thread_leg(tmp: Path, envelopes: int) -> dict:
    from coordinator_core import ipc
    from coordinator_core.bash_guards import _advisory_dedupe
    from coordinator_core.telemetry import log_rotation

    roots = []
    for i in range(DISTINCT_ROOTS):
        root = tmp / f"root-{i}"
        root.mkdir()
        roots.append(str(root))
    logs = tmp / "hub" / "logs"
    logs.mkdir(parents=True)
    for name in log_rotation._KNOWN_SINK_NAMES:
        (logs / name).write_bytes(b"x\n")
    msgs = [{ipc._ORIGIN_WORKTREE_FIELD: roots[i % DISTINCT_ROOTS]} for i in range(envelopes)]

    counters = _Counters(threading.get_ident())
    mp = pytest.MonkeyPatch()
    spawn_mp = pytest.MonkeyPatch()
    try:
        mp.setattr(_advisory_dedupe, "hub_for_common", lambda _gitdir: tmp / "hub")
        mp.setattr(log_rotation, "_last_checked", {})
        _install_counters(mp, counters, frozenset(log_rotation._KNOWN_SINK_NAMES))
        with _count_spawns_attributed(spawn_mp) as spawns:
            cpu0 = time.process_time()
            for msg in msgs:
                ipc.resolve_request_repo(msg)
            _advisory_dedupe._rotate_known_logs(tmp / "gitdir")
            _join_detached_rotation()
            process_time_s = time.process_time() - cpu0
    finally:
        mp.undo()
        spawn_mp.undo()
    return {
        "envelopes": envelopes,
        "distinct_roots": DISTINCT_ROOTS,
        "inline_rotation_stats": counters.sink_stats,
        "realpath_calls": counters.resolve_calls + counters.bare_realpath_calls,
        "path_resolve_calls": counters.resolve_calls,
        "bare_realpath_calls": counters.bare_realpath_calls,
        "stat_calls": counters.stat_calls,
        "process_time_s": round(process_time_s, 4),
        "spawns": len(spawns),
    }


def _pool_leg(tmp: Path, *, pool_size: int, held: int, peers: int, bounded: bool, spike_secs: float) -> dict:
    """Build the server's front-thread pool, hold ``held`` workers in a blocked ``os.stat``, submit ``peers`` ops."""
    import concurrent.futures

    from coordinator_core.bounded_io import run_bounded
    from coordinator_core.warm import server, telemetry

    blocked_target = str(tmp / "blocked-target")
    release = threading.Event()
    real_stat = os.stat

    def stat(path, *a, **k) -> object:
        try:
            if os.fspath(path) == blocked_target:
                release.wait(_GATE_MAX_SECS)
                return real_stat(tmp)
        except TypeError:
            pass
        return real_stat(path, *a, **k)

    def body(msg, caller) -> dict:
        if msg.get("id", 0) < _PEER_ID_BASE:
            if bounded:
                run_bounded(
                    f"hot-path-fs-probe:{msg['id']}", lambda: os.stat(blocked_target),
                    deadline=BOUNDED_DEADLINE_SECS, default=None,
                )
            else:
                os.stat(blocked_target)
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"ok": msg["method"]}}

    mp = pytest.MonkeyPatch()
    spawn_mp = pytest.MonkeyPatch()
    ctx = None
    try:
        mp.setattr(server, "_pool_dispatch_worker_body", body)
        mp.setattr(telemetry, "flush_requests", lambda *_a, **_k: None)
        telemetry._REQUESTS.clear()
        ctx = server._ServerContext(
            name="pipe-hot-path-fs-probe", sid="sid-hot-path-fs-probe", version_state=_GuardVersionState(), engine_root=tmp  # type: ignore[arg-type]
        )
        ctx._claim_dir = tmp / "claims"
        ctx._claim_dir.mkdir()
        ctx._dispatch_pool = concurrent.futures.ThreadPoolExecutor(  # type: ignore[assignment]
            max_workers=pool_size + server._LIGHT_RESERVE_WORKERS
        )
        ctx._start_worker_pool(pool_size=pool_size)
        mp.setattr(os, "stat", stat)
        holders = [_GuardFakeIO(_OP, i) for i in range(held)]
        peer_ios = [_GuardFakeIO(_OP, _PEER_ID_BASE + i) for i in range(peers)]
        with _count_spawns_attributed(spawn_mp) as spawns:
            cpu0 = time.process_time()
            for io in holders:
                ctx._enqueue_connection(io, admitted=False)
            if not bounded:
                held_ok = _wait_until(lambda: ctx._busy_workers() == held, 5.0)
            else:
                held_ok = True
            for io in peer_ios:
                ctx._enqueue_connection(io, admitted=False)
            _wait_until(lambda: all(io.written for io in peer_ios), spike_secs)
            busy_after = ctx._busy_workers()
            completed = sum(1 for io in peer_ios if io.written)
            cpu_s = time.process_time() - cpu0
        return {
            "pool_size": pool_size,
            "held": held,
            "held_confirmed": held_ok,
            "peer_ops_submitted": peers,
            "peer_ops_completed_while_blocked": completed,
            "busy_workers_at_end": busy_after,
            "process_time_s": round(cpu_s, 4),
            "spawns": len(spawns),
        }
    finally:
        release.set()
        if ctx is not None and getattr(ctx, "_dispatch_pool", None) is not None:
            ctx._dispatch_pool.shutdown(wait=True, cancel_futures=True)
        mp.undo()
        spawn_mp.undo()


def _isolation_spike(tmp: Path, pool_size: int, spike_secs: float) -> dict:
    """Every worker held, one peer: does it answer with the blocked call bare, and under run_bounded."""
    bare = _pool_leg(tmp / "spike-bare", pool_size=pool_size, held=pool_size, peers=1, bounded=False, spike_secs=spike_secs)
    bounded = _pool_leg(tmp / "spike-bounded", pool_size=pool_size, held=pool_size, peers=1, bounded=True, spike_secs=spike_secs)
    bare_ok = bare["peer_ops_completed_while_blocked"] == bare["peer_ops_submitted"]
    bounded_ok = bounded["peer_ops_completed_while_blocked"] == bounded["peer_ops_submitted"]
    if bounded_ok and not bare_ok:
        verdict = "run_bounded: holds; slot eviction: not present at HEAD (bare saturated pool starves the peer)"
    elif bare_ok:
        verdict = "bare saturated pool already serves the peer; no isolation gap shown for this op"
    else:
        verdict = "neither run_bounded nor existing admission frees the pool"
    return {
        "mechanism_run_bounded_on_request_thread_fs_call": "holds" if bounded_ok else "does_not_hold",
        "bare_saturated_peer_completed": bare_ok,
        "run_bounded_saturated_peer_completed": bounded_ok,
        "verdict": verdict,
        "bare": bare,
        "bounded": bounded,
    }


def measure(
    *,
    envelopes: int = ENVELOPES,
    peer_ops: int = PEER_OPS,
    pool_size: int = POOL_SIZE,
    spike_secs: float = SPIKE_SECS,
) -> dict:
    """Run both legs and the isolation spike in one tmp dir; one record. Never raises on a hold."""
    from coordinator_core.benchmarks import fixture_settings_home

    tmp = make_managed_temp_dir(prefix="hot-path-fs-probe-")
    settings_scope = fixture_settings_home(tmp)
    settings_scope.start()
    try:
        for sub in ("calling", "held", "spike-bare", "spike-bounded"):
            (tmp / sub).mkdir()
        calling = _calling_thread_leg(tmp / "calling", envelopes)
        pool = _pool_leg(
            tmp / "held", pool_size=pool_size, held=HELD_WORKERS, peers=peer_ops, bounded=False, spike_secs=spike_secs
        )
        spike = _isolation_spike(tmp, pool_size, spike_secs)
    finally:
        settings_scope.stop()
        rmtree_or_raise(tmp, label="hot-path-fs-probe")
    return {
        "inline_rotation_stats": calling["inline_rotation_stats"],
        "realpath_calls": calling["realpath_calls"],
        "distinct_roots": calling["distinct_roots"],
        "process_time_s": calling["process_time_s"],
        "spawns": calling["spawns"] + pool["spawns"],
        "peer_ops_submitted": pool["peer_ops_submitted"],
        "peer_ops_completed_while_blocked": pool["peer_ops_completed_while_blocked"],
        "calling_thread": calling,
        "pool": pool,
        "isolation_spike": spike,
    }


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--json", action="store_true", help="emit the record as JSON")
    ap.add_argument("--envelopes", type=int, default=ENVELOPES)
    ap.add_argument("--peer-ops", type=int, default=PEER_OPS)
    ap.add_argument("--pool-size", type=int, default=POOL_SIZE)
    ap.add_argument("--spike-secs", type=float, default=SPIKE_SECS)
    args = ap.parse_args(argv)
    from coordinator_core.benchmarks import declare_benchmark_origin

    declare_benchmark_origin()
    record = measure(
        envelopes=args.envelopes, peer_ops=args.peer_ops, pool_size=args.pool_size, spike_secs=args.spike_secs
    )
    if args.json:
        print(json.dumps(record, indent=2))
    else:
        print(
            f"inline_rotation_stats {record['inline_rotation_stats']} realpath_calls {record['realpath_calls']} "
            f"(distinct_roots {record['distinct_roots']}) process_time_s {record['process_time_s']} "
            f"spawns {record['spawns']} peers {record['peer_ops_completed_while_blocked']}/{record['peer_ops_submitted']}"
        )
        print(f"isolation spike: {record['isolation_spike']['verdict']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
