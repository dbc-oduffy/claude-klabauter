"""Hook-lane falsifier: hooks.* replies while every heavy pool slot is held, and what a recycled holder's caller receives.

Drives the real `_ServerContext._pool_dispatch` admission path over real `ProcessPoolExecutor` pools
(`_ensure_dispatch_pool` / `_ensure_guard_pool`, so a pool-sizing change lands here without edits).
Heavy holders are ops whose worker blocks in a kernel file-handle wait (POSIX: FIFO `open`; Windows:
`ConnectNamedPipe`), the shape of the N1 open/mkstemp stall. Hooks run their real handlers in the
guard pool. Reported per op: process ms (worker CPU), spawns, p95 reply ms; wall clock alone
never carries a verdict (DR-344). A second rig forces one `_recycle_stalled_pool` and records
what each waiting caller got.

    python -m coordinator_core.benchmarks.hook_lane_full_pool [--heavy-holders auto|N]
        [--hook-calls 20] [--warmup-calls 2] [--measure-budget-secs 20] [--json] [--strict]

TRAP: the holders' own `_pool_dispatch` threads time out near 28s and kill their worker, so a
rig's whole measured phase must end well inside that; `--measure-budget-secs` bounds it.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import json
import math
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

HOLD_OP = "dispatch.ask_stage"
BENCH_DIR_ENV = "HOOK_LANE_BENCH_DIR"
HOOK_P95_BAR_MS = 500.0
HOOK_PROCESS_BAR_MS = 200.0
_READY_WAIT_SECS = 60.0
_RECYCLE_REPLY_WAIT_SECS = 15.0

_ORIGINAL_INIT: Optional[Callable[..., None]] = None


def _hold_in_file_io(bench_dir: Path) -> None:
    """Park this worker in a kernel file-handle wait that never completes; a marker file says it got here."""
    pid = os.getpid()
    (bench_dir / f"held-{pid}").write_text("held", encoding="ascii")
    if sys.platform == "win32":
        import _winapi

        handle = _winapi.CreateNamedPipe(
            rf"\\.\pipe\hook-lane-hold-{pid}",
            _winapi.PIPE_ACCESS_DUPLEX,
            0,  # PIPE_TYPE_BYTE | PIPE_WAIT: both are zero, and `_winapi` exports neither
            1,
            512,
            512,
            0,
            _winapi.NULL,
        )
        _winapi.ConnectNamedPipe(handle, False)
    else:
        fifo = bench_dir / f"hold-{pid}.fifo"
        os.mkfifo(fifo)
        with open(fifo, "rb"):
            pass


def _bench_worker_init(stderr_dir=None, stall_dir=None, forward_queue=None) -> None:
    """Pool-worker initializer: the engine's own init, then a body wrapper that parks `HOLD_OP`
    and stamps each op's worker CPU ms and spawn count into its reply as `_bench`."""
    from coordinator_core.warm import server

    (_ORIGINAL_INIT or server._worker_process_init)(stderr_dir, stall_dir, forward_queue)
    from coordinator_core import ipc

    ipc.allow_unstamped_dispatch()
    bench_dir = Path(os.environ[BENCH_DIR_ENV])
    real_body = server._pool_dispatch_worker_body

    def body(msg: Any, caller: Any) -> Any:
        if isinstance(msg, dict) and msg.get("method") == HOLD_OP:
            _hold_in_file_io(bench_dir)
            return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"held": True}}
        spawns0 = server._spawn_count_or_none()
        cpu0 = time.process_time()
        out = real_body(msg, caller)
        cpu_ms = (time.process_time() - cpu0) * 1000.0
        spawns = server._spawn_delta(spawns0, server._spawn_count_or_none())
        return {**out, "_bench": {"cpu_ms": cpu_ms, "spawns": spawns}} if isinstance(out, dict) else out

    setattr(server, "_pool_dispatch_worker_body", body)


def _bench_noop() -> int:
    return os.getpid()


class _VersionState:
    server_sha = "bench"

    def is_skewed(self, client_token: str) -> bool:
        return False

    def is_token_skewed(self, client_token: str) -> bool:
        return False


