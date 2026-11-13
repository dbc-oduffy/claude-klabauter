"""Route and process time of a headless fire's `dispatch.ask_gate` engineOp.

Drives `headless._invoke_source('dispatch.ask_gate', ...)` exactly as `headless.py :: respond` does for an
`invoke` event, in a child interpreter billed as a whole process tree by
`process_time.single_invocation_tree_process_time` (job-object CPU, never wall clock). Prints
`{route, process_ms, spawns, error_code}` plus supporting fields.

`route` is `cold` when the call spawned `coordinator_core.ops.workflow_fire.cold_operation` (an interpreter
outside the warm engine) and `warm` when it did not. A call that outlives `--timeout` reports
`error_code` `timeout` with the process time measured up to the kill, never a hang.

`op_body_reached` is false when the engine root is an unstamped dev checkout (reply -32005): run against a
stamped engine for the op's own cost.

Run: `python -m coordinator_core.benchmarks.headless_fire_ask_gate_route [--timeout S] [--enforce-bar]`.
Scratch lives in a managed temp dir this script removes.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

#: The brightline process-time bar (CLAUDE.md § The brightline).
BAR_MS = 500.0
DEFAULT_TIMEOUT_SECS = 20.0
COLD_MODULE = "coordinator_core.ops.workflow_fire.cold_operation"
#: The cold interpreter's refusal on an engine root with no build stamp (a dev checkout): the route is
#: still measured, but the op body never ran, so `process_ms` is interpreter-plus-refusal cost only.
UNSTAMPED_ENGINE_ROOT_CODE = -32005


def classify_route(spawn_argv: list[list[str]]) -> str:
    """`cold` when any recorded spawn launched the cold-operation interpreter, else `warm`."""
    return "cold" if any(COLD_MODULE in part for argv in spawn_argv for part in argv) else "warm"


def error_code_of(exc: BaseException | None):
    """The reply code a failed `_invoke_source` call carries: the engine error code when the call raised
    `RuntimeError(json(error))`, `timeout` for a subprocess timeout, else the exception class name."""
    if exc is None:
        return None
    if isinstance(exc, subprocess.TimeoutExpired):
        return "timeout"
    if isinstance(exc, RuntimeError):
        try:
            err = json.loads(str(exc))
        except ValueError:
            err = None
        if isinstance(err, dict) and "code" in err:
            return err["code"]
    return type(exc).__name__


def _child_main(repo: str, params_json: str, artifacts: str, timeout: float, out: str) -> int:
    """Call `_invoke_source` as `respond` does and write the route evidence to `out`."""
    import pytest

    from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
    from coordinator_core.ops.workflow_fire import headless

    mp = pytest.MonkeyPatch()
    exc: BaseException | None = None
    result = None
    with _count_spawns_attributed(mp) as spawns:
        try:
            result = headless._invoke_source("dispatch.ask_gate", json.loads(params_json), repo, Path(artifacts), timeout)
        except BaseException as caught:  # noqa: BLE001 -- classified into error_code, never swallowed
            exc = caught
    mp.undo()
    argv = [list(s.argv) for s in spawns]
    report = {
        "route": classify_route(argv),
        "spawns": len(argv),
        "spawn_argv": argv,
        "error_code": error_code_of(exc),
        "error_message": str(exc)[:500] if exc is not None else None,
        "result_keys": sorted(result) if isinstance(result, dict) else None,
    }
    Path(out).write_text(json.dumps(report, default=str), encoding="utf-8", newline="\n")
    return 0


def measure(timeout: float = DEFAULT_TIMEOUT_SECS) -> dict:
    """Build the ask_gate fixture, then bill one headless `_invoke_source('dispatch.ask_gate', ...)` call."""
    from coordinator_core.benchmarks import fixture_settings_home
    from coordinator_core.benchmarks.ask_gate_cold_route_second_call import RUN_ID, build_fixture
    from coordinator_core.benchmarks.isolated_clone import rmtree_or_raise
    from coordinator_core.benchmarks.process_time import single_invocation_tree_process_time
    from coordinator_core.temp_layout import managed_tempdir

    scratch = Path(tempfile.mkdtemp(prefix="headless-ask-gate-", dir=str(managed_tempdir())))
    try:
        root = scratch / "repo"
        root.mkdir()
        with fixture_settings_home(scratch):
            sizing_rel = build_fixture(root, 0)
            env = dict(os.environ)
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2]) + os.pathsep + env.get("PYTHONPATH", "")
            out = scratch / "route.json"
            params = json.dumps({"sizing_path": sizing_rel, "run_id": RUN_ID})
            cmd = [
                sys.executable, "-m", "coordinator_core.benchmarks.headless_fire_ask_gate_route",
                "--child", "--repo", str(root), "--params", params,
                "--artifacts", str(scratch / "operations" / "1"), "--timeout", str(timeout), "--out", str(out),
            ]
            tree = single_invocation_tree_process_time(
                cmd, env=env, cwd=str(root),
                stdout_path=str(scratch / "child.stdout"), stderr_path=str(scratch / "child.stderr"),
            )
            if tree["rc"] != 0 or not out.is_file():
                tail = (scratch / "child.stderr").read_text(encoding="utf-8", errors="replace")[-1500:]
                raise RuntimeError(f"headless ask_gate child failed (rc={tree['rc']}): {tail}")
            child = json.loads(out.read_text(encoding="utf-8"))
            return {
                "route": child["route"],
                "process_ms": round(tree["process_time_ms"], 1),
                "spawns": child["spawns"],
                "error_code": child["error_code"],
                "bar_ms": BAR_MS,
                "timeout_secs": timeout,
                "procs": tree["procs"],
                "wall_ms": round(tree["wall_ms"], 1),
                "error_message": child["error_message"],
                "spawn_argv": child["spawn_argv"],
                "result_keys": child["result_keys"],
                "op_body_reached": child["error_code"] != UNSTAMPED_ENGINE_ROOT_CODE,
            }
    finally:
        rmtree_or_raise(scratch, label="headless-fire-ask-gate-route-bench")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECS, help="seconds before the call is killed and reported as error_code 'timeout'")
    ap.add_argument("--enforce-bar", action="store_true", help=f"exit 2 unless route is warm, process ms < {BAR_MS:.0f} and no error")
    ap.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--repo", help=argparse.SUPPRESS)
    ap.add_argument("--params", help=argparse.SUPPRESS)
    ap.add_argument("--artifacts", help=argparse.SUPPRESS)
    ap.add_argument("--out", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.child:
        return _child_main(args.repo, args.params, args.artifacts, args.timeout, args.out)
    from coordinator_core.benchmarks import declare_benchmark_origin

    declare_benchmark_origin()
    report = measure(args.timeout)
    print(json.dumps(report, indent=2, default=str))
    ok = report["route"] == "warm" and report["process_ms"] < BAR_MS and report["error_code"] is None
    return 2 if args.enforce_bar and not ok else 0


if __name__ == "__main__":
    sys.exit(main())
