"""review.prep on a heavily dirty shared worktree: process time, spawn count, per-stage diagnosis.

Seeds a throwaway repo (`isolated_clone.record_fixture_repo`) with DIRTY declared files (half
modified, half untracked) plus as many undeclared untracked peer files, then calls
`review_prep._handler` at default params in a child process. Two figures per scenario:

- `process_ms` / `procs`: the child's whole process tree (`process_time.single_invocation_tree_process_time`)
  minus an import-only child (`--setup-only`), so interpreter start and imports are not charged.
- `spawn_count` and `stages`: `subprocess.Popen` creations inside the handler call, attributed to the
  innermost active `review_prep` stage; `wall_ms` / `cpu_ms` per stage are exclusive of nested stages
  (`cpu_ms` is the child's own process CPU and excludes spawned git, so rank stages by spawns and wall).

    python coordinator_core/benchmarks/review_prep_dirty_worktree.py [--dirty 500] [--small 10]
        [--mode worktree|range|runid|both] [--enforce] [--concurrent-writer]
    python -m coordinator_core.benchmarks.review_prep_dirty_worktree --opl [--since EPOCH] [--json]

`--enforce` exits 1 when `process_ms` >= `PROCESS_BAR_MS`, `spawn_count` > `SPAWN_BOUND`, or the spawn
count differs between `--small` and `--dirty` declared files.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path

PROCESS_BAR_MS = 500.0
# Process time is the median of RUNS fresh-clone runs: one run carries +-60ms of box noise at 15.6ms ticks.
RUNS = 5
# Distinct git questions review.prep asks per mode, independent of the dirty-file count: worktree and
# runid: peer log, claims log, diff --numstat, freeze diff; range adds rev-parse and diff-tree for the
# freeze. The untracked split, the dirty check and the head blobs are read in process; slices are cut
# from the frozen whole diff (review_partition_slices._partition `whole_diff`).
SPAWN_BOUND = {"worktree": 4, "range": 5, "runid": 4}
SESSION_ID = "bench-review-prep"
PLAN_REL = "docs/plans/p.md"
SLICE_PREFIX = "bench-prep"
_REPO_ROOT = Path(__file__).resolve().parents[2]

_STAGES = {
    "review_prep": [
        ("_stored_reply", "stored_reply"),
        ("_peer_bases", "peer_bases"),
        ("_freeze_whole_by_base", "freeze_whole_by_base"),
        ("_range_blob_bytes", "range_blob_bytes"),
        ("_declared_hashes", "declared_hashes"),
        ("_write_pre_review_snapshot", "pre_review_snapshot"),
        ("foreign_claims", "foreign_claims"),
        ("_write_sidecar", "write_sidecar"),
        ("_header_only_product_paths", "header_only_paths"),
        ("freeze_diff", "freeze_diff"),
        ("partition_slices", "partition_slices"),
        ("diff_product_files", "product_files"),
    ],
    "landing_integrity": [("unparseable_py", "unparseable_py"), ("unparseable_py_at", "unparseable_py_at")],
}
# `runid` mode resolves the declared set from a run record (`declared_paths_for_run`), the path the
# stall dump of the second hang shows; these stages are exercised only there.
_RUNID_STAGES = {
    "review_prep": [("declared_paths_for_run", "declared_paths_for_run"),
                    ("undeclared_edits_for_run", "undeclared_edits_for_run")],
    "run_write_set": [("_without_peer_held", "without_peer_held")],
    "land_run": [("_peer_held", "peer_held"), ("foreign_halted_withheld", "foreign_halted_withheld")],
    "claim_index": [("lookup", "claim_index_lookup"), ("_read_index_names", "read_index_names")],
}
RUN_ID = "warp-bench-review-prep-0001"
OPL_OP = "review.prep"
OPL_SCOPE = "per_op_handler"


def _git(repo: Path, *args: str) -> str:
    from coordinator_core.git.run import run_git

    got = run_git(["-C", str(repo), "-c", "core.autocrlf=false", *args], timeout=60.0)
    if not got.ok:
        raise RuntimeError(f"git {args[0]} failed rc={got.returncode}: {got.stderr.strip()}")
    return got.stdout


def _write(repo: Path, rel: str, text: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")


def _claim_store_hygiene(repo: Path) -> None:
    """A fresh fixture clone has no hygiene claim, so its first commit-route call spawns the detached
    daily child; a live repo claims once per `CLAIM_INTERVAL_S`. Seed the claim: that child is not
    review.prep's cost."""
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.store_hygiene import _CLAIM_RELPATH

    claim = Path(resolve_git_common_dir(repo), *_CLAIM_RELPATH)
    claim.parent.mkdir(parents=True, exist_ok=True)
    claim.touch()