def percentile(values: list, q: float) -> Optional[float]:
    """Nearest-rank percentile of `values`; None when empty."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))]


def _outcome(resp: Any) -> str:
    if not isinstance(resp, dict):
        return "no-reply"
    if "result" in resp:
        return "ok"
    err = resp.get("error")
    return f"error:{err.get('code')}" if isinstance(err, dict) else "malformed"


class _Rig:
    """A `_ServerContext` over real pools with a scratch home, engine root and repo; `close()` kills every worker."""

    def __init__(self, scratch: Path) -> None:
        self.scratch = scratch
        self.bench_dir = scratch / "bench"
        self.repo = scratch / "repo"
        self.bench_dir.mkdir()
        (self.repo / ".git").mkdir(parents=True)
        self._env_saved: dict = {}
        self._threads: list = []
        self.replies: dict = {}
        self.ctx: Any = None

    def __enter__(self) -> "_Rig":
        from coordinator_core.warm import server

        global _ORIGINAL_INIT
        self.server = server
        for key, value in (
            ("COORDINATOR_SETTINGS_HOME", str(self.scratch / "home")),
            (BENCH_DIR_ENV, str(self.bench_dir)),
        ):
            self._env_saved[key] = os.environ.get(key)
            os.environ[key] = value
        _ORIGINAL_INIT = server._worker_process_init
        setattr(server, "_worker_process_init", _bench_worker_init)
        self.ctx = server._ServerContext(
            name="pipe-hook-lane-bench",
            sid="sid-hook-lane-bench",
            version_state=_VersionState(),  # type: ignore[arg-type]
            engine_root=self.scratch / "engine",
        )
        self.ctx._heal_lost_pool = lambda *_a, **_k: None
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def heavy_cap(self) -> int:
        return int(self.ctx._admission_gate()._heavy_cap)

    def prewarm(self) -> None:
        """Boot every worker of both pools the way the engine's boot prewarm does, before anything is held."""
        ctx = self.ctx
        guard_workers = self.server._guard_pool_workers()
        guard_futs = ctx._prewarm_pool(True, guard_workers)
        heavy = ctx._ensure_dispatch_pool()
        heavy_futs = [heavy.submit(_bench_noop) for _ in range(self.heavy_cap())]
        guard = ctx._ensure_guard_pool()
        guard_futs += [guard.submit(_bench_noop) for _ in range(guard_workers)]
        concurrent.futures.wait(guard_futs + heavy_futs, timeout=_READY_WAIT_SECS)

    def hold_heavy_slots(self, count: int) -> None:
        """Submit `count` `HOLD_OP` calls through `_pool_dispatch` and wait until each worker reports it is parked."""
        for index in range(count):
            msg = {"jsonrpc": "2.0", "id": 9000 + index, "method": HOLD_OP, "params": {}}
            thread = threading.Thread(target=self._call_holder, args=(index, msg), daemon=True)
            thread.start()
            self._threads.append(thread)
        deadline = time.monotonic() + _READY_WAIT_SECS
        while time.monotonic() < deadline:
            if len(list(self.bench_dir.glob("held-*"))) >= count:
                return
            time.sleep(0.05)
        raise RuntimeError(f"only {len(list(self.bench_dir.glob('held-*')))} of {count} heavy holders parked")

    def _call_holder(self, index: int, msg: dict) -> None:
        t0 = time.monotonic()
        try:
            resp = self.ctx._pool_dispatch(msg)
        except BaseException as exc:  # noqa: BLE001 -- a raise is a reply this benchmark must record
            resp = {"raised": f"{type(exc).__name__}: {exc}"}
        self.replies[index] = (resp, time.monotonic() - t0)

    def hook_message(self, op: str, request_id: int) -> dict:
        repo = str(self.repo)
        base = {"cwd": repo, "session_id": "s-hook-lane-bench"}
        if op == "hooks.preuse_bash_dispatch":
            payload = {**base, "tool_name": "Bash", "tool_input": {"command": "ls"}}
        elif op == "hooks.preuse_write_dispatch":
            payload = {
                **base,
                "tool_name": "Write",
                "tool_input": {"file_path": str(self.repo / "notes.txt"), "content": "x"},
            }
        else:
            payload = {**base, "tool_name": "Read", "tool_input": {}}
        return {"jsonrpc": "2.0", "id": request_id, "method": op, "params": {"payload": payload}}

    def _live_procs(self) -> list:
        procs = list(self.ctx.__dict__.get("_retired_procs", []))
        for name in ("_dispatch_pool", "_guard_pool"):
            procs += list((getattr(getattr(self.ctx, name, None), "_processes", None) or {}).values())
        return procs

    def close(self) -> None:
        global _ORIGINAL_INIT
        ctx = self.ctx
        if ctx is not None:
            procs = self._live_procs()
            for name in ("_telemetry_drain_stop", "_guard_telemetry_drain_stop"):
                stop = ctx.__dict__.get(name)
                if stop is not None:
                    stop.set()
            for proc in procs:
                with contextlib.suppress(Exception):
                    proc.kill()
            for name in ("_dispatch_pool", "_guard_pool"):
                pool = getattr(ctx, name, None)
                if pool is not None:
                    with contextlib.suppress(Exception):
                        pool.shutdown(wait=False, cancel_futures=True)
            for thread in self._threads:
                thread.join(10.0)
            for proc in procs:
                with contextlib.suppress(Exception):
                    proc.join(10.0)
            setattr(self.server, "_worker_process_init", _ORIGINAL_INIT or self.server._worker_process_init)
        _ORIGINAL_INIT = None
        for key, old in self._env_saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


