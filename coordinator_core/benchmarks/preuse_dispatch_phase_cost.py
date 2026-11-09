"""Preuse dispatch phase-cost benchmark: process ms, wall ms and spawns per R3 shape.

Drives the registered `hooks.preuse_bash_dispatch` / `hooks.preuse_write_dispatch`
handlers at default arguments, in-process, under tmp dirs (a throwaway git repo is the
payload cwd; nothing touches the live checkout). Process ms is `process_time` (the
chain runs in a worker thread); spawns count `subprocess.Popen`
constructions during the call.

Trap: `process_time` reads ~0 for a phase blocked on disk, a lock or a child, so wall ms
sits beside it and a shape whose wall dwarfs its process ms is a stall, not work.

Forced-slow case: one phase is slept `FORCED_SLOW_SLEEP_S` while `dispatch_abandon.DEADLINE`
is bound `FORCED_DEADLINE_S` out. `returned_by_deadline` is true only when the call returned
before the sleep finished plus the deadline allowance; at a base without the deadline stop it
is false and `slow_phase` is absent.

Usage: python coordinator_core/benchmarks/preuse_dispatch_phase_cost.py [--n N] [--json]
Exit 0 on a completed run; the 500ms bar is reported per shape (`under_bar`), not enforced.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[2]
if importlib.util.find_spec("coordinator_core") is None:
    sys.path.insert(0, str(_REPO_ROOT))

BAR_MS = 500.0
FORCED_SLOW_SLEEP_S = 1.5
FORCED_DEADLINE_S = 0.4
# Slack on top of the deadline for a handler that stops at its deadline to unwind and reply.
DEADLINE_RETURN_SLACK_S = 0.5

_BASH = "hooks.preuse_bash_dispatch"
_WRITE = "hooks.preuse_write_dispatch"


def _git_init(root: Path) -> None:
    subprocess.run(
        ["git", "init", "-q", str(root)],
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _build_fixture(root: Path) -> Dict[str, Any]:
    """Tmp repo holding the files R3's shapes name; returns paths."""
    _git_init(root)
    data = root / "data"
    data.mkdir()
    jsonl = data / "events.jsonl"
    jsonl.write_text("\n".join(json.dumps({"i": i, "k": "v"}) for i in range(200)) + "\n", encoding="utf-8")
    reports = root / "scratch" / "warp" / "run-1"
    reports.mkdir(parents=True)
    for i in range(5):
        (reports / f"planning-report-{i}.md").write_text(f"# report {i}\n", encoding="utf-8")
    edit_target = reports / "planning-report.md"
    edit_target.write_text("# planning\nold line\n", encoding="utf-8")
    share = root / ".coordinator-local" / "subagent-share" / "dispatch-reports" / "run-1"
    share.mkdir(parents=True)
    (share / "existing.md").write_text("# existing\nold line\n", encoding="utf-8")
    return {"root": root, "jsonl": jsonl, "reports": reports, "edit": edit_target, "share": share}


def _shapes(fx: Dict[str, Any]) -> List[Dict[str, Any]]:
    root = str(fx["root"])
    return [
        {"shape": "bash python -c over jsonl", "op": _BASH, "tool": "Bash", "input": {
            "command": f"python3 -c \"import json,sys; print(sum(1 for _ in open(sys.argv[1])))\" {fx['jsonl']}"}},
        {"shape": "bash for-loop over md files", "op": _BASH, "tool": "Bash", "input": {
            "command": f"for f in {fx['reports']}/planning-report-*.md; do head -1 \"$f\"; done"}},
        {"shape": "bash ls/find", "op": _BASH, "tool": "Bash", "input": {
            "command": f"ls -la {root}/data && find {root}/scratch -name '*.md'"}},
        {"shape": "write Edit scratch md", "op": _WRITE, "tool": "Edit", "input": {
            "file_path": str(fx["edit"]), "old_string": "old line", "new_string": "new line"}},
        {"shape": "write Write subagent-share md", "op": _WRITE, "tool": "Write", "input": {
            "file_path": str(fx["share"] / "C4.md"), "content": "# report\nstatus: DONE\n"}},
    ]


