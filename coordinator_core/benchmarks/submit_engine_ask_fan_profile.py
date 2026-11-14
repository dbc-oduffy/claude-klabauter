"""Per-leg process-time profile of the submit hook's `evaluate` over the 14-ask fan payload.

Same payload as `tests/test_submit_engine_ask_fan_process_budget.py` (14 independent asks, tmp repo, 200
PM turns). Each leg is wrapped with `time.process_time`; a leg's time is inclusive of the legs nested in it
(`run_checks` ... `mint` sit inside `emit`). Prints process ms, spawn count, and per-leg calls / total ms.
`--mode deferred` marks every child with the hook's defer key, so `_default_launch` writes 14 deferred-emit
scripts instead of emitting: the measure of the rebuild shape. `--cold-import` also reports the import cost of
`dispatch_emit.op` in a fresh interpreter (outside `evaluate`).
Run: `python -m coordinator_core.benchmarks.submit_engine_ask_fan_profile [--asks N] [--mode base|deferred] [--cold-import]`.
Scratch lives in a temp dir this script removes.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

import pytest

from coordinator_core.benchmarks import declare_benchmark_origin, fixture_settings_home
from coordinator_core.benchmarks.isolated_clone import rmtree_or_raise

SID = "sid-submit-fan-profile"

#: (leg name, module path, attribute) wrapped with a process-time accumulator, in report order.
LEGS = (
    ("fanout", "coordinator_core.hooks.submit_engine_ask", "_default_fanout"),
    ("sizing_load", "coordinator_core.hooks.submit_engine_ask", "_load_sizing_docs"),
    ("emit (per child, inclusive)", "coordinator_core.hooks.submit_engine_ask", "_default_emit"),
    ("  run_checks", "coordinator_core.ops.dispatch_emit.op", "run_checks"),
    ("  compose_ask_script", "coordinator_core.ops.dispatch_emit.ask_compose", "compose_ask_script"),
    ("  requirements_bind", "coordinator_core.ops.dispatch_emit.op", "_start_requirements"),
    ("  mint (run token)", "coordinator_core.ops.dispatch_emit.run_token", "mint"),
    ("write_deferred_script", "coordinator_core.hooks.submit_engine_ask", "_write_deferred_script"),
    ("write_launcher", "coordinator_core.ops.dispatch_emit.fan_launcher", "write_launcher"),
    ("record_submission", "coordinator_core.warp_live", "record_submission"),
)

_COLD_IMPORT_SRC = (
    "import time; t=time.process_time(); import coordinator_core.ops.dispatch_emit.op; "
    "print((time.process_time()-t)*1000)"
)


def _wrap_legs(mp: pytest.MonkeyPatch) -> dict:
    """Wrap each leg with a process-time accumulator. Returns `{leg: [calls, process_s]}`."""
    import importlib

    totals: dict = {}
    for label, modname, attr in LEGS:
        owner = importlib.import_module(modname)
        original = getattr(owner, attr)
        totals[label] = [0, 0.0]

        def make(fn, slot):
            def timed(*a, **k):
                t0 = time.process_time()
                try:
                    return fn(*a, **k)
                finally:
                    slot[0] += 1
                    slot[1] += time.process_time() - t0

            return timed

        mp.setattr(owner, attr, make(original, totals[label]))
    return totals


def cold_import_ms() -> float:
    """Process time of importing `dispatch_emit.op` in a fresh interpreter."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    out = subprocess.run(
        [sys.executable, "-c", _COLD_IMPORT_SRC], capture_output=True, text=True, check=True, creationflags=flags
    )
    return float(out.stdout.strip())


def profile(asks: int = 14, mode: str = "base") -> dict:
    """One `evaluate` over `asks` independent asks; returns elapsed process ms, spawn list length, per-leg split."""
    from coordinator_core import warp_fire
    from coordinator_core import warp_live
    from coordinator_core.hooks import submit_engine_ask as mod
    from coordinator_core.ops import baton_pm_turns as PT
    from coordinator_core.session_baton import store
    from coordinator_core.temp_layout import make_managed_temp_dir

    scratch = make_managed_temp_dir(prefix="fan-profile-")
    mp = pytest.MonkeyPatch()
    try:
        managed = scratch / "managed"
        managed.mkdir()
        mp.setattr(warp_live, "managed_tempdir", lambda: managed)
        mp.setattr(warp_live, "legacy_top_level_path", lambda name: managed / name)
        for name in ("CLAUDE_CODE_REMOTE", "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_CONTAINER_ID",
                     "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PLUGIN_ROOT", "DELIVERABLE_ID"):
            mp.delenv(name, raising=False)
        mp.setenv("COORDINATOR_AGENT_TYPE_HOST", "coordinator")
        mp.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
        root = scratch / "repo"
        (root / "state" / "sizings").mkdir(parents=True)
        (root / ".git").mkdir()
        (scratch / "hub" / SID).mkdir(parents=True)
        mp.setattr(store, "baton_dir", lambda s, cwd=None: scratch / "hub" / s)
        for i in range(200):
            assert PT.append_turn(f"pm turn number {i} with some words", session_id=SID)["ok"]

        spawns: list = []
        real_popen = subprocess.Popen

        def counting_popen(*a, **k):
            spawns.append(a)
            return real_popen(*a, **k)

        mp.setattr(subprocess, "Popen", counting_popen)
        mp.setattr(os, "system", lambda *a, **k: spawns.append(a))

        if mode == "deferred":
            real_child_params = mod._child_params
            mp.setattr(mod, "_child_params", lambda *a, **k: {**real_child_params(*a, **k), mod._DEFER_EMIT_KEY: True})
        totals = _wrap_legs(mp)

        items = [{"ask": f"job {i}", "source": "pm", "label": f"j{i}", "writes": [f"d{i}"]} for i in range(asks)]
        payload = {
            "tool_name": "Workflow",
            "tool_input": {"name": warp_fire.SUBMIT_WORKFLOW_NAME, "args": {"asks": items}},
            "session_id": SID,
            "transcript_path": str(root / "transcript.jsonl"),
            "cwd": str(root),
        }
        start = time.process_time()
        env = mod.evaluate(payload)
        elapsed_ms = (time.process_time() - start) * 1000
        decision = env["hookSpecificOutput"]["permissionDecision"]
        return {
            "mode": mode,
            "asks": asks,
            "decision": decision,
            "process_ms": round(elapsed_ms, 1),
            "spawns": len(spawns),
            "legs": {k: (v[0], round(v[1] * 1000, 1)) for k, v in totals.items()},
        }
    finally:
        mp.undo()
        rmtree_or_raise(scratch, label="fan-profile")


def main() -> None:
    declare_benchmark_origin()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--asks", type=int, default=14)
    ap.add_argument("--mode", choices=("base", "deferred"), default="base")
    ap.add_argument("--cold-import", action="store_true")
    args = ap.parse_args()
    from coordinator_core.temp_layout import managed_temp_dir

    with managed_temp_dir(prefix="fan-profile-home-") as home, fixture_settings_home(home):
        result = profile(args.asks, args.mode)
    legs = result.pop("legs")
    for key, value in result.items():
        print(f"{key}: {value}")
    print("legs (calls, process ms; nested legs are inside emit):")
    for name, (calls, ms) in legs.items():
        print(f"  {name:<32} calls={calls:<3} {ms:9.1f}ms")
    if args.cold_import:
        print(f"cold import dispatch_emit.op (fresh interpreter): {cold_import_ms():.1f}ms")


if __name__ == "__main__":
    main()
