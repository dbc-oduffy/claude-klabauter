"""Run-bound `dispatch.ask_gate` process-time benchmark over a state tree of thousands of warp run dirs.

Builds a tmp git repo with N run dirs under `state/warp` (a mix of `ask-<stamp>` and
`warp-<stem>-<stamp>` dirs, some with `run.json`, some with `parent.json`) and two run-bound
sizings plus a third, `plan_resume`, whose sizing names a plan with open rows (the gate then spends one
`git log --grep` over a history that matches nothing); one non-`-build` stem (the incident shape: `_credit_split_predecessors` returns before its
`parent.json` glob) and one `-build` stem. It calls `ask_gate._handler({"sizing_path", "run_id"}, root)`
in-process and reports process ms (`time.process_time`, never wall clock), spawn count and a per-leg
split: `_credit_split_predecessors`, `_newest_run_binding`, `_bounded_orphan_lookup`, `_bind_run` and
pack reads (`git_objects.read_object`). Legs nest (the orphan lookup and the predecessor credit both
call `_newest_run_binding`), so they overlap by design and do not sum to the total; `other_ms` is the
total minus the top-level legs.

The orphan lookup's wall bound is lifted for the measurement so its whole cost lands in the figure
instead of a daemon thread finishing after the timer stops.

Run: `python -m coordinator_core.benchmarks.ask_gate_run_bound_process_budget [--dirs N] [--runs N]
[--enforce-bar]`. Scratch lives in a managed temp dir this script removes.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import pytest
import yaml

from coordinator_core.benchmarks import declare_benchmark_origin, fixture_settings_home
from coordinator_core.benchmarks.isolated_clone import rmtree_or_raise
from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.git.run import run_git

#: The brightline process-time bar (CLAUDE.md § The brightline).
BAR_MS = 500.0
DEFAULT_DIRS = 4000
DEFAULT_RUNS = 3
_SESSION_ID = "11111111-2222-3333-4444-555555555555"
INCIDENT_STEM = "2026-11-10-codex-tier-name-probe-97c2c1"
#: shape name -> (sizing stem, run id bound to it).
SHAPES = {
    "non_build": (INCIDENT_STEM, "ask-20261110T132102-97c2c1"),
    "build": ("2026-11-10-codex-tier-name-probe-97c2c1-build", "ask-20261110T132103-97c2c2"),
    "plan_resume": ("2026-11-10-codex-tier-name-probe-97c2c1-plan", "ask-20261110T132104-97c2c3"),
}
#: The shape whose sizing carries a `plan:` edge to a plan with open rows: the gate then reads the
#: plan and spends its `git log` over the history on the checkpoint-landed rows.
PLAN_SHAPE = "plan_resume"
DEFAULT_COMMITS = 3000
#: leg name -> (module, attribute) wrapped where the caller looks it up.
LEGS = {
    "credit_split_predecessors": ("coordinator_core.ops.dispatch_emit.ask_gate", "_credit_split_predecessors"),
    "newest_run_binding": ("coordinator_core.ops.dispatch_emit.ask_gate", "_newest_run_binding"),
    "bounded_orphan_lookup": ("coordinator_core.ops.dispatch_emit.ask_gate", "_bounded_orphan_lookup"),
    "bind_run": ("coordinator_core.ops.dispatch_emit.ask_gate", "_bind_run"),
    "resumable_plan": ("coordinator_core.ops.dispatch_emit.sizing_fire", "resumable_plan"),
    "resume_landed_state": ("coordinator_core.ops.dispatch_emit.emit", "resume_landed_state"),
    "pack_read": ("coordinator_core.git.git_objects", "read_object"),
}
_TOP_LEVEL = ("credit_split_predecessors", "bounded_orphan_lookup", "bind_run", "resumable_plan")
_LIFTED_WALL_S = 600.0
_FLOOR_DAY = "20261110"


def _git(root: Path, *args: str) -> None:
    res = run_git(["-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t.example", *args])
    if not res.ok:
        raise RuntimeError(f"git {' '.join(args[:2])} failed (rc={res.returncode}): {res.stderr.strip()}")


def _sizing_doc(stem: str) -> dict:
    return {
        "schema": "sizing-object", "name": stem, "intent": "exercise the run-bound gate",
        "estimate": {"tshirt": "S", "provisional": True}, "route": "spec-dispatch", "detents": [],
        "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "deliverable_id": "dlv-gate-bench-abc123", "interaction_mode": "pm",
        "exit_criterion": {"statement": "done", "accepted": None},
    }


def _stamp(i: int) -> str:
    """A 22-char `YYYYMMDDTHHMMSS-xxxxxx` stamp; one run dir in five is stamped on the floor day."""
    day = _FLOOR_DAY if i % 5 == 0 else f"2026{10 if i % 2 else 9}{(i % 27) + 1:02d}"
    return f"{day}T{(i * 7) % 24:02d}{(i * 13) % 60:02d}{(i * 17) % 60:02d}-{i:06x}"


def _seed_history(root: Path, commits: int) -> None:
    """`commits` plain commits on `main` in one `git fast-import`, so the gate's `git log --grep`
    walks a history that matches nothing, the worst case for a `-n`-limited log."""
    lines = []
    for i in range(commits):
        msg = f"filler commit {i}"
        lines += ["commit refs/heads/main", f"committer t <t@t.example> {1_700_000_000 + i} +0000", f"data {len(msg)}", msg]
        if i == 0:
            lines.append("from refs/heads/main^0")
        lines.append("")
    stream = "\n".join(lines) + "\n"
    res = run_git(["-C", str(root), "fast-import", "--quiet", "--force"], input=stream.encode())
    if not res.ok:
        raise RuntimeError(f"git fast-import failed (rc={res.returncode}): {res.stderr.strip()}")



def build_fixture(root: Path, dirs: int, commits: int = DEFAULT_COMMITS) -> dict:
    """Populate `root` (an empty dir): a git repo, the two sizings and `dirs` warp run dirs.
    Returns `{shape: sizing_rel}`."""
    _git(root, "init", "-q", "-b", "main")
    (root / "README.md").write_text("seed\n", encoding="utf-8", newline="\n")
    sizings = {}
    for shape, (stem, run_id) in SHAPES.items():
        rel = f"state/sizings/{stem}.yaml"
        sizings[shape] = rel
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = _sizing_doc(stem)
        if shape == PLAN_SHAPE:
            plan_rel = f"docs/plans/{stem}.md"
            doc["plan"] = plan_rel
            plan = root / plan_rel
            plan.parent.mkdir(parents=True, exist_ok=True)
            rows = "".join(
                f"- id: A{n}\n  title: T{n}\n  disposition: open\n  writes:\n    - some/a{n}.py\n" for n in range(1, 4)
            )
            plan.write_text(
                f"---\nrun_id: fixture\n---\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n",
                encoding="utf-8", newline="\n",
            )
        path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8", newline="\n")
        run = root / "state" / "warp" / run_id
        run.mkdir(parents=True)
        (run / "run.json").write_text(json.dumps({}) + "\n", encoding="utf-8", newline="\n")
    warp = root / "state" / "warp"
    for i in range(dirs):
        stamp = _stamp(i)
        if i % 2:
            d = warp / f"ask-{stamp}"
            body = {"sizing_rel": f"state/sizings/2026-10-{(i % 27) + 1:02d}-other-{i % 50}.yaml"}
        else:
            d = warp / f"warp-2026-10-{(i % 27) + 1:02d}-other-{i % 50}-{stamp}"
            body = {}
        d.mkdir()
        if i % 3:
            (d / "run.json").write_text(json.dumps(body) + "\n", encoding="utf-8", newline="\n")
        if i % 7 == 0:
            (d / "parent.json").write_text("{}\n", encoding="utf-8", newline="\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed (ask gate bench)")
    if commits:
        _seed_history(root, commits)
    if commits:
        _seed_history(root, commits)
    return sizings


def _wrap_legs(mp: pytest.MonkeyPatch, totals: dict) -> None:
    import importlib

    for name, (modname, attr) in LEGS.items():
        owner = importlib.import_module(modname)
        real = getattr(owner, attr, None)
        if real is None:
            continue

        def timed(*a, _real=real, _name=name, **k):
            t0 = time.process_time()
            try:
                return _real(*a, **k)
            finally:
                totals[_name] = totals.get(_name, 0.0) + (time.process_time() - t0) * 1000.0

        mp.setattr(owner, attr, timed)


def _call_once(root: Path, sizing_rel: str, run_id: str) -> dict:
    from coordinator_core.ops.dispatch_emit import ask_gate

    totals: dict = {}
    mp = pytest.MonkeyPatch()
    try:
        mp.chdir(root)
        mp.setenv("COORDINATOR_SESSION_ID", _SESSION_ID)
        mp.delenv("DELIVERABLE_ID", raising=False)
        mp.setattr(ask_gate, "ORPHAN_LOOKUP_WALL_S", _LIFTED_WALL_S)
        _wrap_legs(mp, totals)
        spawn_mp = pytest.MonkeyPatch()
        with _count_spawns_attributed(spawn_mp) as spawns:
            t0 = time.process_time()
            reply = ask_gate._handler({"sizing_path": sizing_rel, "run_id": run_id}, root)
            total = (time.process_time() - t0) * 1000.0
    finally:
        mp.undo()
    return {
        "process_ms": total,
        "spawns": len(spawns),
        "spawn_argv": [s.argv for s in spawns],
        "legs": {n: round(totals.get(n, 0.0), 1) for n in LEGS if n in totals},
        "other_ms": round(max(0.0, total - sum(totals.get(n, 0.0) for n in _TOP_LEVEL)), 1),
        "reply": reply,
    }


def measure_shape(root: Path, shape: str, sizing_rel: str, runs: int = DEFAULT_RUNS) -> dict:
    """Cold call, then the median of `runs` repeat calls on the same run id; spawn-counted."""
    run_id = SHAPES[shape][1]
    cold = _call_once(root, sizing_rel, run_id)
    samples = [_call_once(root, sizing_rel, run_id) for _ in range(runs)]
    mid = sorted(samples, key=lambda s: s["process_ms"])[len(samples) // 2]
    names = [n for n in LEGS if any(n in s["legs"] for s in samples)]
    reply = mid["reply"]
    return {
        "shape": shape,
        "sizing_rel": sizing_rel,
        "cold_process_ms": round(cold["process_ms"], 1),
        "cold_legs_ms": cold["legs"],
        "process_ms": round(statistics.median(s["process_ms"] for s in samples), 1),
        "spawns": mid["spawns"],
        "spawn_argv": mid["spawn_argv"],
        "legs_ms": {n: round(statistics.median(s["legs"].get(n, 0.0) for s in samples), 1) for n in names},
        "other_ms": round(statistics.median(s["other_ms"] for s in samples), 1),
        "largest_leg": max(cold["legs"], key=cold["legs"].get) if cold["legs"] else None,
        "halt": (reply.get("halt") or {}).get("kind"),
        "arm": reply.get("arm"),
        "resume_plan": reply.get("resume_plan"),
        "bound_sizing_path": reply.get("sizing_path"),
    }


def measure(dirs: int = DEFAULT_DIRS, runs: int = DEFAULT_RUNS) -> dict:
    """Build the fixture and measure both shapes; returns `{"dirs": N, "shapes": {shape: report}}`."""
    from coordinator_core.temp_layout import make_managed_temp_dir

    scratch = make_managed_temp_dir(prefix="ask-gate-bench-")
    try:
        root = scratch / "repo"
        root.mkdir()
        with fixture_settings_home(scratch):
            sizings = build_fixture(root, dirs)
            return {
                "dirs": dirs,
                "shapes": {shape: measure_shape(root, shape, rel, runs) for shape, rel in sizings.items()},
            }
    finally:
        rmtree_or_raise(scratch, label="ask-gate-bench")


def main() -> int:
    declare_benchmark_origin()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dirs", type=int, default=DEFAULT_DIRS)
    ap.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    ap.add_argument("--enforce-bar", action="store_true", help=f"exit 2 when any shape's process ms >= {BAR_MS:.0f}")
    args = ap.parse_args()
    report = measure(args.dirs, args.runs)
    over = any(max(r["process_ms"], r["cold_process_ms"]) >= BAR_MS for r in report["shapes"].values())
    report["after"] = {"process_ms": max(r["process_ms"] for r in report["shapes"].values())}
    report["bar_ms"] = BAR_MS
    print(json.dumps(report, indent=2, default=str))
    return 2 if args.enforce_bar and over else 0


if __name__ == "__main__":
    sys.exit(main())
