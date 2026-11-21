"""Measure the fleet-capacity ops against the brightline: median process time and child spawns per op.

Dispatches each op in-process N times (default 20) through `ipc.get_op_handler`, the engine's registry lookup,
and reports per op the median `time.process_time` of a call (`process_ms`), the child-spawn count of a call
(`spawns`: `subprocess.Popen` constructions, median and max over the calls) and the psutil children delta taken
around each call (`children_delta_max`). `groupem.box_roster` is the baseline; an op the registry does not
resolve reports `op-not-registered`; an op whose every call returns an error reports `errored`
(`groupem.capacity_admit` is measured by `probe_admit` against a tmp settings home, a grant or named refusal).
`process_time` is this process only: a child's CPU is not in `process_ms`, which is what `spawns` is for.
On Windows `process_time` ticks at ~15.6ms, so the median can read 0.0: `process_ms_mean` is the figure to read.
Runs as `python coordinator_core/benchmarks/fleet_capacity_probe.py [--json] [--n N]`; always exits 0.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

if __package__ in (None, ""):
    sys.path.insert(0, os.fspath(Path(__file__).resolve().parents[2]))

BASELINE_OP = "groupem.box_roster"
NEW_OPS = (
    "groupem.fleet_capacity",
    "groupem.capacity_admit",
    "groupem.capacity_place",
    "groupem.idle_capacity_watch",
)
# Valid, read-only params so the op's real path is measured; `capacity_admit` writes ledgers, so
# `probe_admit` runs it against a tmp settings home instead.
READ_ONLY_PARAMS = {"groupem.capacity_place": {"job_type": "fleet-capacity-probe"}}
STAMP_MODULE = "coordinator_core.group_em.capacity_record"
_REAL_POPEN = subprocess.Popen


class _Spawns:
    count = 0


class _CountingPopen(_REAL_POPEN):
    """Probe-only `subprocess.Popen` that counts constructions."""

    def __init__(self, *args, **kwargs) -> None:
        _Spawns.count += 1
        super().__init__(*args, **kwargs)


def _children_now() -> int:
    try:
        import psutil

        return len(psutil.Process().children(recursive=True))
    except Exception:  # noqa: BLE001 -- psutil absent or process gone: no figure
        return 0


def measure(call: Callable[[], object], n: int) -> dict:
    """Run `call` n times; return median process_ms, median/max Popen spawns, max psutil children delta."""
    cpu_ms: list[float] = []
    spawns: list[int] = []
    deltas: list[int] = []
    errors = 0
    setattr(subprocess, "Popen", _CountingPopen)
    try:
        for _ in range(n):
            before_children, before_spawns = _children_now(), _Spawns.count
            t0 = time.process_time()
            try:
                result = call()
                if isinstance(result, dict) and "error" in result:
                    errors += 1
            except Exception:  # noqa: BLE001 -- a raising op is still measured
                errors += 1
            cpu_ms.append((time.process_time() - t0) * 1000.0)
            spawns.append(_Spawns.count - before_spawns)
            deltas.append(_children_now() - before_children)
    finally:
        setattr(subprocess, "Popen", _REAL_POPEN)
    return {
        "n": n,
        "process_ms": round(statistics.median(cpu_ms), 2),
        "process_ms_mean": round(sum(cpu_ms) / len(cpu_ms), 2),
        "process_ms_max": round(max(cpu_ms), 2),
        "spawns": int(statistics.median(spawns)),
        "spawns_max": max(spawns),
        "children_delta_max": max(deltas),
        "error_results": errors,
    }


def _resolve(op: str) -> Optional[Callable]:
    from coordinator_core import ipc

    try:
        return ipc.get_op_handler(op)
    except Exception:  # noqa: BLE001 -- a suspended/killed op reads as not served
        return None


def probe_op(op: str, n: int) -> dict:
    handler = _resolve(op)
    if handler is None:
        return {"op": op, "status": "op-not-registered"}
    params = READ_ONLY_PARAMS.get(op, {})
    row = {"op": op, "status": "measured", **measure(lambda: handler(dict(params)), n)}
    if row["error_results"] == n:
        row["status"] = "errored"
    return row


def probe_admit(n: int) -> dict:
    """Measure `groupem.capacity_admit` against a tmp settings home: a stamped 32768/6144 MiB sample, this
    home registered under a different box id, and a measured 100 MiB vram peak. Every reply is a grant or a
    named refusal; `replies` counts each. Grant and wait ledgers land under the tmp home only."""
    op = "groupem.capacity_admit"
    handler = _resolve(op)
    if handler is None:
        return {"op": op, "status": "op-not-registered"}
    import tempfile

    from coordinator_core.group_em import box_registry, capacity_record, job_peaks

    sample = {
        "os": sys.platform, "arch": "x86_64", "cores_physical": 16, "cores_logical": 24, "load_pct": 5.0,
        "ram_total_mb": 131072, "ram_avail_mb": 50000, "jobs": [],
        "gpus": [{"index": 0, "name": "probe", "vram_total_mib": 32768, "vram_used_mib": 6144, "util_pct": 1.0}],
    }
    saved = os.environ.get("COORDINATOR_SETTINGS_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        os.environ["COORDINATOR_SETTINGS_HOME"] = tmp
        try:
            box_registry.ensure_identity(home)
            box_registry.register(home, {}, box_id="probe-registered-id")
            if not capacity_record.stamp(sample, home):
                return {"op": op, "status": "errored", "reason": "stamp-failed"}
            job_peaks.record("fleet-capacity-probe", {"vram_mib": 100}, home)
            replies = {"grant": 0, "refused": 0, "error": 0}

            def call() -> dict:
                out = handler({"job_type": "fleet-capacity-probe"})
                key = "error" if "error" in out else ("grant" if out.get("grant") else "refused")
                replies[key] += 1
                return out

            row = {"op": op, "status": "measured", **measure(call, n), "replies": replies}
        finally:
            if saved is None:
                os.environ.pop("COORDINATOR_SETTINGS_HOME", None)
            else:
                os.environ["COORDINATOR_SETTINGS_HOME"] = saved
    if row["error_results"] == n:
        row["status"] = "errored"
    return row


def probe_stamp(n: int) -> dict:
    name = "capacity_record.stamp"
    try:
        module = importlib.import_module(STAMP_MODULE)
        stamp = getattr(module, "stamp")
    except (ImportError, AttributeError):
        return {"op": name, "status": "not-importable"}
    import tempfile

    with tempfile.TemporaryDirectory() as home:
        return {"op": name, "status": "measured", **measure(lambda: {} if stamp({}, Path(home)) else {"error": "stamp"}, n)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="print one JSON line instead of a table")
    parser.add_argument("--n", type=int, default=20, help="calls per op (default 20)")
    args = parser.parse_args(argv)
    if args.n < 1:
        parser.error("--n must be >= 1")
    rows = [probe_admit(args.n) if op == "groupem.capacity_admit" else probe_op(op, args.n) for op in (BASELINE_OP, *NEW_OPS)]
    rows.append(probe_stamp(args.n))
    if args.json:
        print(json.dumps({"probe": "fleet_capacity", "n": args.n, "platform": sys.platform, "ops": rows}), flush=True)
        return 0
    for row in rows:
        if row["status"] != "measured":
            print(f"{row['op']}: {row['status']}")
        else:
            print(f"{row['op']}: process_ms={row['process_ms']} spawns={row['spawns']} "
                  f"(max {row['spawns_max']}, children_delta_max {row['children_delta_max']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
