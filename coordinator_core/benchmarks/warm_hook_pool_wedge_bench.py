"""Hook op after a guard-pool wedge: server process time, pool-result wait and spawn count per op.

Drives the real `_ServerContext._pool_dispatch` over real `ProcessPoolExecutor` guard and heavy pools
(the rig of `hook_lane_full_pool`). Phase `steady` answers SendMessage-hook probes on a warm guard
pool; phase `wedged` parks every guard worker in a kernel file-handle wait until each is overdue
(every hook lane worker stuck), then answers the same probes while any replacement guard pool boots
workers that each pay `--boot-secs` of extra init (a model of the 4-8 s a loaded box charged a
`ping` right after a recycle; it applies only to workers started after the wedge). Reported per
phase: probe outcomes, accept-process ms, worker process ms, spawns (accept `Popen` plus worker),
`_await_pool_result` wait ms and recycle count. Wall-clock reply ms is printed for context; the
verdict reads outcomes, process time and pool-wait, never wall clock (DR-344).

    python -m coordinator_core.benchmarks.warm_hook_pool_wedge_bench [--label base|end] [--probes 8]
        [--boot-secs 5.0] [--out PATH] [--json] [--strict]

TRAP: the probe is the direct `hooks.rewrite_sendmessage_peer_name` frame: the scratch engine root
has no `hook-run` entrypoint, so the `invoke.from_argv` wrapper (whose `_hook_originated_budget_secs`
clamp only changes the reply shape) is not reproduced here.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

from coordinator_core.benchmarks import hook_lane_full_pool as _lane

BOOT_SECS_ENV = "WARM_WEDGE_BENCH_BOOT_SECS"
LOAD_MARKER = "load-started"
HOLD_FLAG = "bench_hold"
SEND_OP = "hooks.rewrite_sendmessage_peer_name"
PROBE_PROCESS_BAR_MS = 500.0
_WEDGE_WAIT_SECS = 30.0


def _wedge_worker_init(stderr_dir=None, stall_dir=None, forward_queue=None, prewarm_emit=False) -> None:
    """Pool-worker initializer: the engine's own init, the boot delay once the load marker exists, then a body
    wrapper that parks a `HOLD_FLAG` frame and stamps each op's worker CPU ms and spawn count into its reply."""
    from coordinator_core.warm import server

    server._worker_process_init(stderr_dir, stall_dir, forward_queue, prewarm_emit)
    from coordinator_core import ipc

    ipc.allow_unstamped_dispatch()
    bench_dir = Path(os.environ[_lane.BENCH_DIR_ENV])
    if (bench_dir / LOAD_MARKER).exists():
        time.sleep(float(os.environ.get(BOOT_SECS_ENV, "0")))
    real_body = server._pool_dispatch_worker_body

    def body(msg: Any, caller: Any) -> Any:
        if isinstance(msg, dict) and (msg.get("params") or {}).get(HOLD_FLAG):
            _lane._hold_in_file_io(bench_dir)
            return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"held": True}}
        spawns0 = server._spawn_count_or_none()
        cpu0 = time.process_time()
        out = real_body(msg, caller)
        cpu_ms = (time.process_time() - cpu0) * 1000.0
        spawns = server._spawn_delta(spawns0, server._spawn_count_or_none())
        return {**out, "_bench": {"cpu_ms": cpu_ms, "spawns": spawns}} if isinstance(out, dict) else out

    setattr(server, "_pool_dispatch_worker_body", body)


class _PoolWaitMeter:
    """Sums the seconds `_await_pool_result` blocked, per call, while active."""

    def __init__(self, server: Any) -> None:
        self.server = server
        self.waits_ms: list = []
        self._orig: Any = None

    def __enter__(self) -> "_PoolWaitMeter":
        meter = self
        self._orig = self.server._await_pool_result

        def timed(*args: Any, **kwargs: Any) -> Any:
            t0 = time.perf_counter()
            try:
                return meter._orig(*args, **kwargs)
            finally:
                meter.waits_ms.append((time.perf_counter() - t0) * 1000.0)

        setattr(self.server, "_await_pool_result", timed)
        return self

    def __exit__(self, *_exc: Any) -> None:
        setattr(self.server, "_await_pool_result", self._orig)