class _SpawnCounter:
    """Counts `subprocess.Popen` constructions in this (accept) process while active."""

    def __init__(self) -> None:
        self.count = 0
        self._orig: Any = None

    def __enter__(self) -> "_SpawnCounter":
        counter = self
        self._orig = subprocess.Popen.__init__

        def counted(popen_self: Any, *args: Any, **kwargs: Any) -> None:
            counter.count += 1
            return counter._orig(popen_self, *args, **kwargs)

        subprocess.Popen.__init__ = counted  # type: ignore[method-assign]
        return self

    def __exit__(self, *_exc: Any) -> None:
        subprocess.Popen.__init__ = self._orig  # type: ignore[method-assign]


def measure_hooks(rig: _Rig, ops: list, calls: int, warmup: int, budget_secs: float) -> dict:
    """Fire `warmup` untimed then `calls` timed rounds of every op, round-robin, through `_pool_dispatch`."""
    stats: dict = {op: {"reply_ms": [], "worker_cpu_ms": [], "worker_spawns": 0, "accept_cpu_ms": [], "outcomes": {}} for op in ops}
    guard_pool = rig.ctx._ensure_guard_pool()
    workers_before = len(getattr(guard_pool, "_processes", {}) or {})
    skipped = 0
    deadline = time.monotonic() + budget_secs
    request_id = 1
    with _SpawnCounter() as accept_spawns:
        for round_index in range(warmup + calls):
            for op in ops:
                if time.monotonic() > deadline:
                    skipped += 1
                    continue
                msg = rig.hook_message(op, request_id)
                request_id += 1
                cpu0 = time.process_time()
                t0 = time.perf_counter()
                resp = rig.ctx._pool_dispatch(msg)
                wall_ms = (time.perf_counter() - t0) * 1000.0
                accept_ms = (time.process_time() - cpu0) * 1000.0
                if round_index < warmup:
                    continue
                row = stats[op]
                outcome = _outcome(resp)
                row["outcomes"][outcome] = row["outcomes"].get(outcome, 0) + 1
                row["reply_ms"].append(wall_ms)
                row["accept_cpu_ms"].append(accept_ms)
                bench = resp.get("_bench") if isinstance(resp, dict) else None
                if isinstance(bench, dict):
                    row["worker_cpu_ms"].append(float(bench["cpu_ms"]))
                    row["worker_spawns"] += int(bench.get("spawns") or 0)
    workers_after = len(getattr(rig.ctx._guard_pool, "_processes", {}) or {})
    out_ops = {}
    for op, row in stats.items():
        out_ops[op] = {
            "calls": len(row["reply_ms"]),
            "outcomes": row["outcomes"],
            "p50_reply_ms": _r(percentile(row["reply_ms"], 0.5)),
            "p95_reply_ms": _r(percentile(row["reply_ms"], 0.95)),
            "max_reply_ms": _r(max(row["reply_ms"], default=None)),
            "worker_process_ms_median": _r(statistics.median(row["worker_cpu_ms"]) if row["worker_cpu_ms"] else None),
            "worker_process_ms_max": _r(max(row["worker_cpu_ms"], default=None)),
            "worker_spawns": row["worker_spawns"],
            "accept_process_ms_median": _r(statistics.median(row["accept_cpu_ms"]) if row["accept_cpu_ms"] else None),
        }
    return {
        "ops": out_ops,
        "accept_spawns": accept_spawns.count,
        "guard_workers_started_during_measure": workers_after - workers_before,
        "skipped_calls": skipped,
    }


