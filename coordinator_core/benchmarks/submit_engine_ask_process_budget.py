"""Submit-path process-time benchmark: the engine-ask submit hook's default `evaluate`, end to end.

Builds a tmp git repo with one sizing, drives `submit_engine_ask.evaluate` (launch stubbed, default
emit) for a sizing submission and a raw-ask submission, and reports process ms (never wall clock),
spawn count and a per-phase split (sizing read, resume route, premint, emit, minted-run scan, the
rest). A phase is a named hook function wrapped for timing; one the hook no longer has is skipped.
`measure()` is the single entry the pin test drives. Figures are in-process CPU: a spawned child's
CPU is not in them, only its spawn count.

Run: `python -m coordinator_core.benchmarks.submit_engine_ask_process_budget [--runs N] [--enforce-bar]`.
Scratch lives in a managed temp dir this script removes.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import statistics
import sys
import tempfile
import time
from pathlib import Path

import pytest
import yaml

from coordinator_core.benchmarks import declare_benchmark_origin, fixture_settings_home
from coordinator_core.benchmarks.isolated_clone import rmtree_or_raise
from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.git.run import run_git

#: The brightline process-time bar for the submit path (CLAUDE.md § The brightline).
BAR_MS = 500.0
SIZING_REL = "state/sizings/2026-11-01-submit-budget-bench.yaml"
DEFAULT_RUNS = 5
_SESSION_ID = "11111111-2222-3333-4444-555555555555"

#: phase name -> hook function wrapped for timing. Nested phases (an emit that scans) double-count by design;
#: `other_ms` is the evaluate total minus the top-level phases only.
PHASES = {
    "sizing_read": "_load_sizing_doc",
    "resume_route": "_resume_route",
    "overlap_check": "_live_write_overlap_deny",
    "premint": "_premint_marker",
    "emit": "_default_emit",
    "minted_run_scan": "_run_dirs",
}
_TOP_LEVEL = ("sizing_read", "resume_route", "overlap_check", "premint", "emit")
#: Legs below `emit`, outside the hook: phase name -> (module, attribute) wrapped where the caller looks it up.
#: Importing the module is timed apart (`imports_ms`): the first import in a process is the cold cost.
EXTRA_PHASES = {
    "emit_dispatch": ("coordinator_core.ops.dispatch_emit.op", "_dispatch_emit_body"),
    "compose": ("coordinator_core.ops.dispatch_emit.ask_compose", "compose_ask_script"),
    "script_file": ("coordinator_core.ops.dispatch_emit.op", "_emit_script_file"),
    "contract_checks": ("coordinator_core.ops.dispatch_emit.op", "run_checks"),
}
#: Umbrella legs that contain other legs; never named as the largest cold leg.
_UMBRELLA = ("emit", "emit_dispatch", "script_file")


def _git(root: Path, *args: str) -> None:
    res = run_git(["-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t.example", *args])
    if not res.ok:
        raise RuntimeError(f"git {' '.join(args[:2])} failed (rc={res.returncode}): {res.stderr.strip()}")


def build_fixture(root: Path) -> None:
    """Populate `root` (an empty dir): a git repo with one committed S sizing and nothing else."""
    sizing = {
        "schema": "sizing-object", "name": "submit budget bench", "intent": "exercise the submit path",
        "estimate": {"tshirt": "S", "provisional": True}, "route": "spec-dispatch", "detents": [],
        "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "deliverable_id": "dlv-submit-budget-bench-abc123", "interaction_mode": "pm",
        "exit_criterion": {"statement": "done", "accepted": None},
    }
    _git(root, "init", "-q", "-b", "main")
    (root / "README.md").write_text("seed\n", encoding="utf-8", newline="\n")
    (root / SIZING_REL).parent.mkdir(parents=True, exist_ok=True)
    (root / SIZING_REL).write_text(yaml.safe_dump(sizing, sort_keys=False), encoding="utf-8", newline="\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed (submit budget bench)")


def _payload(repo: Path, args: dict) -> dict:
    return {
        "hook_event_name": "PreToolUse", "tool_name": "Workflow", "session_id": _SESSION_ID,
        "cwd": str(repo), "tool_use_id": "bench-submit-path",
        "tool_input": {"name": "coordinator:engine-ask", "args": args},
    }


SHAPES = {
    "sizing": lambda repo: _payload(repo, {"sizing": SIZING_REL}),
    "ask": lambda repo: _payload(repo, {"ask": "bench: exercise the raw-ask submit path", "source": "relayed"}),
}
#: Record-write primitives the hook path calls: (module, attribute). One call is one durable write.
WRITE_PRIMITIVES = (
    ("coordinator_core.ops.dispatch_emit.run_token", "mint"),
    ("coordinator_core.hooks.submit_engine_ask", "_write_deferred_script"),
    ("coordinator_core.ops.dispatch_emit.ask_fanout", "_stage_text"),
)
_EM_CONTEXT = ("EM context line: constraint, live checkout, land files atomically. " * 30)[:2048]


def shape_payload(repo: Path, shape: str) -> dict:
    """The hook payload of `shape`: a `SHAPES` key, or `asks:<N>` for one N-entry `asks` submission."""
    if shape in SHAPES:
        return SHAPES[shape](repo)
    count = int(shape.removeprefix("asks:"))
    return _payload(repo, {"asks": [
        {"ask": f"bench job {i}", "label": f"job-{i}", "writes": [f"pkg/m{i}.py"], "em_context": _EM_CONTEXT}
        for i in range(count)
    ]})


@contextlib.contextmanager
def _counted_writes(mp: pytest.MonkeyPatch, counts: dict):
    """Wrap each record-write primitive so every call adds one to `counts[attribute]`."""

    def wrap(owner, attr):
        real = getattr(owner, attr, None)
        if real is None:
            return

        def counted(*a, _real=real, _attr=attr, **k):
            counts[_attr] = counts.get(_attr, 0) + 1
            return _real(*a, **k)

        mp.setattr(owner, attr, counted)

    for modname, attr in WRITE_PRIMITIVES:
        wrap(importlib.import_module(modname), attr)
    yield


@contextlib.contextmanager
def _timed_phases(mp: pytest.MonkeyPatch, hook, totals: dict, extra: dict | None = None, imports: dict | None = None):
    """Wrap each present phase function (in `hook`, and in `extra` modules) so its process time accumulates
    into `totals[name]`; a module first imported here adds its import process ms to `imports[module]`."""

    def wrap(owner, attr, name):
        real = getattr(owner, attr, None)
        if real is None:
            return

        def timed(*a, _real=real, _name=name, **k):
            t0 = time.process_time()
            try:
                return _real(*a, **k)
            finally:
                totals[_name] = totals.get(_name, 0.0) + (time.process_time() - t0) * 1000.0

        mp.setattr(owner, attr, timed)

    for name, attr in PHASES.items():
        wrap(hook, attr, name)
    for name, (modname, attr) in (extra or {}).items():
        first = modname not in sys.modules
        t0 = time.process_time()
        mod = importlib.import_module(modname)
        if first and imports is not None:
            imports[modname] = round((time.process_time() - t0) * 1000.0, 1)
        wrap(mod, attr, name)
    yield


def _call_once(repo: Path, shape: str, counted: bool = False) -> dict:
    """One `evaluate` of `shape` on a fresh run state; returns ms, spawns, phases, decision."""
    from coordinator_core.hooks import submit_engine_ask as hook

    launched: list = []

    def stub_launch(*a, **k):
        launched.append((a, k))
        return repo / "scratch" / "bench-launcher.workflow.mjs"

    payload = shape_payload(repo, shape)
    totals: dict = {}
    imports: dict = {}
    writes: dict = {}
    mp = pytest.MonkeyPatch()
    try:
        mp.chdir(repo)
        mp.setenv("COORDINATOR_SESSION_ID", _SESSION_ID)
        mp.delenv("DELIVERABLE_ID", raising=False)
        spawn_mp = pytest.MonkeyPatch()
        with _timed_phases(mp, hook, totals, EXTRA_PHASES, imports), _counted_writes(mp, writes), \
                _count_spawns_attributed(spawn_mp) as spawns:
            t0 = time.process_time()
            out = hook.evaluate(payload, launch=stub_launch)
            total = (time.process_time() - t0) * 1000.0
    finally:
        mp.undo()
    hso = out.get("hookSpecificOutput") or {}
    phases = {name: round(totals.get(name, 0.0), 1) for name in [*PHASES, *EXTRA_PHASES] if name in totals}
    return {
        "process_ms": total + sum(imports.values()),
        "imports_ms": imports,
        "durable_writes": sum(writes.values()),
        "writes_by_primitive": writes,
        "spawns": len(spawns),
        "spawn_argv": [s.argv for s in spawns],
        "phases": phases,
        "other_ms": round(max(0.0, total - sum(totals.get(n, 0.0) for n in _TOP_LEVEL)), 1),
        "decision": hso.get("permissionDecision"),
        "reason": hso.get("permissionDecisionReason"),
        "launch_stub_called": bool(launched),
    }


def _reset(repo: Path) -> None:
    """Drop what the previous call minted so each measured call starts from the committed fixture."""
    _git(repo, "clean", "-fdxq", "--", "state/warp", "scratch", ".coordinator-local")
    _git(repo, "checkout", "-q", "HEAD", "--", SIZING_REL)


def measure_shape(repo: Path, shape: str, runs: int = DEFAULT_RUNS) -> dict:
    """Cold call, then the median of `runs` warm calls (spawn-counted), each on a reset fixture."""
    _reset(repo)
    cold = _call_once(repo, shape)
    samples = []
    for _ in range(runs):
        _reset(repo)
        samples.append(_call_once(repo, shape))
    ordered = sorted(samples, key=lambda s: s["process_ms"])
    mid = ordered[len(ordered) // 2]
    phase_names = sorted({n for s in samples for n in s["phases"]}, key=[*PHASES, *EXTRA_PHASES].index)
    legs = {**cold["phases"], **{f"import:{m}": ms for m, ms in cold["imports_ms"].items()}}
    named = {k: v for k, v in legs.items() if k not in _UMBRELLA}
    return {
        "shape": shape,
        "cold_process_ms": round(cold["process_ms"], 1),
        "cold_phases_ms": cold["phases"],
        "cold_imports_ms": cold["imports_ms"],
        "cold_largest_leg": max(named, key=named.get) if named else None,
        "cold_spawns": cold["spawns"],
        "warm_process_ms": round(statistics.median(s["process_ms"] for s in samples), 1),
        "process_ms": round(mid["process_ms"], 1),
        "spawns": mid["spawns"],
        "durable_writes": mid["durable_writes"],
        "writes_by_primitive": mid["writes_by_primitive"],
        "spawn_argv": mid["spawn_argv"],
        "phases_ms": {n: round(statistics.median(s["phases"].get(n, 0.0) for s in samples), 1) for n in phase_names},
        "other_ms": round(statistics.median(s["other_ms"] for s in samples), 1),
        "decision": mid["decision"],
        "reason": mid["reason"],
        "reached_emit_or_launch": bool(mid["launch_stub_called"] or "emit" in mid["phases"] or "premint" in mid["phases"]),
    }


def measure(runs: int = DEFAULT_RUNS, asks: int | None = None) -> dict:
    """Build the fixture and measure every submit shape (or, with `asks`, only one `asks`-entry submission);
    returns `{shape: report}`."""
    shapes = [f"asks:{asks}"] if asks else list(SHAPES)
    scratch = Path(tempfile.mkdtemp(prefix="submit-budget-bench-", dir=_scratch_parent()))
    try:
        root = scratch / "repo"
        root.mkdir()
        build_fixture(root)
        with fixture_settings_home(scratch):
            return {shape: measure_shape(root, shape, runs) for shape in shapes}
    finally:
        rmtree_or_raise(scratch, label="submit-budget-bench")


def _scratch_parent() -> str:
    from coordinator_core.temp_layout import managed_tempdir

    return str(managed_tempdir())


def main() -> int:
    declare_benchmark_origin()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    ap.add_argument("--asks", type=int, default=None, help="measure one N-ask submission instead of the sizing and ask shapes")
    ap.add_argument("--enforce-bar", action="store_true", help=f"exit 2 when any shape's cold or warm process ms >= {BAR_MS:.0f}")
    args = ap.parse_args()
    report = measure(args.runs, args.asks)
    over = False
    for shape, res in report.items():
        print(f"== {shape}")
        for key, value in res.items():
            if key != "spawn_argv":
                print(f"{key}: {value}")
        for argv in res["spawn_argv"]:
            print(f"spawn: {' '.join(argv)[:160]}")
        shape_over = max(res["warm_process_ms"], res["cold_process_ms"]) >= BAR_MS
        over = over or shape_over
        print(f"bar: {'OVER' if shape_over else 'UNDER'} {BAR_MS:.0f}ms process time (cold {res['cold_process_ms']}, warm {res['warm_process_ms']})")
    return 2 if args.enforce_bar and over else 0


if __name__ == "__main__":
    sys.exit(main())