def _probe_message(rig: Any, request_id: int) -> dict:
    repo = str(rig.repo)
    payload = {
        "cwd": repo,
        "session_id": "s-wedge-bench",
        "tool_name": "SendMessage",
        "tool_input": {"to": "peer", "message": "hello"},
    }
    return {"jsonrpc": "2.0", "id": request_id, "method": SEND_OP, "params": {"payload": payload}}


def _probe_outcome(resp: Any) -> str:
    base = _lane._outcome(resp)
    if base != "ok":
        return base
    result = resp.get("result") if isinstance(resp, dict) else None
    if isinstance(result, dict) and "hook-timeout" in str(result.get("stderr", "")):
        return "hook-timeout"
    return "ok"


def run_probes(rig: Any, calls: int, first_id: int) -> dict:
    """`calls` sequential SendMessage probes through `_pool_dispatch`; per-call outcome and process figures."""
    server = rig.server
    rows: list = []
    recycles_before = rig.recycle_count()
    with _lane._SpawnCounter() as accept_spawns, _PoolWaitMeter(server) as meter:
        for index in range(calls):
            cpu0 = time.process_time()
            t0 = time.perf_counter()
            resp = rig.ctx._pool_dispatch(_probe_message(rig, first_id + index))
            wall_ms = (time.perf_counter() - t0) * 1000.0
            accept_ms = (time.process_time() - cpu0) * 1000.0
            bench = resp.get("_bench") if isinstance(resp, dict) else None
            rows.append(
                {
                    "outcome": _probe_outcome(resp),
                    "wall_ms": wall_ms,
                    "accept_ms": accept_ms,
                    "worker_ms": float(bench["cpu_ms"]) if isinstance(bench, dict) else None,
                    "worker_spawns": int(bench.get("spawns") or 0) if isinstance(bench, dict) else 0,
                }
            )
    outcomes: dict = {}
    for row in rows:
        outcomes[row["outcome"]] = outcomes.get(row["outcome"], 0) + 1
    process_ms = [row["accept_ms"] + (row["worker_ms"] or 0.0) for row in rows]
    return {
        "calls": calls,
        "outcomes": outcomes,
        "p95_process_ms": _lane._r(_lane.percentile(process_ms, 0.95)),
        "median_process_ms": _lane._r(statistics.median(process_ms) if process_ms else None),
        "max_pool_result_wait_ms": _lane._r(max(meter.waits_ms, default=None)),
        "p95_pool_result_wait_ms": _lane._r(_lane.percentile(meter.waits_ms, 0.95)),
        "spawns_accept": accept_spawns.count,
        "spawns_worker": sum(row["worker_spawns"] for row in rows),
        "spawns_per_op": _lane._r((accept_spawns.count + sum(r["worker_spawns"] for r in rows)) / max(1, calls), 2),
        "recycles": rig.recycle_count() - recycles_before,
        "p95_wall_ms_context_only": _lane._r(_lane.percentile([row["wall_ms"] for row in rows], 0.95)),
    }


class _WedgeRig(_lane._Rig):
    """The hook-lane rig with this bench's worker init and a recycle counter."""

    def __enter__(self) -> "_WedgeRig":
        super().__enter__()
        setattr(self.server, "_worker_process_init", _wedge_worker_init)
        self._recycles = [0]
        real = self.ctx._write_recycle_line
        counter = self._recycles

        def counted(*args: Any, **kwargs: Any) -> Any:
            counter[0] += 1
            return real(*args, **kwargs)

        self.ctx._write_recycle_line = counted
        return self

    def recycle_count(self) -> int:
        return self._recycles[0]

    def wedge_every_guard_worker(self) -> int:
        """Park each guard worker in a kernel wait via one held hook frame apiece; returns once every guard
        worker is tracked overdue. The held callers' own timeouts run in daemon threads."""
        workers = self.server._guard_pool_workers()
        for index in range(workers):
            msg = _probe_message(self, 8000 + index)
            msg["params"][HOLD_FLAG] = True
            thread = threading.Thread(target=self._call_holder, args=(index, msg), daemon=True)
            thread.start()
            self._threads.append(thread)
        deadline = time.monotonic() + _WEDGE_WAIT_SECS
        while time.monotonic() < deadline:
            if len(self.ctx.__dict__.get("_guard_overdue") or ()) >= workers:
                return workers
            time.sleep(0.05)
        raise RuntimeError(f"only {len(self.ctx.__dict__.get('_guard_overdue') or ())} of {workers} guard workers overdue")