def force_recycle(rig: _Rig, holders: int) -> dict:
    """Recycle the heavy pool under `holders` parked callers and record each caller's reply."""
    ctx = rig.ctx
    pool = ctx._ensure_dispatch_pool()
    t0 = time.monotonic()
    ctx._recycle_stalled_pool(pool, [HOLD_OP] * holders)
    deadline = t0 + _RECYCLE_REPLY_WAIT_SECS
    while time.monotonic() < deadline and len(rig.replies) < holders:
        time.sleep(0.05)
    time.sleep(1.0)
    stall_dir = Path(getattr(ctx, "_stall_dir", rig.scratch / "no-stall-dir"))
    recycle_lines = sorted(p.name for p in stall_dir.glob("*-recycle.txt")) if stall_dir.is_dir() else []
    callers = []
    for index in range(holders):
        resp, secs = rig.replies.get(index, (None, None))
        err = resp.get("error") if isinstance(resp, dict) else None
        message = str(err.get("message", "")) if isinstance(err, dict) else ""
        data = err.get("data") if isinstance(err, dict) and isinstance(err.get("data"), dict) else {}
        blob = json.dumps({"message": message, "data": data}, default=str)
        callers.append(
            {
                "answered": resp is not None,
                "reply_secs": _r(secs, 2),
                "outcome": _outcome(resp),
                "code": err.get("code") if isinstance(err, dict) else None,
                "names_op": HOLD_OP in blob,
                "names_stall_dump": "stall" in blob.lower() and ("stall_dump" in blob or "stall-dumps" in blob),
                "message_head": message[:160],
            }
        )
    return {
        "holders": holders,
        "callers": callers,
        "unanswered": sum(1 for c in callers if not c["answered"]),
        "recycle_line_files": recycle_lines,
        "indeterminate_code": getattr(rig.server, "WARM_DISPATCH_INDETERMINATE", None),
    }


def _r(value: Optional[float], digits: int = 1) -> Optional[float]:
    return None if value is None else round(value, digits)


def verdict(report: dict) -> list:
    """Reasons the falsifier does not hold; an empty list means every expected_when_true clause holds."""
    reasons = []
    for op, row in report["hooks"]["ops"].items():
        if row["calls"] == 0:
            reasons.append(f"{op}: no timed call answered")
            continue
        if row["outcomes"].get("ok", 0) != row["calls"]:
            reasons.append(f"{op}: outcomes {row['outcomes']}")
        if row["p95_reply_ms"] is None or row["p95_reply_ms"] >= HOOK_P95_BAR_MS:
            reasons.append(f"{op}: p95 reply {row['p95_reply_ms']}ms >= {HOOK_P95_BAR_MS:.0f}ms")
        proc = row["worker_process_ms_median"]
        if proc is not None and proc >= HOOK_PROCESS_BAR_MS:
            reasons.append(f"{op}: process {proc}ms >= {HOOK_PROCESS_BAR_MS:.0f}ms")
    if report["hooks"]["skipped_calls"]:
        reasons.append(f"{report['hooks']['skipped_calls']} hook calls skipped at the measure budget")
    recycle = report["recycle"]
    if recycle["unanswered"]:
        reasons.append(f"recycle: {recycle['unanswered']} of {recycle['holders']} callers unanswered")
    for caller in recycle["callers"]:
        if caller["answered"] and not (caller["names_op"] and caller["names_stall_dump"]):
            reasons.append(
                f"recycle: reply {caller['outcome']} names_op={caller['names_op']} "
                f"names_stall_dump={caller['names_stall_dump']}"
            )
            break
    return reasons


