"""Cold-route `dispatch.ask_gate` second-call cost over a state tree of thousands of warp run dirs.

Reproduces the incident shape: a run-bound sizing is routed by a first `dispatch.ask_gate` call, then a
second call for the same run arrives with no warm engine answering, so ipc runs it IN_PROCESS (the cold
route) in a fresh interpreter. Each call is its own child process (`--child`), billed as a whole
process tree by `process_time.single_invocation_tree_process_time` (job-object CPU, never wall clock),
so interpreter start, imports, the lock waits, the orphan-lookup thread join, the op body and the reply
land in one figure. A third call repeats the second while the parent holds the sizing's `locked_rmw` lock
(a peer mid engine swap), so the wait the bind spends on a held lock is located.

Per-phase figures come from inside the child: `process_ms` (CPU of the child, thread-inclusive) and
`wall_ms` (recorded only to locate waits; a CPU-idle wait shows as wall with no process time). Phases:
`interpreter_start` (CPU at main entry), `imports`, `lock_waits` (`ask_gate._clear_held_lock`, the gate's bounded
wait on a held sizing lock, plus any `locked_write.locked_rmw` call), `orphan_lookup` (`_bounded_orphan_lookup`, including its
thread join), `op_body` (`_gate_body` minus those two), `reply` (dispatch minus `_gate_body`).
`dominant_phase` names the phase with the most process time; the second call's is the finding.
The child's spawns are counted at the subprocess seam; the tree's `procs` includes the root.

Run: `python -m coordinator_core.benchmarks.ask_gate_cold_route_second_call [--dirs N] [--hold-secs S]
[--enforce-bar]`. Scratch lives in a managed temp dir this script removes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

#: The brightline process-time bar (CLAUDE.md § The brightline).
BAR_MS = 500.0
DEFAULT_DIRS = 4000
DEFAULT_HOLD_SECS = 1.0
SIZING_STEM = "2026-11-11-codex-ask-gate-cold-probe-d1bd92"
RUN_ID = "ask-20261111T051319-d1bd92"
#: Child phases, in the order a call spends them; `dominant_phase` is one of these.
PHASES = ("interpreter_start", "imports", "lock_waits", "orphan_lookup", "op_body", "reply")
#: The cold-await ceiling's reply code the incident died on.
HUNG_PAST_CEILING_CODE = -32004
_SESSION_ID = "11111111-2222-3333-4444-555555555555"
_FLOOR_DAY = "20261111"


def _stamp(i: int) -> str:
    day = _FLOOR_DAY if i % 5 == 0 else f"2026{10 if i % 2 else 9}{(i % 27) + 1:02d}"
    return f"{day}T{(i * 7) % 24:02d}{(i * 13) % 60:02d}{(i * 17) % 60:02d}-{i:06x}"


def build_fixture(root: Path, dirs: int) -> str:
    """Populate `root` (an empty dir): a git repo, one routed-ready sizing, the incident run's dir and
    `dirs` filler warp run dirs. Returns the sizing's repo-relative path."""
    import yaml

    from coordinator_core.git.run import run_git

    def git(*args: str) -> None:
        res = run_git(["-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t.example", *args])
        if not res.ok:
            raise RuntimeError(f"git {' '.join(args[:2])} failed (rc={res.returncode}): {res.stderr.strip()}")

    git("init", "-q", "-b", "main")
    (root / "README.md").write_text("seed\n", encoding="utf-8", newline="\n")
    sizing_rel = f"state/sizings/{SIZING_STEM}.yaml"
    sizing = root / sizing_rel
    sizing.parent.mkdir(parents=True)
    doc = {
        "schema": "sizing-object", "name": SIZING_STEM, "intent": "exercise the cold-route second call",
        "estimate": {"tshirt": "S", "provisional": True}, "route": "spec-dispatch", "detents": [],
        "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "deliverable_id": "dlv-gate-bench-abc123", "interaction_mode": "pm",
        "exit_criterion": {"statement": "done", "accepted": None},
    }
    sizing.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8", newline="\n")
    warp = root / "state" / "warp"
    run = warp / RUN_ID
    run.mkdir(parents=True)
    (run / "run.json").write_text("{}\n", encoding="utf-8", newline="\n")
    for i in range(dirs):
        stamp = _stamp(i)
        if i % 2:
            d, body = warp / f"ask-{stamp}", {"sizing_rel": f"state/sizings/2026-10-{(i % 27) + 1:02d}-other-{i % 50}.yaml"}
        else:
            d, body = warp / f"warp-2026-10-{(i % 27) + 1:02d}-other-{i % 50}-{stamp}", {}
        d.mkdir()
        if i % 3:
            (d / "run.json").write_text(json.dumps(body) + "\n", encoding="utf-8", newline="\n")
        if i % 7 == 0:
            (d / "parent.json").write_text("{}\n", encoding="utf-8", newline="\n")
    git("add", "-A")
    git("commit", "-q", "-m", "seed (ask gate cold route bench)")
    return sizing_rel


