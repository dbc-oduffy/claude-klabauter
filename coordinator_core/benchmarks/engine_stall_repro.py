"""Warm-engine stall probe: reproduces, in-process, each telemetry-path hold that can park an op's caller.

Three scenarios, each against a tmp directory this script creates and removes (never the live engine):

- ``concurrent-append``: threads append non-droppable lines to healthy sinks while a peer sink's writer is wedged.
  Hold site: ``atomic_append._registry_lock`` (taken by every append) and each caller's own ``job.done.wait``.
- ``rotation-all-sinks-peer-held``: every ``_KNOWN_SINK_NAMES`` sink is over threshold and its rename blocks.
  Hold site: ``log_rotation.rotate_if_needed`` ``worker.join(_JOIN_BOUND_SECS)`` once per sink, serially.
- ``stalled-sink-record-append``: a non-droppable ``append_line`` with the default wait (``RECORD_WAIT_SECS``) to a sink whose
  writer is blocked. Hold site: ``atomic_append.append_line`` ``job.done.wait(RECORD_WAIT_SECS)``.

``hold_ms`` is the caller's blocked time (``perf_counter``): a parked caller burns no CPU, so ``cpu_ms`` (process time)
stays near zero and cannot show the hold; ``spawns`` counts child processes. ``measure()`` is the entry the tests drive.

Run: ``python coordinator_core/benchmarks/engine_stall_repro.py [--json] [--guard-secs N] [--bar-ms N] [--enforce-bar]``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

_ENGINE_ROOT = Path(__file__).resolve().parents[2]
if str(_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_ENGINE_ROOT))

import pytest  # noqa: E402

from coordinator_core import atomic_append  # noqa: E402
from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed  # noqa: E402
from coordinator_core.telemetry import log_rotation  # noqa: E402

#: The per-scenario caller-hold bar (plan acceptance 1).
BAR_MS = 300.0
#: The stalled-sink scenario never waits past this, so the probe itself cannot hang.
GUARD_SECS = 5.0
#: Longest a blocked probe writer waits for release, so an abandoned thread always ends.
_GATE_MAX_SECS = 15.0
CONCURRENT_THREADS = 8
APPENDS_PER_THREAD = 5
SCENARIOS = ("concurrent-append", "rotation-all-sinks-peer-held", "stalled-sink-record-append")


def _wait_drained(pred, secs: float = 3.0) -> None:
    deadline = time.monotonic() + secs
    while pred() and time.monotonic() < deadline:
        time.sleep(0.01)


def _record(name: str, hold_ms: float, cpu_ms: float, spawns: int, hold_site: str, **extra) -> dict:
    return {
        "scenario": name,
        "hold_ms": round(hold_ms, 1),
        "cpu_ms": round(cpu_ms, 1),
        "spawns": spawns,
        "hold_site": hold_site,
        **extra,
    }


def _scenario_concurrent_append(tmp: Path, guard_secs: float) -> dict:
    gate = threading.Event()
    stalled_sink = tmp / "stalled-peer.jsonl"
    real_write_once = atomic_append._write_once

    def write_once(sink_str: str, encoded: bytes) -> None:
        if sink_str == str(stalled_sink):
            gate.wait(_GATE_MAX_SECS)
        real_write_once(sink_str, encoded)

    mp = pytest.MonkeyPatch()
    spawn_mp = pytest.MonkeyPatch()
    holds: list[float] = []
    holds_lock = threading.Lock()
    try:
        mp.setattr(atomic_append, "_write_once", write_once)
        peer = threading.Thread(
            target=atomic_append.append_line, args=(stalled_sink, b"x\n"), daemon=True, name="stall-peer",
        )
        peer.start()
        _wait_drained(lambda: str(stalled_sink) not in atomic_append.in_flight_sinks(), 1.0)
        barrier = threading.Barrier(CONCURRENT_THREADS)

        def worker(i: int) -> None:
            sink = tmp / f"healthy-{i}.jsonl"
            barrier.wait()
            for n in range(APPENDS_PER_THREAD):
                t0 = time.perf_counter()
                atomic_append.append_line(sink, b'{"i":%d,"n":%d}\n' % (i, n))
                dt = (time.perf_counter() - t0) * 1000.0
                with holds_lock:
                    holds.append(dt)

        with _count_spawns_attributed(spawn_mp) as spawns:
            cpu0 = time.process_time()
            threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(CONCURRENT_THREADS)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(guard_secs)
            cpu_ms = (time.process_time() - cpu0) * 1000.0
        completed = len(holds) == CONCURRENT_THREADS * APPENDS_PER_THREAD
        return _record(
            "concurrent-append", max(holds) if holds else guard_secs * 1000.0, cpu_ms, len(spawns),
            "atomic_append._registry_lock; per-caller job.done.wait",
            appends=len(holds), completed=completed, guard_hit=not completed,
        )
    finally:
        gate.set()
        mp.undo()
        spawn_mp.undo()
        _wait_drained(atomic_append.in_flight_sinks)


def _scenario_rotation_all_sinks(tmp: Path, guard_secs: float) -> dict:
    gate = threading.Event()
    real_replace = os.replace

    def replace(src, dst, *a, **k):
        if threading.current_thread().name == "log-rotation-chain":
            gate.wait(_GATE_MAX_SECS)
        return real_replace(src, dst, *a, **k)

    logs = tmp / "logs"
    logs.mkdir()
    for name in log_rotation._KNOWN_SINK_NAMES:
        (logs / name).write_bytes(b"x\n")
    mp = pytest.MonkeyPatch()
    spawn_mp = pytest.MonkeyPatch()
    try:
        mp.setattr(os, "replace", replace)
        with _count_spawns_attributed(spawn_mp) as spawns:
            cpu0 = time.process_time()
            t0 = time.perf_counter()
            rotated = log_rotation.rotate_all_known_sinks(logs, threshold_bytes=1)
            hold_ms = (time.perf_counter() - t0) * 1000.0
            cpu_ms = (time.process_time() - cpu0) * 1000.0
        return _record(
            "rotation-all-sinks-peer-held", hold_ms, cpu_ms, len(spawns),
            "log_rotation.rotate_if_needed worker.join(_JOIN_BOUND_SECS), once per sink in rotate_all_known_sinks",
            sinks=len(log_rotation._KNOWN_SINK_NAMES), rotated=rotated,
            join_bound_ms=log_rotation._JOIN_BOUND_SECS * 1000.0,
        )
    finally:
        gate.set()
        mp.undo()
        spawn_mp.undo()
        _wait_drained(lambda: bool(log_rotation._in_flight))
        with log_rotation._in_flight_lock:
            log_rotation._in_flight.clear()


def _scenario_stalled_sink(tmp: Path, guard_secs: float) -> dict:
    gate = threading.Event()
    sink = tmp / "stalled.jsonl"
    real_write_once = atomic_append._write_once

    def write_once(sink_str: str, encoded: bytes) -> None:
        if sink_str == str(sink):
            gate.wait(_GATE_MAX_SECS)
        real_write_once(sink_str, encoded)

    mp = pytest.MonkeyPatch()
    spawn_mp = pytest.MonkeyPatch()
    result: dict = {}
    try:
        mp.setattr(atomic_append, "_write_once", write_once)

        def caller() -> None:
            result["ret"] = atomic_append.append_line(sink, b'{"record":1}\n')

        with _count_spawns_attributed(spawn_mp) as spawns:
            cpu0 = time.process_time()
            t0 = time.perf_counter()
            th = threading.Thread(target=caller, daemon=True, name="stall-caller")
            th.start()
            th.join(guard_secs)
            hold_ms = (time.perf_counter() - t0) * 1000.0
            guard_hit = th.is_alive()
            cpu_ms = (time.process_time() - cpu0) * 1000.0
        gate.set()
        th.join(guard_secs)
        _wait_drained(atomic_append.in_flight_sinks)
        landed = sink.exists() and sink.read_bytes() == b'{"record":1}\n'
        return _record(
            "stalled-sink-record-append", hold_ms, cpu_ms, len(spawns),
            "atomic_append.append_line job.done.wait(RECORD_WAIT_SECS) (default bound)",
            guard_hit=guard_hit, guard_secs=guard_secs, line_landed_after_release=landed,
            returned=result.get("ret", "pending"),
        )
    finally:
        gate.set()
        mp.undo()
        spawn_mp.undo()


_RUNNERS = {
    "concurrent-append": _scenario_concurrent_append,
    "rotation-all-sinks-peer-held": _scenario_rotation_all_sinks,
    "stalled-sink-record-append": _scenario_stalled_sink,
}


def measure(guard_secs: float = GUARD_SECS, scenarios: "tuple[str, ...]" = SCENARIOS) -> "list[dict]":
    """Run each scenario in its own tmp dir; one record per scenario, in order. Never raises on a hold."""
    out: list[dict] = []
    for name in scenarios:
        tmp = Path(tempfile.mkdtemp(prefix="engine-stall-repro-"))
        try:
            out.append(_RUNNERS[name](tmp, guard_secs))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return out


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--json", action="store_true", help="emit the records as JSON")
    ap.add_argument("--guard-secs", type=float, default=GUARD_SECS)
    ap.add_argument("--bar-ms", type=float, default=BAR_MS)
    ap.add_argument("--enforce-bar", action="store_true", help="exit 1 when any scenario's hold exceeds --bar-ms")
    args = ap.parse_args(argv)
    from coordinator_core.benchmarks import declare_benchmark_origin

    declare_benchmark_origin()
    records = measure(args.guard_secs)
    over = [r["scenario"] for r in records if r["hold_ms"] > args.bar_ms]
    if args.json:
        print(json.dumps({"bar_ms": args.bar_ms, "over_bar": over, "records": records}, indent=2))
    else:
        for r in records:
            print(f"{r['scenario']}: hold {r['hold_ms']}ms cpu {r['cpu_ms']}ms spawns {r['spawns']} -- {r['hold_site']}")
    return 1 if (args.enforce_bar and over) else 0


if __name__ == "__main__":
    sys.exit(main())