def _payload(spec: Dict[str, Any], cwd: str, session_id: str) -> Dict[str, Any]:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": spec["tool"],
        "tool_input": spec["input"],
        "session_id": session_id,
        "cwd": cwd,
    }


class _SpawnCounter:
    """Counts `subprocess.Popen` constructions while installed."""

    def __init__(self) -> None:
        self.count = 0
        self._orig: Optional[Callable[..., Any]] = None

    def __enter__(self) -> "_SpawnCounter":
        self._orig = subprocess.Popen.__init__
        counter = self
        orig = self._orig

        def counted(popen_self: Any, *a: Any, **kw: Any) -> None:
            counter.count += 1
            orig(popen_self, *a, **kw)

        subprocess.Popen.__init__ = counted  # type: ignore[method-assign]
        return self

    def __exit__(self, *exc: object) -> None:
        subprocess.Popen.__init__ = self._orig  # type: ignore[method-assign]


def _handler(op: str) -> Callable[..., Any]:
    from coordinator_core.ipc import get_op_handler

    h = get_op_handler(op)
    if h is None:
        raise RuntimeError(f"op not registered: {op}")
    return h


def _call(op: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """One in-process handler call; returns timings, spawns and the drained row fields."""
    from coordinator_core.telemetry import op_row_fields

    handler = _handler(op)
    token = op_row_fields.begin()
    spawns = _SpawnCounter()
    w0 = time.perf_counter()
    c0 = time.process_time()
    error = None
    try:
        with spawns:
            handler(payload, None)
    except Exception as exc:  # noqa: BLE001 -- a raise is a measured outcome
        error = f"{type(exc).__name__}: {exc}"
    process_ms = (time.process_time() - c0) * 1000.0
    wall_ms = (time.perf_counter() - w0) * 1000.0
    fields = op_row_fields.drain(token)
    return {
        "process_ms": round(process_ms, 1),
        "wall_ms": round(wall_ms, 1),
        "spawns": spawns.count,
        "slow_phase": fields.get("slow_phase"),
        "spans": fields.get("spans"),
        "legs": fields.get("legs"),
        "error": error,
    }


def measure_shape(spec: Dict[str, Any], cwd: str, n: int) -> Dict[str, Any]:
    """First call (cold: imports, discovery) then `n` steady calls; steady figures are medians."""
    cold = _call(spec["op"], _payload(spec, cwd, "bench-cold"))
    steady = [_call(spec["op"], _payload(spec, cwd, f"bench-{i}")) for i in range(n)]
    med = lambda key: round(statistics.median(s[key] for s in steady), 1)  # noqa: E731
    last = steady[-1]
    return {
        "shape": spec["shape"],
        "op": spec["op"],
        "cold_process_ms": cold["process_ms"],
        "cold_wall_ms": cold["wall_ms"],
        "process_ms": med("process_ms"),
        "wall_ms": med("wall_ms"),
        "spawns": max(s["spawns"] for s in steady),
        "slow_phase": last["slow_phase"],
        "legs": last["legs"],
        "error": cold["error"] or last["error"],
        "under_bar": med("process_ms") < BAR_MS and cold["process_ms"] < BAR_MS,
    }


@contextlib.contextmanager
def _slow_one_phase(op: str, sleep_s: float):
    """Sleep once inside the first guard phase of `op`, via the seam each chain loops through."""
    fired = {"done": False}

    def nap() -> None:
        if not fired["done"]:
            fired["done"] = True
            time.sleep(sleep_s)

    if op == _BASH:
        from coordinator_core.bash_guards import dispatch as bdispatch

        real_leg = bdispatch._leg

        @contextlib.contextmanager
        def slow_leg(name: str):
            with real_leg(name):
                if name != "claims":
                    nap()
                yield

        bdispatch._leg = slow_leg
        try:
            yield
        finally:
            bdispatch._leg = real_leg
    else:
        from coordinator_core.write_guards import engine

        real_check = engine._run_check

        def slow_check(guard: Any, payload: Dict[str, Any]) -> Any:
            nap()
            return real_check(guard, payload)

        engine._run_check = slow_check
        try:
            yield
        finally:
            engine._run_check = real_check


def measure_forced_slow(spec: Dict[str, Any], cwd: str) -> Dict[str, Any]:
    """Bind a short DEADLINE, slow one phase, record when the call returns."""
    from coordinator_core import dispatch_abandon

    _call(spec["op"], _payload(spec, cwd, "bench-warm"))
    with _slow_one_phase(spec["op"], FORCED_SLOW_SLEEP_S):
        token = dispatch_abandon.DEADLINE.set(time.monotonic() + FORCED_DEADLINE_S)
        try:
            r = _call(spec["op"], _payload(spec, cwd, "bench-forced"))
        finally:
            dispatch_abandon.DEADLINE.reset(token)
    return {
        "shape": f"forced-slow {spec['op']}",
        "op": spec["op"],
        "deadline_s": FORCED_DEADLINE_S,
        "slept_s": FORCED_SLOW_SLEEP_S,
        "return_wall_ms": r["wall_ms"],
        "returned_by_deadline": r["wall_ms"] / 1000.0 <= FORCED_DEADLINE_S + DEADLINE_RETURN_SLACK_S,
        "slow_phase": r["slow_phase"],
        "error": r["error"],
    }


def run(n: int = 5) -> Dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="preuse-phase-cost-") as td:
        root = Path(td).resolve() / "repo"
        root.mkdir()
        fx = _build_fixture(root)
        specs = _shapes(fx)
        cwd = str(root)
        shapes = [measure_shape(s, cwd, n) for s in specs]
        forced = [
            measure_forced_slow(next(s for s in specs if s["op"] == _BASH), cwd),
            measure_forced_slow(next(s for s in specs if s["op"] == _WRITE), cwd),
        ]
    return {"head": _head(), "n": n, "bar_ms": BAR_MS, "shapes": shapes, "forced_slow": forced}