def _child_main(repo: str, params_json: str, out: str) -> int:
    """The cold route in a fresh interpreter: the same `ipc.dispatch_message` a cold-run call reaches,
    with the lock, orphan-lookup and body legs timed. Writes the phase report to `out`."""
    startup_cpu = time.process_time()
    import asyncio

    import pytest

    from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed

    legs: dict = {"lock_waits": [0.0, 0.0, 0], "orphan_lookup": [0.0, 0.0, 0], "gate_body": [0.0, 0.0, 0]}

    def timed(owner, attr: str, leg: str) -> None:
        real = getattr(owner, attr)

        def wrapper(*a, **k):
            c0, w0 = time.process_time(), time.perf_counter()
            try:
                return real(*a, **k)
            finally:
                legs[leg][0] += (time.process_time() - c0) * 1000.0
                legs[leg][1] += (time.perf_counter() - w0) * 1000.0
                legs[leg][2] += 1

        setattr(owner, attr, wrapper)

    c0, w0 = time.process_time(), time.perf_counter()
    from coordinator_core import ipc, locked_write
    from coordinator_core.ops.dispatch_emit import ask_gate

    imports = ((time.process_time() - c0) * 1000.0, (time.perf_counter() - w0) * 1000.0)
    ipc.allow_unstamped_dispatch()
    os.environ["COORDINATOR_SESSION_ID"] = _SESSION_ID
    os.environ.pop("DELIVERABLE_ID", None)
    timed(locked_write, "locked_rmw", "lock_waits")
    timed(ask_gate, "_clear_held_lock", "lock_waits")
    timed(ask_gate, "_bounded_orphan_lookup", "orphan_lookup")
    timed(ask_gate, "_gate_body", "gate_body")
    msg = {
        "jsonrpc": "2.0", "id": 1, "method": "dispatch.ask_gate", "params": json.loads(params_json),
        ipc._ORIGIN_WORKTREE_FIELD: repo,
    }
    mp = pytest.MonkeyPatch()
    with _count_spawns_attributed(mp) as spawns:
        c0, w0 = time.process_time(), time.perf_counter()
        envelope = asyncio.run(ipc.dispatch_message(msg))
        dispatch = ((time.process_time() - c0) * 1000.0, (time.perf_counter() - w0) * 1000.0)
    mp.undo()
    lock, orphan, body = legs["lock_waits"], legs["orphan_lookup"], legs["gate_body"]
    result = envelope.get("result") if isinstance(envelope.get("result"), dict) else {}
    error = envelope.get("error") if isinstance(envelope.get("error"), dict) else None
    report = {
        "phases": {
            "interpreter_start": {"process_ms": startup_cpu * 1000.0, "wall_ms": None},
            "imports": {"process_ms": imports[0], "wall_ms": imports[1]},
            "lock_waits": {"process_ms": lock[0], "wall_ms": lock[1], "calls": lock[2]},
            "orphan_lookup": {"process_ms": orphan[0], "wall_ms": orphan[1], "calls": orphan[2]},
            "op_body": {"process_ms": max(0.0, body[0] - lock[0] - orphan[0]), "wall_ms": max(0.0, body[1] - lock[1] - orphan[1])},
            "reply": {"process_ms": max(0.0, dispatch[0] - body[0]), "wall_ms": max(0.0, dispatch[1] - body[1])},
        },
        "spawns": len(spawns),
        "spawn_argv": [s.argv for s in spawns],
        "error_code": error.get("code") if error else None,
        "error_message": error.get("message") if error else None,
        "halt": (result.get("halt") or {}).get("kind") if isinstance(result.get("halt"), dict) else None,
        "bound_sizing_path": result.get("sizing_path"),
    }
    Path(out).write_text(json.dumps(report, default=str), encoding="utf-8", newline="\n")
    return 0


def _bill_call(root: Path, scratch: Path, label: str, sizing_rel: str, env: dict) -> dict:
    """One cold-route call as a child process: whole-tree process time from the job-object primitive, phases from the child."""
    from coordinator_core.benchmarks.process_time import single_invocation_tree_process_time

    out = scratch / f"{label}.phases.json"
    params = json.dumps({"sizing_path": sizing_rel, "run_id": RUN_ID})
    cmd = [
        sys.executable, "-m", "coordinator_core.benchmarks.ask_gate_cold_route_second_call",
        "--child", "--repo", str(root), "--params", params, "--out", str(out),
    ]
    tree = single_invocation_tree_process_time(
        cmd, env=env, cwd=str(root),
        stdout_path=str(scratch / f"{label}.stdout"), stderr_path=str(scratch / f"{label}.stderr"),
    )
    if tree["rc"] != 0 or not out.is_file():
        tail = (scratch / f"{label}.stderr").read_text(encoding="utf-8", errors="replace")[-1500:]
        raise RuntimeError(f"cold-route child {label} failed (rc={tree['rc']}): {tail}")
    child = json.loads(out.read_text(encoding="utf-8"))
    phases = child["phases"]
    dominant = max(PHASES, key=lambda n: phases[n]["process_ms"])
    return {
        "label": label,
        "process_ms": round(tree["process_time_ms"], 1),
        "procs": tree["procs"],
        "wall_ms": round(tree["wall_ms"], 1),
        "phases": {n: {k: (round(v, 1) if isinstance(v, float) else v) for k, v in p.items()} for n, p in phases.items()},
        "dominant_phase": dominant,
        "spawns": child["spawns"],
        "spawn_argv": child["spawn_argv"],
        "error_code": child["error_code"],
        "error_message": child["error_message"],
        "halt": child["halt"],
        "bound_sizing_path": child["bound_sizing_path"],
    }