def seed(repo: Path, dirty: int, mode: str) -> dict:
    """Seed `repo` and return the handler params. `dirty` declared files: half tracked-and-modified,
    half untracked; `dirty` more undeclared untracked peer files. `range` commits the declared
    changes (head) and leaves the peer dirt uncommitted."""
    modified = dirty // 2
    tracked = [f"src/d{i % 20}/m{i}.py" for i in range(modified)]
    fresh = [f"src/d{i % 20}/u{i}.py" for i in range(dirty - modified)]
    _write(repo, PLAN_REL, "---\nplan_id: pln-bench-1\n---\n\n# p\n")
    for rel in tracked:
        _write(repo, rel, "v = 0\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@example.com", "-c", "user.name=t", "-c", "commit.gpgsign=false",
         "commit", "-q", "-m", "bench base")
    base = _git(repo, "rev-parse", "HEAD").strip()
    for rel in tracked:
        _write(repo, rel, "v = 1\n" * 3)
    for rel in fresh:
        _write(repo, rel, "v = 1\n" * 3)
    declared = tracked + fresh
    head = None
    if mode == "range":
        _git(repo, "add", "--", *declared)
        _git(repo, "-c", "user.email=t@example.com", "-c", "user.name=t", "-c", "commit.gpgsign=false",
             "commit", "-q", "-m", "bench head")
        head = _git(repo, "rev-parse", "HEAD").strip()
    for i in range(dirty):
        _write(repo, f"peer/p{i}.txt", f"peer {i}\n")
    _claim_store_hygiene(repo)
    params = {"base": base, "paths": declared, "slice_prefix": SLICE_PREFIX, "plan_path": PLAN_REL,
              "session_id": SESSION_ID}
    params.update({"head": head} if head else {"worktree": True})
    if mode == "runid":
        _write(repo, f"state/warp/{RUN_ID}/manifest.json",
               json.dumps({"review_declared_paths": declared, "plan_path": PLAN_REL}))
        params = {"base": base, "run_id": RUN_ID, "worktree": True, "plan_path": PLAN_REL,
                  "session_id": SESSION_ID}
    return params


def _git_verb(argv: tuple) -> str:
    args = list(argv[1:])
    i = 0
    while i < len(args):
        if args[i] in ("-C", "-c"):
            i += 2
            continue
        if args[i].startswith("-"):
            i += 1
            continue
        return args[i]
    return Path(argv[0]).name if argv else "?"


class _Recorder:
    """Exclusive per-stage attribution: time and spawns go to the innermost active stage."""

    def __init__(self) -> None:
        self.stack: list = []
        self.stages: dict = {}
        self.last_wall = time.perf_counter()
        self.last_cpu = time.process_time()

    def _bucket(self, name: str) -> dict:
        return self.stages.setdefault(name, {"calls": 0, "spawns": 0, "wall_ms": 0.0, "cpu_ms": 0.0, "git": Counter()})

    def _tick(self) -> None:
        now_wall, now_cpu = time.perf_counter(), time.process_time()
        bucket = self._bucket(self.stack[-1] if self.stack else "other")
        bucket["wall_ms"] += (now_wall - self.last_wall) * 1000
        bucket["cpu_ms"] += (now_cpu - self.last_cpu) * 1000
        self.last_wall, self.last_cpu = now_wall, now_cpu

    def enter(self, name: str) -> None:
        self._tick()
        self.stack.append(name)
        self._bucket(name)["calls"] += 1

    def exit(self) -> None:
        self._tick()
        self.stack.pop()

    def spawn(self, argv: tuple) -> None:
        bucket = self._bucket(self.stack[-1] if self.stack else "other")
        bucket["spawns"] += 1
        bucket["git"][_git_verb(argv) if argv and "git" in Path(argv[0]).name.lower() else Path(argv[0]).name if argv else "?"] += 1


