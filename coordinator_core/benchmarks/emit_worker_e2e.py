"""Worker-e2e falsifier: `emit-dispatch-workflow --plan` through the door to a warm engine.

Prints one JSON object {process_ms, git_spawns, lines, path, ...}. Run at the run base and again at close;
the close reading is held against `BUDGET_MS`, `git_spawns == 0`, and `lines` below `LINES_BASE`.

AXES. `process_ms` is the door process's tree process time (`batched_process_time_ms`) plus the warm
engine's own per-request CPU (`worker_ms`, the engine job's process time delta over the same calls, git
children included), per call, median over SAMPLES of K_INVOCATIONS-call batches: never wall clock.
`git_spawns` counts git children the engine tree starts during one K-call pass; `procs` is the job-accounted
process count (door plus engine) and is reported apart, because on Windows each git child costs two.
`lines` is the `wc -l` total of `LINE_FILES`: the emit modules and the three door sources a plan row writes.

PATH. `warm-door`: the `--plan` argv goes through the settings-home door binary (`emit-dispatch-workflow.exe`
on Windows, the POSIX door elsewhere) to an isolated warm engine booted from a hardlink clone of this tree,
so the figure is the working tree's code with nothing shared with peers. A door that falls through to a cold
interpreter is refused, never read.

MODES. Default emits a fixture plan committed into a scratch one-file repo. `--real-repo` emits a committed plan
of this repo with `--repo-root` here, so the grep and ignore sources face the full tracked test population.
Before sampling, the engine pool is filled and settled so a first table build is never in a sample;
`rebuild_ms` is the engine CPU spent after those emits returned until the engine went quiet.

Run: `python coordinator_core/benchmarks/emit_worker_e2e.py [--real-repo] [--samples N] [--k N]`.
Scratch is a managed temp dir (and, for `--real-repo`, one `scratch/` dir) this script removes.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterator

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from coordinator_core.benchmarks import declare_benchmark_origin  # noqa: E402
from coordinator_core.benchmarks.engine_op_overhead import isolated_warm_engine  # noqa: E402
from coordinator_core.benchmarks.process_time import (  # noqa: E402
    IS_WINDOWS,
    LiveTreeAccountant,
    batched_process_time_ms,
)
from coordinator_core.win_portability import no_console_creationflags  # noqa: E402

K_INVOCATIONS = 10
SAMPLES = 5
#: A real-repo emit at base carries a git grep over ~9.1k tests; fewer, smaller batches keep the base reading affordable.
REAL_K_INVOCATIONS = 3
REAL_SAMPLES = 3
BUDGET_MS = 500.0
#: `door.c + door_posix.c + door_core.c` at the plan's census commit; the run-base reading is the
#: `_emit_worker_e2e_base.base.door_lines` in budget-manifest.json, which a close is held below.
LINES_BASE = 7315
PATH_WARM_DOOR = "warm-door"
ENTRYPOINT = "emit-dispatch-workflow"
#: A door round trip is the door plus, on Windows, its console host; a cold fall-through reads 7 or more.
_DOOR_PROCS_CEILING = 3.0
#: Batches discarded for a cold fall-through before the reading is refused; the count is `fall_through_batches`.
_FALL_THROUGH_RETRIES = 2

_FIXTURE_PLAN = _REPO_ROOT / "coordinator_core" / "benchmarks" / "fixtures" / "emit_full_path_plan.md"
_PLAN_REL = "docs/plans/emit-full-path-plan.md"
#: Preferred committed plan of this repo; `pick_real_plan` takes it only while its write set reads clean.
REAL_PLAN_REL = "docs/plans/2026-11/2026-11-21-hnd-span-cpu-metric-5909a4.md"
_PLAN_CANDIDATES = 60
_PLAN_MIN_ROWS = 2
_DOOR = "coordinator_core/warm/door"
_EMIT = "coordinator_core/ops/dispatch_emit"
#: Files the emit-memoise plan writes whose size is the line axis (door sources count the 7315 total).
LINE_FILES = (
    f"{_EMIT}/emit.py",
    f"{_EMIT}/ask_contract.py",
    f"{_EMIT}/ask_staged_prompts.py",
    f"{_EMIT}/ask_stage.py",
    f"{_DOOR}/door.c",
    f"{_DOOR}/door_posix.c",
    f"{_DOOR}/door_core.c",
)
_DOOR_FILES = LINE_FILES[-3:]

_CALL_TIMEOUT_S = 180
_POOL_FILL_ROUNDS = 3
#: One round: each fill emit against the real repo costs a full test-referencer pass at base.
_REAL_FILL_ROUNDS = 1
_SETTLE_WINDOW_S = 1.0
_SETTLE_MS_PER_S = 250.0
_SETTLE_TIMEOUT_S = 90.0
_GIT_IMAGES = frozenset({"git", "git.exe"})
_ADMISSION_DISABLE_ENV = "COORDINATOR_WORKFLOW_ADMISSION_DISABLE"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        **no_console_creationflags(),
    )


def _build_repo(root: Path) -> Path:
    repo = root / "repo"
    plan = repo / _PLAN_REL
    plan.parent.mkdir(parents=True)
    shutil.copyfile(_FIXTURE_PLAN, plan)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "budget@example.invalid")
    _git(repo, "config", "user.name", "budget")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")
    return repo


def pick_real_plan(preferred: str = REAL_PLAN_REL, repo_root: Path = _REPO_ROOT) -> str:
    """Repo-relative committed plan to emit for `--real-repo`.

    A plan whose write set is dirty in the live tree makes the route answer the dirty-write-set check
    with a git spawn, which would read as the emit's cost. Takes `preferred` while its writes are provably
    clean, else the clean committed plan with the most rows among the newest `_PLAN_CANDIDATES`.
    RuntimeError when none reads clean.
    """
    from coordinator_core.ops.dispatch_emit.plan_route import _write_set_provably_clean
    from coordinator_core.ops.dispatch_emit.spine_read import read_spine

    tracked = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "--", "docs/plans"],
        check=True,
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    ).stdout.splitlines()
    candidates = [preferred] + sorted((t for t in tracked if t.endswith(".md") and t != preferred), reverse=True)
    best, best_rows = None, 0
    for rel in candidates[: _PLAN_CANDIDATES + 1]:
        try:
            rows = len(read_spine(repo_root / rel))
            if rows < _PLAN_MIN_ROWS or not _write_set_provably_clean(repo_root / rel, repo_root):
                continue
        except Exception:
            continue
        if rel == preferred:
            return rel
        if rows > best_rows:
            best, best_rows = rel, rows
    if best is None:
        raise RuntimeError("no committed plan under docs/plans has a provably clean write set; commit or finish peers' edits")
    return best


def count_lines(paths=LINE_FILES, repo_root: Path = _REPO_ROOT) -> dict[str, int]:
    """`wc -l` (newline count) per repo-relative path; a missing file counts 0."""
    out = {}
    for rel in paths:
        p = repo_root / rel
        out[rel] = p.read_bytes().count(b"\n") if p.is_file() else 0
    return out


@contextlib.contextmanager
def _admission_disabled() -> Iterator[None]:
    """Admission off in the engine this script boots: a loaded box makes the engine refuse an emission-shaped
    call warm (`should_refuse_warm`) and the door re-runs it cold, which this harness must never read as warm."""
    prior = os.environ.get(_ADMISSION_DISABLE_ENV)
    os.environ[_ADMISSION_DISABLE_ENV] = "1"
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop(_ADMISSION_DISABLE_ENV, None)
        else:
            os.environ[_ADMISSION_DISABLE_ENV] = prior


def door_path() -> Path:
    """The settings-home door for `emit-dispatch-workflow`; RuntimeError naming it when absent."""
    home = Path(os.environ.get("COORDINATOR_SETTINGS_HOME") or Path.home() / ".coordinator-claude-settings")
    door = home / "bin" / (f"{ENTRYPOINT}.exe" if IS_WINDOWS else ENTRYPOINT)
    if not door.is_file():
        raise RuntimeError(f"door not installed: {door}; install the coordinator doors into the settings home")
    return door


class _EngineMeter:
    """Process time and process count of the warm engine tree: a job-object accountant on Windows
    (attached before any dispatch, so lazily started workers and git children inherit it), summed
    psutil CPU times elsewhere."""

    def __init__(self, pid: int) -> None:
        self._pid = pid
        self._acct = LiveTreeAccountant(pid) if IS_WINDOWS else None

    def process_ms(self) -> float:
        if self._acct is not None:
            return self._acct.snapshot()["process_time_ms"]
        import psutil

        total = 0.0
        root = psutil.Process(self._pid)
        for proc in [root, *root.children(recursive=True)]:
            try:
                t = proc.cpu_times()
            except psutil.Error:
                continue
            total += t.user + t.system + getattr(t, "children_user", 0.0) + getattr(t, "children_system", 0.0)
        return total * 1000.0

    def procs(self) -> int:
        return self._acct.snapshot()["procs"] if self._acct is not None else 0

    def close(self) -> None:
        if self._acct is not None:
            self._acct.close()


class _GitSpawnWatch:
    """Polls the engine's descendants and counts distinct git processes seen. A git child that starts and exits
    between two polls is missed, so the count is a lower bound: 0 means none was observed, not none ran."""

    def __init__(self, pid: int) -> None:
        self._pid = pid
        self._seen: set = set()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="git-spawn-watch", daemon=True)

    def _run(self) -> None:
        import psutil

        try:
            root = psutil.Process(self._pid)
        except psutil.Error:
            return
        while not self._stop.is_set():
            try:
                for child in root.children(recursive=True):
                    try:
                        if child.name().lower() in _GIT_IMAGES:
                            self._seen.add((child.pid, child.create_time()))
                    except psutil.Error:
                        continue
            except psutil.Error:
                return
            self._stop.wait(0.001)

    def __enter__(self) -> "_GitSpawnWatch":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()
        self._thread.join()

    @property
    def count(self) -> int:
        return len(self._seen)


def _door_call(cmd: list, env: dict, *, must_succeed: bool = True) -> None:
    done = subprocess.run(
        cmd, capture_output=True, text=True, env=env, timeout=_CALL_TIMEOUT_S, **no_console_creationflags()
    )
    if must_succeed and done.returncode != 0:
        raise RuntimeError(f"door call rc={done.returncode}: {(done.stderr or done.stdout)[:400]!r}")


def _fill_pool(cmd: list, env: dict, rounds: int) -> None:
    """Concurrent calls start every pool worker and give each its first emit; a call that overflows the
    pipe runs outside the engine, so a fill call's own exit code says nothing and is not asserted."""
    from coordinator_core.warm.server import DISPATCH_PROCESS_POOL_SIZE

    size = int(DISPATCH_PROCESS_POOL_SIZE)
    for _ in range(rounds):
        with ThreadPoolExecutor(max_workers=size) as ex:
            list(ex.map(lambda _i: _door_call(cmd, env, must_succeed=False), range(size)))