def run(heavy_holders: Optional[int], hook_calls: int, warmup_calls: int, budget_secs: float) -> dict:
    """Both rigs, each under its own scratch dir; returns the report dict."""
    ops = ["hooks.preuse_bash_dispatch", "hooks.preuse_write_dispatch", "hooks.guard_editor_python_direct"]
    report: dict = {}
    with tempfile.TemporaryDirectory(prefix="hook-lane-hooks-") as scratch:
        with _Rig(Path(scratch)) as rig:
            rig.prewarm()
            count = heavy_holders if heavy_holders is not None else rig.heavy_cap()
            rig.hold_heavy_slots(count)
            report["heavy_holders"] = count
            report["heavy_cap"] = rig.heavy_cap()
            report["guard_pool_workers"] = rig.server._guard_pool_workers()
            report["hooks"] = measure_hooks(rig, ops, hook_calls, warmup_calls, budget_secs)
    with tempfile.TemporaryDirectory(prefix="hook-lane-recycle-") as scratch:
        with _Rig(Path(scratch)) as rig:
            rig.prewarm()
            count = heavy_holders if heavy_holders is not None else rig.heavy_cap()
            rig.hold_heavy_slots(count)
            report["recycle"] = force_recycle(rig, count)
    report["failures"] = verdict(report)
    return report


def format_report(report: dict) -> str:
    lines = [
        f"hook-lane full-pool: heavy_holders={report['heavy_holders']} (cap {report['heavy_cap']}) "
        f"guard_pool_workers={report['guard_pool_workers']} accept_spawns={report['hooks']['accept_spawns']} "
        f"guard_workers_started_during_measure={report['hooks']['guard_workers_started_during_measure']}",
        f"{'op':38} {'calls':>5} {'p50ms':>8} {'p95ms':>8} {'maxms':>8} {'procms':>7} {'spawns':>6}  outcomes",
    ]
    for op, row in report["hooks"]["ops"].items():
        lines.append(
            f"{op:38} {row['calls']:>5} {row['p50_reply_ms']!s:>8} {row['p95_reply_ms']!s:>8} "
            f"{row['max_reply_ms']!s:>8} {row['worker_process_ms_median']!s:>7} {row['worker_spawns']:>6}  {row['outcomes']}"
        )
    recycle = report["recycle"]
    lines.append(f"recycle: holders={recycle['holders']} unanswered={recycle['unanswered']} recycle_line_files={recycle['recycle_line_files']}")
    for index, caller in enumerate(recycle["callers"]):
        lines.append(
            f"  caller {index}: {caller['outcome']} after {caller['reply_secs']}s names_op={caller['names_op']} "
            f"names_stall_dump={caller['names_stall_dump']} :: {caller['message_head']!r}"
        )
    lines.append("falsifier: " + ("HOLDS" if not report["failures"] else "FAILS"))
    lines += [f"  - {reason}" for reason in report["failures"]]
    return "\n".join(lines)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--heavy-holders", default="auto")
    parser.add_argument("--hook-calls", type=int, default=20)
    parser.add_argument("--warmup-calls", type=int, default=2)
    parser.add_argument("--measure-budget-secs", type=float, default=20.0)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true", help="exit 1 when the falsifier does not hold")
    args = parser.parse_args(argv)
    holders = None if args.heavy_holders == "auto" else int(args.heavy_holders)
    report = run(holders, args.hook_calls, args.warmup_calls, args.measure_budget_secs)
    print(json.dumps(report, indent=2, default=str) if args.json else format_report(report))
    return 1 if args.strict and report["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