def _instrument(rec: _Recorder):
    import importlib

    review_prep = importlib.import_module("coordinator_core.ops.review_prep")
    landing = importlib.import_module("coordinator_core.ops.dispatch_emit.landing_integrity")
    modules = {
        "review_prep": review_prep,
        "landing_integrity": landing,
        "run_write_set": importlib.import_module("coordinator_core.ops.dispatch_emit.run_write_set"),
        "land_run": importlib.import_module("coordinator_core.ops.dispatch_emit.land_run"),
        "claim_index": importlib.import_module("coordinator_core.session.claim_index"),
    }
    for heavy in ("freeze_diff", "partition_slices", "diff_product_files"):
        review_prep._heavy(heavy)

    def wrap(fn, label):
        def inner(*a, **kw):
            rec.enter(label)
            try:
                return fn(*a, **kw)
            finally:
                rec.exit()

        return inner

    for mod_key in {*_STAGES, *_RUNID_STAGES}:
        entries = [*_STAGES.get(mod_key, []), *_RUNID_STAGES.get(mod_key, [])]
        mod = modules[mod_key]
        for attr, label in entries:
            setattr(mod, attr, wrap(getattr(mod, attr), label))

    real_popen = subprocess.Popen

    class _Popen(real_popen):
        def __init__(self, args, *a, **kw):
            argv = tuple(str(x) for x in args) if isinstance(args, (list, tuple)) else (str(args),)
            rec.spawn(argv)
            super().__init__(args, *a, **kw)

    subprocess.Popen = _Popen
    return review_prep


def child_main(repo: Path, params: dict, setup_only: bool) -> int:
    rec = _Recorder()
    review_prep = _instrument(rec)
    if setup_only:
        return 0
    rec.last_wall, rec.last_cpu = time.perf_counter(), time.process_time()
    reply = review_prep._handler(params, repo)
    rec._tick()
    stages = {
        name: {**{k: (round(v, 3) if isinstance(v, float) else v) for k, v in b.items() if k != "git"},
               "git": dict(b["git"])}
        for name, b in rec.stages.items()
    }
    print(json.dumps({
        "error": reply.get("error"),
        "verdict": reply.get("verdict"),
        "product_files": reply.get("product_files"),
        "slices": len(reply.get("slices") or []),
        "spawn_count": sum(b["spawns"] for b in rec.stages.values()),
        "stages": stages,
    }))
    return 0


def _run_child(repo: Path, params_path: Path, out_dir: Path, tag: str, setup_only: bool) -> dict:
    from coordinator_core.benchmarks import process_time

    cmd = [sys.executable, str(Path(__file__).resolve()), "--child", "--repo", str(repo),
           "--params", str(params_path)]
    if setup_only:
        cmd.append("--setup-only")
    out, err = out_dir / f"{tag}.out", out_dir / f"{tag}.err"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    measured = process_time.single_invocation_tree_process_time(
        cmd, env=env, cwd=str(_REPO_ROOT), stdout_path=str(out), stderr_path=str(err)
    )
    if measured["rc"] != 0:
        raise RuntimeError(f"{tag}: child rc={measured['rc']}: {err.read_text(errors='replace')[-2000:]}")
    measured["payload"] = None if setup_only else json.loads(out.read_text(encoding="utf-8").strip().splitlines()[-1])
    return measured


WRITER_REL = "src/d0/m0.py"