def _call_under_peer_lock(root: Path, scratch: Path, label: str, sizing_rel: str, env: dict, hold_secs: float) -> dict:
    """The call with a peer holding the sizing's `locked_rmw` lock for `hold_secs` after the child starts."""
    from coordinator_core.locked_write import held_lock

    held, release, failure = threading.Event(), threading.Event(), []

    def peer() -> None:
        try:
            with held_lock(root / sizing_rel, anchor_root=root, holder_label="engine-swap-sim"):
                held.set()
                release.wait(hold_secs + 30.0)
        except BaseException as exc:  # noqa: BLE001 -- surfaced below, never swallowed
            failure.append(exc)
            held.set()

    thread = threading.Thread(target=peer, daemon=True)
    thread.start()
    held.wait(10.0)
    if failure:
        raise RuntimeError(f"peer lock not taken: {failure[0]!r}")
    timer = threading.Timer(hold_secs, release.set)
    timer.start()
    try:
        res = _bill_call(root, scratch, label, sizing_rel, env)
    finally:
        release.set()
        timer.cancel()
        thread.join(10.0)
    res["peer_hold_secs"] = hold_secs
    return res


def measure(dirs: int = DEFAULT_DIRS, hold_secs: float = DEFAULT_HOLD_SECS) -> dict:
    """Build the fixture, then bill the first (routing) call, the second call and the second call under a peer-held lock."""
    from coordinator_core.benchmarks import fixture_settings_home
    from coordinator_core.benchmarks.isolated_clone import rmtree_or_raise
    from coordinator_core.benchmarks.process_time import single_invocation_tree_process_time
    from coordinator_core.temp_layout import managed_tempdir

    scratch = Path(tempfile.mkdtemp(prefix="ask-gate-cold-", dir=str(managed_tempdir())))
    try:
        root = scratch / "repo"
        root.mkdir()
        with fixture_settings_home(scratch):
            sizing_rel = build_fixture(root, dirs)
            engine_root = str(Path(__file__).resolve().parents[2])
            env = dict(os.environ)
            env["PYTHONPATH"] = engine_root + os.pathsep + env.get("PYTHONPATH", "")
            floor = single_invocation_tree_process_time(
                [sys.executable, "-c", "pass"], env=env, cwd=str(root),
                stdout_path=str(scratch / "floor.stdout"), stderr_path=str(scratch / "floor.stderr"),
            )
            first = _bill_call(root, scratch, "first", sizing_rel, env)
            second = _bill_call(root, scratch, "second", sizing_rel, env)
            peer = _call_under_peer_lock(root, scratch, "second_peer_lock", sizing_rel, env, hold_secs)
            return {
                "dirs": dirs,
                "bar_ms": BAR_MS,
                "interpreter_floor_ms": round(floor["process_time_ms"], 1),
                "first_call": first,
                "second_call": second,
                "second_call_peer_lock": peer,
                "second_call_dominant_phase": second["dominant_phase"],
            }
    finally:
        rmtree_or_raise(scratch, label="ask-gate-cold-route-bench")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dirs", type=int, default=DEFAULT_DIRS)
    ap.add_argument("--hold-secs", type=float, default=DEFAULT_HOLD_SECS)
    ap.add_argument("--enforce-bar", action="store_true", help=f"exit 2 when a call's process ms >= {BAR_MS:.0f} or replies -32004")
    ap.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--repo", help=argparse.SUPPRESS)
    ap.add_argument("--params", help=argparse.SUPPRESS)
    ap.add_argument("--out", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.child:
        return _child_main(args.repo, args.params, args.out)
    from coordinator_core.benchmarks import declare_benchmark_origin

    declare_benchmark_origin()
    report = measure(args.dirs, args.hold_secs)
    print(json.dumps(report, indent=2, default=str))
    calls = (report["first_call"], report["second_call"], report["second_call_peer_lock"])
    over = any(c["process_ms"] >= BAR_MS or c["error_code"] == HUNG_PAST_CEILING_CODE for c in calls)
    return 2 if args.enforce_bar and over else 0


if __name__ == "__main__":
    sys.exit(main())