def _head() -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "rev-parse", "--short=10", "HEAD"],
            capture_output=True, text=True, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return out.stdout.strip()
    except OSError:
        return ""


def format_report(result: Dict[str, Any]) -> str:
    lines = [
        f"head={result['head']} n={result['n']} bar={result['bar_ms']:.0f}ms (process time, process_time)",
        "%-32s %9s %9s %9s %9s %7s %-14s %s"
        % ("shape", "cold_proc", "proc_ms", "cold_wall", "wall_ms", "spawns", "slow_phase", "under_bar"),
    ]
    for s in result["shapes"]:
        lines.append(
            "%-32s %9.1f %9.1f %9.1f %9.1f %7d %-14s %s%s"
            % (s["shape"], s["cold_process_ms"], s["process_ms"], s["cold_wall_ms"], s["wall_ms"],
               s["spawns"], s["slow_phase"] or "-", s["under_bar"], f"  ERR {s['error']}" if s["error"] else "")
        )
    lines.append("")
    for f in result["forced_slow"]:
        lines.append(
            "%-34s slept=%.1fs deadline=%.1fs return_wall_ms=%.1f returned_by_deadline=%s slow_phase=%s%s"
            % (f["shape"], f["slept_s"], f["deadline_s"], f["return_wall_ms"], f["returned_by_deadline"],
               f["slow_phase"] or "-", f"  ERR {f['error']}" if f["error"] else "")
        )
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    from coordinator_core.benchmarks import declare_benchmark_origin

    declare_benchmark_origin()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=5)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = run(n=args.n)
    print(json.dumps(result, indent=2) if args.json else format_report(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