def _settle(meter: _EngineMeter) -> float:
    """Block until two consecutive windows each burn under `_SETTLE_MS_PER_S` of engine CPU; seconds waited."""
    start = time.monotonic()
    calm = 0
    prev = meter.process_ms()
    while calm < 2 and time.monotonic() - start < _SETTLE_TIMEOUT_S:
        time.sleep(_SETTLE_WINDOW_S)
        now = meter.process_ms()
        calm = calm + 1 if (now - prev) / _SETTLE_WINDOW_S < _SETTLE_MS_PER_S else 0
        prev = now
    return time.monotonic() - start


def _read_batch(meter: _EngineMeter, cmd: list, env: dict, k: int) -> dict:
    before_ms, before_procs = meter.process_ms(), meter.procs()
    door = batched_process_time_ms(cmd, k=k, env=env)
    worker_ms = (meter.process_ms() - before_ms) / k
    return {
        "process_ms": door["process_time_ms"] + worker_ms,
        "door_ms": door["process_time_ms"],
        "worker_ms": worker_ms,
        "procs": door["procs_per_call"] + (meter.procs() - before_procs) / k,
        "door_procs": door["procs_per_call"],
        "rc": door["rc"],
    }


def measure(
    samples: int = SAMPLES, k: int = K_INVOCATIONS, real_repo: bool = False, plan_rel: str = REAL_PLAN_REL
) -> dict:
    """One reading of the door `--plan` emit: process_ms, git_spawns, lines, path; rc must be 0 and the script written."""
    door = door_path()
    tmp = Path(tempfile.mkdtemp(prefix="emit_worker_e2e_"))
    out_dir = (_REPO_ROOT / "scratch" / f"emit_worker_e2e_{os.getpid()}") if real_repo else tmp / "repo" / "out"
    try:
        repo = _REPO_ROOT if real_repo else _build_repo(tmp)
        if real_repo:
            plan_rel = pick_real_plan(plan_rel, repo)
        plan = repo / (plan_rel if real_repo else _PLAN_REL)
        out = out_dir / "budget.workflow.mjs"
        out_dir.mkdir(parents=True, exist_ok=True)
        cmd = [str(door), "--plan", str(plan), "--out", str(out), "--force", "--repo-root", str(repo)]
        with _admission_disabled(), isolated_warm_engine(_REPO_ROOT, entrypoints=[ENTRYPOINT]) as engine:
            env = dict(engine.env)
            meter = _EngineMeter(engine.pid)
            try:
                _fill_pool(cmd, env, _REAL_FILL_ROUNDS if real_repo else _POOL_FILL_ROUNDS)
                before_settle = meter.process_ms()
                settle_secs = _settle(meter)
                rebuild_ms = meter.process_ms() - before_settle
                _door_call(cmd, env)
                readings, fell_through = [], 0
                while len(readings) < samples:
                    reading = _read_batch(meter, cmd, env, k)
                    if reading["door_procs"] > _DOOR_PROCS_CEILING:
                        fell_through += 1
                        if fell_through > _FALL_THROUGH_RETRIES:
                            raise RuntimeError(
                                f"door fell through to a cold interpreter in {fell_through} batches "
                                f"({reading['door_procs']} procs per call in the last)"
                            )
                        continue
                    readings.append(reading)
                with _GitSpawnWatch(engine.pid) as watch:
                    for _ in range(k):
                        _door_call(cmd, env)
                git_spawns = watch.count
            finally:
                meter.close()
        for r in readings:
            if r["rc"] != 0 or not out.is_file():
                raise RuntimeError(f"emit did not succeed (rc={r['rc']}, script written={out.is_file()})")
        lines = count_lines()
        process_ms = statistics.median(r["process_ms"] for r in readings)
        return {
            "process_ms": round(process_ms, 1),
            "door_ms": round(statistics.median(r["door_ms"] for r in readings), 1),
            "worker_ms": round(statistics.median(r["worker_ms"] for r in readings), 1),
            "rebuild_ms": round(rebuild_ms, 1),
            "settle_secs": round(settle_secs, 1),
            "fall_through_batches": fell_through,
            "git_spawns": git_spawns,
            "procs": round(statistics.median(r["procs"] for r in readings), 3),
            "lines": sum(lines.values()),
            "door_lines": sum(lines[f] for f in _DOOR_FILES),
            "door_lines_base": LINES_BASE,
            "line_detail": lines,
            "path": PATH_WARM_DOOR,
            "mode": "real-repo" if real_repo else "fixture",
            "plan": plan_rel if real_repo else _PLAN_REL,
            "k": k,
            "samples": samples,
            "sample_process_ms": sorted(round(r["process_ms"], 1) for r in readings),
            "budget_ms": BUDGET_MS,
            "under_budget": process_ms < BUDGET_MS and git_spawns == 0,
            "platform": sys.platform,
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        if real_repo:
            shutil.rmtree(out_dir, ignore_errors=True)


def main(argv=None) -> int:
    declare_benchmark_origin()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--samples", type=int, default=None)
    ap.add_argument("--k", type=int, default=None)
    ap.add_argument("--real-repo", action="store_true", help="emit a committed plan of this repo, not the scratch fixture repo")
    ap.add_argument("--plan-rel", default=REAL_PLAN_REL, help="preferred repo-relative committed plan for --real-repo (a dirty one is replaced)")
    args = ap.parse_args(argv)
    samples = args.samples or (REAL_SAMPLES if args.real_repo else SAMPLES)
    k = args.k or (REAL_K_INVOCATIONS if args.real_repo else K_INVOCATIONS)
    print(json.dumps(measure(samples, k, args.real_repo, args.plan_rel), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