def run(probes: int, boot_secs: float) -> dict:
    report: dict = {"boot_secs_injected_after_wedge": boot_secs}
    with tempfile.TemporaryDirectory(prefix="warm-hook-wedge-") as scratch:
        os.environ[BOOT_SECS_ENV] = str(boot_secs)
        try:
            with _WedgeRig(Path(scratch)) as rig:
                rig.prewarm()
                report["guard_pool_workers"] = rig.server._guard_pool_workers()
                report["hook_lane_budget_secs"] = rig.server._HOOK_LANE_BUDGET_SECS
                report["steady"] = run_probes(rig, probes, 1)
                (rig.bench_dir / LOAD_MARKER).write_text("1", encoding="ascii")
                report["wedged_guard_workers"] = rig.wedge_every_guard_worker()
                report["wedged"] = run_probes(rig, probes, 100)
        finally:
            os.environ.pop(BOOT_SECS_ENV, None)
    report["failures"] = verdict(report)
    return report


def verdict(report: dict) -> list:
    """Reasons a phase breaks the contract; empty means every hook op answered for real, inside the
    process-time bar, without waiting on the pool result past it."""
    reasons = []
    for phase in ("steady", "wedged"):
        row = report[phase]
        if row["outcomes"].get("ok", 0) != row["calls"]:
            reasons.append(f"{phase}: outcomes {row['outcomes']}")
        if row["p95_process_ms"] is not None and row["p95_process_ms"] >= PROBE_PROCESS_BAR_MS:
            reasons.append(f"{phase}: p95 process {row['p95_process_ms']}ms >= {PROBE_PROCESS_BAR_MS:.0f}ms")
        wait = row["max_pool_result_wait_ms"]
        if wait is not None and wait >= PROBE_PROCESS_BAR_MS:
            reasons.append(f"{phase}: pool-result wait {wait}ms >= {PROBE_PROCESS_BAR_MS:.0f}ms")
    return reasons


def format_report(report: dict) -> str:
    lines = [
        f"warm hook pool wedge: guard_workers={report['guard_pool_workers']} hook_budget={report['hook_lane_budget_secs']}s "
        f"boot_secs_injected={report['boot_secs_injected_after_wedge']}",
        f"{'phase':8} {'calls':>5} {'p95proc':>8} {'medproc':>8} {'maxwait':>8} {'sp/op':>6} {'recyc':>5}  outcomes",
    ]
    for phase in ("steady", "wedged"):
        row = report[phase]
        lines.append(
            f"{phase:8} {row['calls']:>5} {row['p95_process_ms']!s:>8} {row['median_process_ms']!s:>8} "
            f"{row['max_pool_result_wait_ms']!s:>8} {row['spawns_per_op']!s:>6} {row['recycles']:>5}  {row['outcomes']}"
        )
    lines.append("contract: " + ("HOLDS" if not report["failures"] else "BROKEN"))
    lines += [f"  - {reason}" for reason in report["failures"]]
    return "\n".join(lines)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--label", default="base", help="free-form tag recorded in the report")
    parser.add_argument("--probes", type=int, default=8)
    parser.add_argument("--boot-secs", type=float, default=5.0)
    parser.add_argument("--out", default=None, help="write the JSON report here")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true", help="exit 1 when the contract is broken")
    args = parser.parse_args(argv)
    report = {"label": args.label, **run(args.probes, args.boot_secs)}
    text = json.dumps(report, indent=2, default=str)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text if args.json else format_report(report))
    return 1 if args.strict and report["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
