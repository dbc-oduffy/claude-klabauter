"""Process time of the three hook ops over the 200ms one-process bar, single and under concurrent load.

Process time is defined here once, for every reader (C3, C4, the baseline JSON):

    process time = server-side op CPU + hook client process time, spawn count beside it.

* server-side op CPU: per-call CPU of the registered op handler in this process, batch-amortised by
  `process_time.in_process_time_ms` (sub-tick safe), taken through `entry_seam.reentrant_dispatch`.
  `p50_ms`/`p95_ms` are per-call `time.process_time` samples (tick-quantised on Windows) shown beside it.
* hook client process time: a fresh interpreter that imports `coordinator_core.warm.entry_seam`, the
  forwarder every warm-forwarded hook pays before it dials, via `process_time.batched_process_time_ms`.
  It is one figure shared by all three ops (the real hook scripts live in the discovery plane, not here).
* spawn count: `subprocess.Popen` constructions inside the op call, plus the client's `procs_per_call`.

Wall clock never gates anything. Trap: process time reads ~0 for a call blocked on a lock, disk or child,
so the concurrent mode's liveness verdict is `stalled` (a hook that got no answer within `STALL_CAP_SECS`,
a liveness cap, not an acceptance figure) and its wall columns are context only.

Concurrent mode (the stall falsifier): an in-process `_ServerContext` with every pool worker parked on a
held heavy op, then `CONCURRENT_HOOKS` hook calls (the three ops round-robin) through the real accept path.
`stall_free` is true only when every hook answered; `process_ms_per_call` is the whole-process CPU of the
burst over the calls answered.

Usage: python -m coordinator_core.benchmarks.hook_ops_process_time [--n N] [--label base|end] [--no-write] [--json]
The default label is `base` while the baseline JSON has no base row, `end` afterwards; a row with the same
label is replaced. Exit 0 on a completed run; the bar is reported per op (`under_bar`), not enforced.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import statistics
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from coordinator_core.benchmarks.preuse_dispatch_phase_cost import (
    _SpawnCounter,
    _build_fixture,
    _call,
    _head,
)
from coordinator_core.benchmarks.process_time import (
    batched_process_time_ms,
    in_process_time_ms,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = Path(__file__).resolve().parent / "baselines" / "hook_ops_process_time.json"

BAR_MS = 200.0
CLIENT_K = 5
CONCURRENT_HOOKS = 12
HOLD_OP = "dispatch.ask_stage"
HOLD_SECS = 30.0
STALL_CAP_SECS = 8.0

OP_GUARD_EDITOR = "hooks.guard_editor_python_direct"
OP_TASKSTOP = "hooks.taskstop_launch_recorder"
OP_PREUSE_BASH = "hooks.preuse_bash_dispatch"
HOOK_OPS = (OP_GUARD_EDITOR, OP_TASKSTOP, OP_PREUSE_BASH)

_CLIENT_ARGV = [sys.executable, "-c", "from coordinator_core.warm import entry_seam"]


def _write_route_table(root: Path) -> None:
    """A route table with an `editor_python` leg, so the guard does its real match work, not the no-table exit."""
    table = {
        "schema_version": 1,
        "default_agent_type": "coordinator:executor",
        "work_classes": [],
        "editor_python": {
            "preferred_agent_type": "example-game-repo",
            "level": "warn",
            "tool_names": ["mcp__example_game_repo-control__execute_python_code"],
            "bash_patterns": [r"UnrealEditor\S*\s.*-ExecutePythonScript", r"\bpython\S*\s.*unreal_editor_cmd"],
        },
    }
    target = root / ".claude" / "route-table.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(table), encoding="utf-8")


def _payloads(fx: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """The common live shape per op: an ordinary Bash call that every guard lets through."""
    cwd = str(fx["root"])
    command = f"ls -la {fx['root']}/data"
    pre = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command},
           "session_id": "bench", "cwd": cwd}
    post = {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": command},
            "tool_response": {"stdout": "ok"}, "session_id": "bench", "cwd": cwd}
    return {OP_GUARD_EDITOR: pre, OP_TASKSTOP: post, OP_PREUSE_BASH: pre}


def _quantile(ordered: List[float], pct: float) -> float:
    idx = max(0, min(len(ordered) - 1, int(pct * (len(ordered) - 1) + 0.5)))
    return ordered[idx]


def measure_client() -> Dict[str, Any]:
    """Hook client process time: a fresh `entry_seam` import, amortised over `CLIENT_K` spawns."""
    r = batched_process_time_ms(_CLIENT_ARGV, k=CLIENT_K, cwd=str(_REPO_ROOT))
    if r["rc"] != 0:
        raise RuntimeError(f"hook client probe exited {r['rc']}")
    return {"client_ms": round(r["process_time_ms"], 1), "client_procs": round(r["procs_per_call"], 2)}


def measure_op(op: str, payload: Dict[str, Any], n: int) -> Dict[str, Any]:
    """Server-side CPU for one op: cold call, `n` steady per-call samples, then the amortised figure."""
    from coordinator_core.warm.entry_seam import reentrant_dispatch

    cold = _call(op, payload)
    if (cold["error"] or "").startswith("OpUnavailableError"):
        raise RuntimeError(f"op not registered: {op}: {cold['error']}")
    steady = [_call(op, payload) for _ in range(n)]
    ordered = sorted(s["process_ms"] for s in steady)

    def one() -> None:
        with contextlib.suppress(Exception):
            reentrant_dispatch(op, payload)

    amortised = in_process_time_ms(one)
    return {
        "op": op,
        "cold_ms": cold["process_ms"],
        "p50_ms": round(statistics.median(ordered), 1),
        "p95_ms": round(_quantile(ordered, 0.95), 1),
        "server_cpu_ms": amortised["process_time_ms"],
        "spawns": max(s["spawns"] for s in steady),
        "slow_phase": steady[-1]["slow_phase"],
        "error": cold["error"] or steady[-1]["error"],
    }


def measure_single(n: int, fx: Dict[str, Any]) -> List[Dict[str, Any]]:
    client = measure_client()
    rows = []
    for op, payload in _payloads(fx).items():
        row = measure_op(op, payload, n)
        row.update(client)
        row["process_ms"] = round(row["server_cpu_ms"] + client["client_ms"], 1)
        row["spawn_count"] = row["spawns"] + client["client_procs"]
        row["under_bar"] = row["process_ms"] < BAR_MS
        rows.append(row)
    return rows


class _VersionState:
    server_sha = "bench"

    def is_skewed(self, client_token: str) -> bool:
        return False

    def is_token_skewed(self, client_token: str) -> bool:
        return False


class _FakeIO:
    def __init__(self, method: str, request_id: int, params: Dict[str, Any]):
        frame = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params,
                 "_engine_token": "client-token"}
        self._lines = [(json.dumps(frame) + "\n").encode("utf-8")]
        self.written: list = []

    def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


def _wait(predicate, secs: float) -> bool:
    deadline = time.monotonic() + secs
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def measure_concurrent(fx: Dict[str, Any], hooks: int = CONCURRENT_HOOKS) -> Dict[str, Any]:
    """Every pool worker parked on a held heavy op, then `hooks` hook calls; reports whether any stalled."""
    import concurrent.futures

    from coordinator_core.temp_layout import managed_temp_dir
    from coordinator_core.warm import server, telemetry
    from coordinator_core.warm.entry_seam import reentrant_dispatch

    payloads = _payloads(fx)
    release = threading.Event()
    errors: List[str] = []

    def body(msg: dict, caller: Any) -> dict:
        method = msg["method"]
        if method == HOLD_OP:
            release.wait(HOLD_SECS)
            return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"ok": method}}
        try:
            return {"jsonrpc": "2.0", "id": msg.get("id"), "result": reentrant_dispatch(method, msg.get("params") or {})}
        except Exception as exc:  # noqa: BLE001 -- a raise is a measured outcome
            errors.append(f"{method}: {type(exc).__name__}: {exc}")
            return {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32000, "message": str(exc)}}

    real_body, real_flush = server._pool_dispatch_worker_body, telemetry.flush_requests
    server._pool_dispatch_worker_body = body  # type: ignore[assignment]
    telemetry.flush_requests = lambda *_a, **_k: None  # type: ignore[assignment]
    telemetry._REQUESTS.clear()
    spawns = _SpawnCounter()
    try:
        with managed_temp_dir(prefix="hook-ops-conc-") as td:
            tmp = Path(td).resolve()
            ctx = server._ServerContext(name="pipe-hook-ops", sid="sid-hook-ops",
                                        version_state=_VersionState(), engine_root=tmp)
            ctx._claim_dir = tmp / "claims"
            ctx._claim_dir.mkdir()
            light = getattr(server, "_LIGHT_RESERVE_WORKERS", 2)
            ctx._dispatch_pool = concurrent.futures.ThreadPoolExecutor(max_workers=server.WORKER_POOL_SIZE + light)
            ctx._start_worker_pool(pool_size=server.WORKER_POOL_SIZE)
            holders = [_FakeIO(HOLD_OP, i, {}) for i in range(server.WORKER_POOL_SIZE)]
            hook_ios = [
                _FakeIO(HOOK_OPS[i % len(HOOK_OPS)], 1000 + i, payloads[HOOK_OPS[i % len(HOOK_OPS)]])
                for i in range(hooks)
            ]
            try:
                for io in holders:
                    ctx._enqueue_connection(io, admitted=False)
                held = _wait(lambda: ctx._busy_workers() == server.WORKER_POOL_SIZE, 10.0)
                cpu0 = time.process_time()
                with spawns:
                    for io in hook_ios:
                        ctx._enqueue_connection(io, admitted=False)
                    _wait(lambda: all(io.written for io in hook_ios), STALL_CAP_SECS)
                cpu_ms = (time.process_time() - cpu0) * 1000.0
            finally:
                release.set()
                ctx._dispatch_pool.shutdown(wait=False, cancel_futures=True)
            answered = [io for io in hook_ios if io.written]
            rows = [r for r in telemetry.request_samples() if r.get("op") in HOOK_OPS]
            waits = [(r["end_ts"] - r["enqueue_ts"]) * 1000.0 for r in rows if "end_ts" in r and "enqueue_ts" in r]
    finally:
        server._pool_dispatch_worker_body = real_body  # type: ignore[assignment]
        telemetry.flush_requests = real_flush  # type: ignore[assignment]
    stalled = len(hook_ios) - len(answered)
    per_call = round(cpu_ms / len(answered), 1) if answered else None
    return {
        "heavy_slots_held": held,
        "pool_size": server.WORKER_POOL_SIZE,
        "hooks": len(hook_ios),
        "answered": len(answered),
        "stalled": stalled,
        "stall_free": held and stalled == 0,
        "process_ms_per_call": per_call,
        "spawns": spawns.count,
        "errors": errors[:5],
        "wall_context_max_ms": round(max(waits), 1) if waits else None,
        "under_bar": per_call is not None and per_call < BAR_MS,
    }


def run(n: int = 15) -> Dict[str, Any]:
    from coordinator_core.temp_layout import managed_temp_dir

    with managed_temp_dir(prefix="hook-ops-pt-") as td:
        root = Path(td).resolve() / "repo"
        root.mkdir()
        fx = _build_fixture(root)
        _write_route_table(root)
        single = measure_single(n, fx)
        concurrent_row = measure_concurrent(fx)
    return {"head": _head(), "n": n, "bar_ms": BAR_MS, "single": single, "concurrent": concurrent_row}


def _next_label(path: Path) -> str:
    try:
        rows = json.loads(path.read_text(encoding="utf-8")).get("rows", [])
    except (OSError, ValueError):
        return "base"
    return "end" if any(r.get("label") == "base" for r in rows) else "base"


def write_row(result: Dict[str, Any], label: str, path: Path = BASELINE_PATH) -> None:
    """Replace the row carrying `label` in the baseline JSON, or append it."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = {"schema": 1, "bar_ms": BAR_MS, "rows": []}
    doc["rows"] = [r for r in doc.get("rows", []) if r.get("label") != label] + [{"label": label, **result}]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def format_report(result: Dict[str, Any]) -> str:
    lines = [
        f"head={result['head']} n={result['n']} bar={result['bar_ms']:.0f}ms "
        "(process time = server op CPU + hook client process; never wall)",
        "%-34s %8s %8s %8s %10s %9s %7s %s"
        % ("op", "p50_ms", "p95_ms", "cpu_ms", "client_ms", "total_ms", "spawns", "under_bar"),
    ]
    for r in result["single"]:
        lines.append(
            "%-34s %8.1f %8.1f %8.3f %10.1f %9.1f %7.1f %s%s"
            % (r["op"], r["p50_ms"], r["p95_ms"], r["server_cpu_ms"], r["client_ms"], r["process_ms"],
               r["spawn_count"], r["under_bar"], f"  slow_phase={r['slow_phase']}" if r["slow_phase"] else "")
        )
    c = result["concurrent"]
    lines.append(
        f"concurrent: heavy_slots_held={c['heavy_slots_held']} hooks={c['hooks']} answered={c['answered']} "
        f"stalled={c['stalled']} stall_free={c['stall_free']} process_ms_per_call={c['process_ms_per_call']} "
        f"spawns={c['spawns']} wall_context_max_ms={c['wall_context_max_ms']} under_bar={c['under_bar']}"
    )
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    from coordinator_core.benchmarks import declare_benchmark_origin

    declare_benchmark_origin()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=15)
    parser.add_argument("--label", default=None)
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = run(n=args.n)
    if not args.no_write:
        write_row(result, args.label or _next_label(BASELINE_PATH))
    print(json.dumps(result, indent=2) if args.json else format_report(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
