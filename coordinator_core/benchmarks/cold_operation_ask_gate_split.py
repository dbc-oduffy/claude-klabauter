"""Per-leg process time of a cold `cold_operation` child running a run-bound `dispatch.ask_gate`.

Spawns `python -m coordinator_core.ops.workflow_fire.cold_operation <request> <response>` the way
`headless._invoke_source` does on a cold fall-through (`COORDINATOR_WARM=0`, `PYTHONPATH` = the executor
root, cwd = the repo), billed as a whole process tree by `process_time.single_invocation_tree_process_time`
(job-object CPU, never wall clock). The fixture is `ask_gate_cold_route_second_call.build_fixture`.

Legs, all process ms: `interpreter_start_ms` (a bare `python -c pass`), `imports_ms` (the engine modules a
dispatch needs, imported in a bare interpreter, minus the floor), `dispatch_ms` (the in-process dispatch
minus the gate body), `body_ms` (the gate body less its lock waits) and `lock_wait_ms` (wall spent inside
`locked_rmw` / `_clear_held_lock` that is not CPU). `dispatch_ms`, `body_ms` and `lock_wait_ms` come from a
probe child (`ask_gate_cold_route_second_call --child`) that runs the same `ipc.dispatch_message` call with
timing wrappers; `total_process_ms` is the real cold child. `unattributed_ms` is the total minus the legs.
`import_top` lists the largest `-X importtime` cumulative entries of the import command (wall, attribution only).

An engine root with no build stamp refuses the dispatch (`ipc.UNSTAMPED_ENGINE_ROOT_ERROR`); on such a root
the real child is launched as `python -c` calling `ipc.allow_unstamped_dispatch()` then `runpy`-running the same
module, and the report carries `unstamped_carveout: true`. The carve-out adds no import the child does not make.

Run: `python -m coordinator_core.benchmarks.cold_operation_ask_gate_split [--dirs N] [--runs N] [--enforce-bar]`.
Scratch lives in a managed temp dir this script removes.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
from pathlib import Path

#: The brightline process-time bar (CLAUDE.md § The brightline).
BAR_MS = 500.0
DEFAULT_DIRS = 4000
DEFAULT_RUNS = 3
COLD_MODULE = "coordinator_core.ops.workflow_fire.cold_operation"
IMPORT_STATEMENT = (
    "import coordinator_core.ipc, coordinator_core.locked_write, coordinator_core.ops.dispatch_emit.ask_gate"
)
_STAMP_PARTS = ("coordinator_core", "_engine_stamp")
_CARVEOUT_LAUNCH = (
    "from coordinator_core import ipc; ipc.allow_unstamped_dispatch(); import runpy; "
    f"runpy.run_module({'coordinator_core.ops.workflow_fire.cold_operation'!r}, run_name='__main__')"
)
_IMPORT_TOP = 8


def engine_root() -> Path:
    """The executor root `headless._executor_root` resolves for this checkout."""
    return Path(__file__).resolve().parents[2]


def needs_unstamped_carveout(root: Path) -> bool:
    """True when `root` carries no non-empty engine build stamp, so the stamp gate would refuse the dispatch."""
    try:
        return len((root.joinpath(*_STAMP_PARTS)).read_bytes()) == 0
    except OSError:
        return True


def over_bar(report: dict) -> bool:
    """True when the cold child breaches the bar, or never reached the op body (an error reply or a halt)."""
    return (
        max(report["total_process_ms"], report["cold_total_process_ms"]) >= BAR_MS
        or report["error_code"] is not None
        or report["halt"] is not None
    )


def parse_importtime(stderr: str, top: int = _IMPORT_TOP) -> list[dict]:
    """The `top` largest cumulative entries of a `-X importtime` stderr, as `{module, cumulative_us, self_us}`."""
    rows = []
    for line in stderr.splitlines():
        if not line.startswith("import time:"):
            continue
        parts = [p.strip() for p in line[len("import time:"):].split("|")]
        if len(parts) != 3 or not parts[0].isdigit():
            continue
        rows.append({"module": parts[2], "cumulative_us": int(parts[1]), "self_us": int(parts[0])})
    return sorted(rows, key=lambda r: r["cumulative_us"], reverse=True)[:top]


def _tree(cmd: list[str], env: dict, cwd: Path, scratch: Path, label: str) -> dict:
    from coordinator_core.benchmarks.process_time import single_invocation_tree_process_time

    tree = single_invocation_tree_process_time(
        cmd, env=env, cwd=str(cwd),
        stdout_path=str(scratch / f"{label}.stdout"), stderr_path=str(scratch / f"{label}.stderr"),
    )
    if tree["rc"] != 0:
        tail = (scratch / f"{label}.stderr").read_text(encoding="utf-8", errors="replace")[-1500:]
        raise RuntimeError(f"{label} failed (rc={tree['rc']}): {tail}")
    return tree


def _median_tree(cmd: list[str], env: dict, cwd: Path, scratch: Path, label: str, runs: int) -> float:
    return statistics.median(_tree(cmd, env, cwd, scratch, f"{label}{i}")["process_time_ms"] for i in range(runs))


def _cold_call(root: Path, scratch: Path, label: str, env: dict, request: dict, carveout: bool) -> dict:
    """One cold child as `_invoke_source` spawns it (`-m`, or `-c` running the same module when the stamp
    gate needs the carve-out); returns tree billing plus the reply it wrote."""
    artifacts = scratch / f"op-{label}"
    artifacts.mkdir()
    req, resp = artifacts / "request.json", artifacts / "response.json"
    req.write_text(json.dumps(request), encoding="utf-8")
    launch = ["-c", _CARVEOUT_LAUNCH] if carveout else ["-m", COLD_MODULE]
    tree = _tree([sys.executable, *launch, str(req), str(resp)], env, root, scratch, label)
    reply = json.loads(resp.read_text(encoding="utf-8"))
    error = reply.get("error") if isinstance(reply.get("error"), dict) else None
    result = reply.get("result") if isinstance(reply.get("result"), dict) else {}
    return {
        "process_ms": tree["process_time_ms"],
        "procs": tree["procs"],
        "error_code": error.get("code") if error else None,
        "error_message": error.get("message") if error else None,
        "halt": (result.get("halt") or {}).get("kind") if isinstance(result.get("halt"), dict) else None,
        "bound_sizing_path": result.get("sizing_path"),
    }


def _probe(root: Path, scratch: Path, env: dict, sizing_rel: str, run_id: str) -> dict:
    """The in-process phase split of the same dispatch (`ask_gate_cold_route_second_call --child`)."""
    out = scratch / "probe.phases.json"
    cmd = [
        sys.executable, "-m", "coordinator_core.benchmarks.ask_gate_cold_route_second_call", "--child",
        "--repo", str(root), "--params", json.dumps({"sizing_path": sizing_rel, "run_id": run_id}), "--out", str(out),
    ]
    _tree(cmd, env, root, scratch, "probe")
    return json.loads(out.read_text(encoding="utf-8"))


def _import_top(env: dict, root: Path) -> list[dict]:
    res = subprocess.run(
        [sys.executable, "-X", "importtime", "-c", IMPORT_STATEMENT], env=env, cwd=str(root),
        capture_output=True, text=True, check=True, timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return parse_importtime(res.stderr)


def measure(dirs: int = DEFAULT_DIRS, runs: int = DEFAULT_RUNS) -> dict:
    """Build the fixture and bill the cold child per leg; returns the report dict."""
    from coordinator_core.benchmarks import fixture_settings_home
    from coordinator_core.benchmarks.ask_gate_cold_route_second_call import RUN_ID, build_fixture
    from coordinator_core.benchmarks.isolated_clone import rmtree_or_raise
    from coordinator_core.temp_layout import make_managed_temp_dir

    scratch = make_managed_temp_dir(prefix="cold-ask-gate-split-")
    try:
        root = scratch / "repo"
        root.mkdir()
        with fixture_settings_home(scratch):
            sizing_rel = build_fixture(root, dirs)
            executor = engine_root()
            base_env = dict(os.environ, COORDINATOR_WARM="0", PYTHONPATH=str(executor))
            carveout = needs_unstamped_carveout(executor)
            request = {"op": "dispatch.ask_gate", "params": {"sizing_path": sizing_rel, "run_id": RUN_ID}, "repo_root": str(root)}
            floor = _median_tree([sys.executable, "-c", "pass"], base_env, root, scratch, "floor", runs)
            imported = _median_tree([sys.executable, "-c", IMPORT_STATEMENT], base_env, root, scratch, "imports", runs)
            top = _import_top(base_env, root)
            cold = _cold_call(root, scratch, "cold", base_env, request, carveout)
            repeats = [_cold_call(root, scratch, f"repeat{i}", base_env, request, carveout) for i in range(runs)]
            spawns = _probe(root, scratch, base_env, sizing_rel, RUN_ID)
            phases = spawns["phases"]
    finally:
        rmtree_or_raise(scratch, label="cold-ask-gate-split")
    lock = phases["lock_waits"]
    lock_wait = max(0.0, lock["wall_ms"] - lock["process_ms"])
    orphan = phases["orphan_lookup"]["process_ms"]
    legs = {
        "interpreter_start_ms": floor,
        "imports_ms": max(0.0, imported - floor),
        "dispatch_ms": phases["reply"]["process_ms"],
        "body_ms": phases["op_body"]["process_ms"] + orphan + lock["process_ms"],
        "lock_wait_ms": lock_wait,
    }
    total = statistics.median(c["process_ms"] for c in repeats)
    last = repeats[-1]
    report = {
        **{k: round(v, 1) for k, v in legs.items()},
        "spawns": spawns["spawns"],
        "spawn_argv": spawns["spawn_argv"],
        "total_process_ms": round(total, 1),
        "cold_total_process_ms": round(cold["process_ms"], 1),
        "unattributed_ms": round(cold["process_ms"] - sum(v for k, v in legs.items() if k != "lock_wait_ms"), 1),
        "tree_procs": cold["procs"],
        "dominant_leg": max(
            ("interpreter_start_ms", "imports_ms", "dispatch_ms", "body_ms"), key=lambda k: legs[k]
        ),
        "import_top": top,
        "unstamped_carveout": carveout,
        "error_code": cold["error_code"] or last["error_code"],
        "error_message": cold["error_message"] or last["error_message"],
        "halt": cold["halt"] or last["halt"],
        "bound_sizing_path": cold["bound_sizing_path"],
        "dirs": dirs,
        "runs": runs,
        "bar_ms": BAR_MS,
    }
    return report


def main() -> int:
    from coordinator_core.benchmarks import declare_benchmark_origin

    declare_benchmark_origin()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dirs", type=int, default=DEFAULT_DIRS)
    ap.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    ap.add_argument("--enforce-bar", action="store_true", help=f"exit 2 when total process ms >= {BAR_MS:.0f} or the child errors")
    args = ap.parse_args()
    report = measure(args.dirs, args.runs)
    print(json.dumps(report, indent=2, default=str))
    return 2 if args.enforce_bar and over_bar(report) else 0


if __name__ == "__main__":
    sys.exit(main())