@contextlib.contextmanager
def concurrent_writer(repo: Path, rel: str = WRITER_REL):
    """Yield a stats dict while a thread repeatedly rewrites tracked `rel` and runs `git add` on it
    (takes index.lock), the second-run shape of the review.prep stall. Stats: `iterations` (completed
    adds) and `failures` (adds git refused, e.g. a lock held by the measured child)."""
    stats = {"iterations": 0, "failures": 0}
    stop = threading.Event()

    def loop() -> None:
        n = 0
        while not stop.is_set():
            n += 1
            _write(repo, rel, f"v = {n}\n" * 3)
            try:
                _git(repo, "add", "--", rel)
                stats["iterations"] += 1
            except RuntimeError:
                stats["failures"] += 1
            stop.wait(0.005)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    try:
        yield stats
    finally:
        stop.set()
        thread.join(timeout=70)


def measure(mode: str, dirty: int, tree: bool, writer: bool = False) -> dict:
    from coordinator_core.benchmarks.isolated_clone import record_fixture_repo

    writer_stats = None
    with record_fixture_repo(_REPO_ROOT, [], prefix="rp-dirty", label="review_prep_dirty") as repo:
        params = seed(repo, dirty, mode)
        params_path = repo.parent / "params.json"
        params_path.write_text(json.dumps(params), encoding="utf-8")
        setup = _run_child(repo, params_path, repo.parent, "setup", True) if tree else None
        if writer:
            with concurrent_writer(repo) as writer_stats:
                full = _run_child(repo, params_path, repo.parent, "full", False)
        else:
            full = _run_child(repo, params_path, repo.parent, "full", False)
    payload = full["payload"]
    if payload["error"]:
        raise RuntimeError(f"review.prep ({mode}, dirty={dirty}) errored: {payload['error']}")
    result = {
        "mode": mode,
        "dirty_declared": dirty,
        "spawn_count": payload["spawn_count"],
        "product_files": payload["product_files"],
        "verdict": payload["verdict"],
        "slices": payload["slices"],
        "stages": payload["stages"],
    }
    if writer_stats is not None:
        result["writer"] = writer_stats
    if tree:
        result["process_ms"] = round(full["process_time_ms"] - setup["process_time_ms"], 1)
        result["procs"] = full["procs"] - setup["procs"]
        result["wall_ms"] = round(full["wall_ms"] - setup["wall_ms"], 1)
    ranked = sorted(payload["stages"].items(), key=lambda kv: (kv[1]["spawns"], kv[1]["wall_ms"]), reverse=True)
    result["dominant_stage"] = ranked[0][0]
    return result


def _pctile(sorted_vals: list, q: float):
    if not sorted_vals:
        return None
    return sorted_vals[min(len(sorted_vals) - 1, int(q * len(sorted_vals)))]


