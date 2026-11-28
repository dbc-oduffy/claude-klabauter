"""Profile `targeted-test-run provision` at default arguments against a subject repo: exit, stderr, per-git-call
process time and spawn count, then reap the clone.

The subject repo is read-only except for the run's cone file under its bug-blitz scratch run directory, which
the submit hook writes in production and this script removes afterwards. `--targets` lists failing tests
(`path/to/test_x.py::name`, one per line; `#` comments and blanks skipped); the cone is each file's top-level
directory. Process time is os.times() children CPU around each git call, never wall clock. Prints one JSON line.
Runs as `python -m coordinator_core.benchmarks.bug_blitz_provision_profile --repo R --targets F`.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_ENGINE_ROOT = Path(__file__).resolve().parents[2]
_OUTER_DEADLINE_S = 900

# Child body: wraps worker_clone.run_git to record each git call, then runs the production provision CLI entry.
_CHILD = r"""
import json, os, sys, time
from coordinator_core.bug_blitz import worker_clone, targeted_run
from coordinator_core.telemetry import spawn_counter

out_path = sys.argv[1]
real = worker_clone.run_git
calls = []

from coordinator_core import ipc

bucket = []
ipc._op_cpu_secs_var.set(bucket)

def _cpu_ms():
    # Windows: run_git charges each git Job's user+kernel time to the bucket; POSIX: os.times children.
    if sys.platform == "win32":
        return sum(bucket) * 1000.0
    t = os.times()
    return (t.children_user + t.children_system) * 1000.0

def wrapped(args, **kw):
    before, t0 = _cpu_ms(), time.monotonic()
    res = real(args, **kw)
    calls.append({
        "git": " ".join(str(a) for a in args[:3]),
        "rc": res.returncode,
        "timed_out": bool(res.timed_out),
        "process_ms": round(_cpu_ms() - before, 1),
        "wall_ms": round((time.monotonic() - t0) * 1000.0, 1),
    })
    return res

worker_clone.run_git = wrapped
rc = targeted_run.main(["provision", *sys.argv[2:]])
with open(out_path, "w", encoding="utf-8") as fh:
    json.dump({"calls": calls, "spawn_count": spawn_counter.spawn_count()}, fh)
sys.exit(rc)
"""


def cone_dirs(targets_text: str) -> list[str]:
    """Top-level directory of each target's file, first-seen order, root-level files naming none: the production
    filter, `hooks.submit_bug_blitz._cone_dirs`, applied to each listed file."""
    from types import SimpleNamespace

    from coordinator_core.hooks.submit_bug_blitz import _cone_dirs

    tests = []
    for raw in targets_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("FAILED "):
            line = line[len("FAILED "):]
        tests.append(SimpleNamespace(file=line.split("::", 1)[0].strip()))
    return _cone_dirs(tuple(tests))


def _no_console() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def profile(repo: Path, targets: Path, *, no_cone: bool = False) -> dict:
    """Run provision against `repo` once; the result dict never raises on a provision failure. `no_cone` records
    no cone file, so provision takes the unresolved-cone path (whole tree up to its file cap, else root files)."""
    from coordinator_core.bug_blitz import worker_clone

    run_id = f"provprofile-{os.getpid()}-{int(time.time())}"
    work_root = worker_clone._clones_dir(repo) / run_id
    cone = [] if no_cone else cone_dirs(targets.read_text(encoding="utf-8"))
    run_dir = worker_clone.run_dir(repo, run_id)
    scratch = Path(tempfile.mkdtemp(prefix="bug-blitz-provision-profile-"))
    result: dict = {"repo": os.fspath(repo), "run_id": run_id, "cone": cone}
    try:
        if cone:
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / worker_clone.CONE_FILE).write_text("\n".join(cone) + "\n", encoding="utf-8", newline="\n")
        calls_path = scratch / "calls.json"
        t0 = time.monotonic()
        try:
            done = subprocess.run(
                [sys.executable, "-c", _CHILD, os.fspath(calls_path),
                 "--repo", os.fspath(repo), "--run-id", run_id, "--work-root", os.fspath(work_root)],
                cwd=os.fspath(_ENGINE_ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=_OUTER_DEADLINE_S, creationflags=_no_console(),
            )
            result.update(exit=done.returncode, stderr=done.stderr.strip(), stdout=done.stdout.strip())
        except subprocess.TimeoutExpired:
            result.update(exit=None, stderr=f"outer deadline {_OUTER_DEADLINE_S}s exceeded", stdout="")
        result["wall_s"] = round(time.monotonic() - t0, 1)
        try:
            data = json.loads(calls_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {"calls": [], "spawn_count": None}
        calls: list = data.get("calls") or []
        result["git_calls"] = calls
        result["git_process_ms"] = round(sum(c["process_ms"] for c in calls), 1)
        result["git_spawns"] = len(calls)
        result["child_spawn_count"] = data.get("spawn_count")
        result["any_timed_out"] = any(c["timed_out"] for c in calls)
        result["base_written"] = (work_root / ".git" / worker_clone.BASE_FILE).is_file()
        if work_root.is_dir():
            top = sorted(p.name for p in work_root.iterdir() if p.name != ".git")
            result["worktree_top_level"] = top
    finally:
        try:
            worker_clone.reap(repo, run_id)
        finally:
            result["clone_reaped"] = not work_root.exists()
            shutil.rmtree(run_dir, ignore_errors=True)
            shutil.rmtree(scratch, ignore_errors=True)
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", required=True, help="subject repo (read-only apart from the run's cone file)")
    ap.add_argument("--targets", required=True, help="file of failing tests, one `path::name` per line")
    ap.add_argument("--no-cone", action="store_true", help="record no cone: the full-checkout base behaviour")
    args = ap.parse_args(argv)
    if __package__ in (None, ""):
        sys.path.insert(0, os.fspath(_ENGINE_ROOT))
    result = profile(Path(args.repo).resolve(), Path(args.targets), no_cone=args.no_cone)
    print(json.dumps(result))
    return 0 if result.get("exit") == 0 and not result.get("any_timed_out") else 1


if __name__ == "__main__":
    sys.exit(main())