def measure_opl(repo_root: Path, since_epoch=None, op: str = OPL_OP, scope: str = OPL_SCOPE) -> dict:
    """Production baseline: `process_ms` of `op` `kind: process_time` rows of `scope` in the live OPL
    sink and its rotated generations, rows with `t_start >= since_epoch` only. `over_bar_legs` counts,
    per leg name, the over-bar rows whose `legs` field names it (empty until review.prep attaches legs)."""
    from coordinator_core.telemetry.op_latency import sink_generations

    vals: list = []
    over_bar_legs: dict = {}
    paths = sink_generations(repo_root)
    for path in paths:
        try:
            fh = open(path, "r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if op not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict) or row.get("kind") != "process_time" or row.get("op") != op:
                    continue
                if row.get("measurement_scope") != scope:
                    continue
                t_start, ms = row.get("t_start"), row.get("process_ms")
                if not isinstance(ms, (int, float)) or isinstance(ms, bool):
                    continue
                if since_epoch is not None and (not isinstance(t_start, (int, float)) or t_start < since_epoch):
                    continue
                vals.append(float(ms))
                if ms > PROCESS_BAR_MS:
                    legs = row.get("legs")
                    for leg in legs if isinstance(legs, list) else ():
                        name = leg.get("name") if isinstance(leg, dict) else None
                        if isinstance(name, str):
                            over_bar_legs[name] = over_bar_legs.get(name, 0) + 1
    vals.sort()
    return {
        "op": op, "measurement_scope": scope, "since_epoch": since_epoch, "sinks": len(paths),
        "calls": len(vals), "over_bar": sum(1 for v in vals if v > PROCESS_BAR_MS), "bar_ms": PROCESS_BAR_MS,
        "process_ms_p50": _pctile(vals, 0.5), "process_ms_p95": _pctile(vals, 0.95),
        "process_ms_max": vals[-1] if vals else None,
        "legs_histogram": over_bar_legs,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--opl", action="store_true",
                    help="read the live OPL sink (review.prep per_op_handler process_time rows) instead of seeding")
    ap.add_argument("--json", action="store_true", help="OPL arm: print one JSON object")
    ap.add_argument("--since", type=float, default=None, help="OPL arm: only rows with t_start >= this epoch")
    ap.add_argument("--dirty", type=int, default=500)
    ap.add_argument("--small", type=int, default=10)
    ap.add_argument("--mode", choices=("worktree", "range", "runid", "both"), default="both")
    ap.add_argument("--enforce", action="store_true")
    ap.add_argument("--concurrent-writer", action="store_true",
                    help="run a writer loop (modify a tracked file, git add) in the clone while review.prep runs")
    ap.add_argument("--child", action="store_true")
    ap.add_argument("--setup-only", action="store_true")
    ap.add_argument("--repo")
    ap.add_argument("--params")
    args = ap.parse_args(argv)
    if args.child:
        return child_main(Path(args.repo), json.loads(Path(args.params).read_text(encoding="utf-8")), args.setup_only)
    if args.opl:
        report = measure_opl(_REPO_ROOT, args.since)
        print(json.dumps(report, indent=1) if args.json else
              " ".join(f"{k}={v}" for k, v in report.items()))
        return 0

    from coordinator_core.benchmarks import declare_benchmark_origin

    declare_benchmark_origin()
    modes = ("worktree", "range", "runid") if args.mode == "both" else (args.mode,)
    failures = []
    report = []
    for mode in modes:
        small = measure(mode, args.small, tree=False, writer=args.concurrent_writer)
        large = sorted((measure(mode, args.dirty, tree=True, writer=args.concurrent_writer) for _ in range(RUNS)),
                       key=lambda r: r["process_ms"])[RUNS // 2]
        large["spawn_count_small"] = small["spawn_count"]
        large["spawn_constant"] = small["spawn_count"] == large["spawn_count"]
        large["within_bar"] = large["process_ms"] < PROCESS_BAR_MS
        large["within_spawn_bound"] = large["spawn_count"] <= SPAWN_BOUND[mode]
        if not (large["spawn_constant"] and large["within_bar"] and large["within_spawn_bound"]):
            failures.append(mode)
        report.append(large)
        print(f"[{mode}] process_ms={large['process_ms']} spawn_count={large['spawn_count']} "
              f"(dirty={small['dirty_declared']}: {small['spawn_count']}) procs={large['procs']} "
              f"dominant_stage={large['dominant_stage']}"
              + (f" writer={large['writer']}" if "writer" in large else ""))
        for name, b in sorted(large["stages"].items(), key=lambda kv: -kv[1]["wall_ms"]):
            print(f"  {name:22s} calls={b['calls']:<4d} spawns={b['spawns']:<5d} wall_ms={b['wall_ms']:<9} "
                  f"cpu_ms={b['cpu_ms']:<8} git={b['git']}")
    print(json.dumps(report))
    return 1 if (args.enforce and failures) else 0


if __name__ == "__main__":
    # Script invocation puts this directory first on sys.path, shadowing stdlib names.
    sys.path[:] = [str(_REPO_ROOT)] + [p for p in sys.path if Path(p or ".").resolve() != Path(__file__).resolve().parent]
    sys.exit(main())
