"""Falsifier for the update-docs ops budget: the four ops through their default door, and door round-trip under load.

For each of `update-docs-probes repomap-gate`, `refresh-queries`, `reap-stale-subagent-sidecars` and
`check-citation-integrity` it runs the op once with default arguments, in a fresh interpreter, from a fixture
repo (a stale subagent sidecar, a resolved queue entry, a wiki, a seeded citation baseline). The route is the
door's: `invoke.from_argv` with `entrypoint=<cli>`, exactly what the warm engine runs. An entrypoint the
allowlist refuses (`-32007`) takes the door's cold fall-through instead (`route: cold`, the CLI's own launcher
in a child interpreter). Process time is the tree's user+kernel CPU with the harness's own interpreter and
imports subtracted; spawn count is the Popen audit count; never wall clock (DR-344).

The door leg boots an isolated warm engine from the source tree and times `coordinator-invoke ping {}` round
trips through the installed door, idle (sequential) and under N concurrent callers. Round-trip latency is wall
time by definition; it is reported as a latency figure, not a process-time one. It needs the installed door and
reports `door.unavailable` with the reason when there is none.

Output keys (C6 and C7 consume them; pinned by the test):
  ops.<op>: route, reply_code, process_ms, spawn_count, dominant_leg, wall_ms
  door: callers, door_rtt_p50_ms_idle, door_rtt_p95_ms_idle, door_rtt_p50_ms_load, door_rtt_p95_ms_load,
        failed_idle, failed_load, unavailable

    python -m coordinator_core.benchmarks.update_docs_ops_budget [--source live|<sha>] [--callers N]
        [--workdir DIR] [--no-door]

`--source <sha>` measures a `git archive <sha>` export of coordinator_core, coordinator/bin and coordinator/lib under the work
directory, never the live tree. Default arguments measure the live tree. The work directory defaults to a fresh
directory under `<repo>/scratch` and is removed afterwards.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from typing import Optional, Sequence

#: op label -> (coordinator/bin entrypoint, default argv).
OPS: dict[str, tuple[str, list[str]]] = {
    "repomap-gate": ("update-docs-probes", ["repomap-gate"]),
    "refresh-queries": ("refresh-queries", []),
    "reap-stale-subagent-sidecars": ("reap-stale-subagent-sidecars", []),
    "check-citation-integrity": ("check-citation-integrity", []),
}
WIKI_PAGES = 400
WIKI_PER_DIR = 50
EXPORT_PATHSPEC = ("coordinator_core", "coordinator/bin", "coordinator/lib")
DOOR_IDLE_CALLS = 20
DOOR_WARMUP_CALLS = 3
#: Calls each concurrent caller makes under load.
DOOR_LOAD_ROUNDS = 6
_CALL_TIMEOUT_S = 120
_IDLE_OP = "__idle__"
_REFUSED_CODE = -32007
_ERROR_CODE = -32603
_BASELINE_REL = "state/baselines/citation-integrity.json"
_BOOTSTRAP = (
    "import importlib.util as u, sys\n"
    "spec = u.spec_from_file_location('_update_docs_ops_child', sys.argv[1])\n"
    "m = u.module_from_spec(spec); sys.modules[spec.name] = m; spec.loader.exec_module(m)\n"
    "sys.exit(m.child_main(sys.argv[2:]))\n"
)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_callers() -> int:
    """physical_cores/2, capped by usable RAM at 150MB per caller, never below 1."""
    cores = os.cpu_count() or 2
    ram_gb = None
    try:
        import psutil

        cores = psutil.cpu_count(logical=False) or cores
        ram_gb = psutil.virtual_memory().total / (1024 ** 3)
    except ImportError:
        pass
    cap = cores // 2
    if ram_gb is not None:
        cap = min(cap, int(ram_gb * 1024 / 150))
    return max(1, cap)


def percentile(samples: Sequence[float], pct: float) -> float:
    """Linear-interpolated percentile of `samples`; ValueError on an empty list."""
    if not samples:
        raise ValueError("percentile: no samples")
    ordered = sorted(samples)
    rank = pct / 100.0 * (len(ordered) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (rank - lo)


def export_source(repo: Path, sha: str, dest: Path) -> Path:
    """`git archive <sha>` of the engine paths extracted into `dest`; returns `dest`."""
    from coordinator_core.win_portability import no_console_creationflags

    dest.mkdir(parents=True, exist_ok=True)
    done = subprocess.run(
        ["git", "archive", "--format=tar", sha, *EXPORT_PATHSPEC],
        cwd=str(repo), capture_output=True, timeout=_CALL_TIMEOUT_S, **no_console_creationflags(),
    )
    if done.returncode != 0:
        raise RuntimeError(f"git archive {sha} failed rc={done.returncode}: {done.stderr.decode(errors='replace')[:300]}")
    import io

    with tarfile.open(fileobj=io.BytesIO(done.stdout)) as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(dest, filter="data")
        else:
            tar.extractall(dest)
    return dest


def _git_root_marker(export: Path) -> None:
    """The door leg's isolated engine clones under `<source>/scratch`, which must sit under a git root."""
    from coordinator_core.git.run import run_git

    run_git(["init", "-q"], cwd=str(export))


def ensure_ops_fixture(parent: Path, wiki_pages: int = WIKI_PAGES) -> Path:
    """The cold-run fixture plus a wiki of `wiki_pages` linked pages and a seeded citation baseline, committed."""
    from coordinator_core.benchmarks import updatedocs_gates_cold_full_path as base
    from coordinator_core.benchmarks.updatedocs_gates_cold_full_path import _git

    repo = base.ensure_fixture(parent, plans=20, handoffs=5)
    for i in range(wiki_pages):
        d = repo / "docs" / "wiki" / f"{i // WIKI_PER_DIR:03d}"
        d.mkdir(parents=True, exist_ok=True)
        nxt = (i + 1) % wiki_pages
        link = f"../{nxt // WIKI_PER_DIR:03d}/page-{nxt:04d}.md"
        (d / f"page-{i:04d}.md").write_text(f"# Page {i}\n\nSee [next]({link}).\n", encoding="utf-8")
    baseline = repo / _BASELINE_REL
    baseline.parent.mkdir(parents=True, exist_ok=True)
    baseline.write_text(json.dumps({"baseline_sha": None, "wiki_file_count": 0, "partitions": {}}), encoding="utf-8")
    _git(repo, "add", "docs/wiki", "state")
    _git(repo, "commit", "-q", "-m", "wiki")
    return repo


# --------------------------------------------------------------------------- child (fresh interpreter)


def _spawn_label(argv) -> str:
    items = argv if isinstance(argv, (list, tuple)) else [argv]
    for item in items:
        if str(item).endswith(".py"):
            return os.path.basename(str(item))
    return os.path.basename(str(items[0])) if items else "?"


def child_main(argv: list[str]) -> int:
    """Runs in the measured interpreter: `<op> <out.json> <repo>`; writes a metrics JSON to out."""
    import cProfile
    import pstats

    op, out, repo = argv[0], Path(argv[1]), argv[2]
    from coordinator_core.ops import invoke_from_argv as ifa
    from coordinator_core.telemetry.spawn_counter import spawn_count

    if op == _IDLE_OP:
        out.write_text(json.dumps({"idle": True}), encoding="utf-8")
        return 0
    entrypoint, args = OPS[op]
    os.chdir(repo)
    labels: list[str] = []

    def hook(event: str, a: tuple) -> None:
        if event == "subprocess.Popen":
            try:
                labels.append(_spawn_label(a[1]))
            except (IndexError, TypeError):
                pass

    sys.addaudithook(hook)
    prof = cProfile.Profile(time.process_time)
    s0, c0, w0 = spawn_count(), time.process_time(), time.perf_counter()
    route, reply = "warm", None
    try:
        prof.enable()
        try:
            reply = int(ifa._invoke_from_argv({"argv": list(args), "cwd": repo, "entrypoint": entrypoint})["exit_code"])
        finally:
            prof.disable()
    except ifa.EntrypointNotWarmLoadableError:
        route, reply = "cold", _REFUSED_CODE
        script = Path(ifa._ENGINE_ROOT) / "coordinator" / "bin" / f"{entrypoint}.py"
        done = subprocess.run([sys.executable, str(script), *args], cwd=repo, capture_output=True,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        reply = done.returncode
    except Exception as exc:  # the op's own failure is a reported reply, not a harness crash
        reply = _ERROR_CODE
        labels.append(f"raised:{type(exc).__name__}")
    top = ""
    if route == "warm":
        root = str(Path(ifa._ENGINE_ROOT).resolve())
        best = 0.0
        for (filename, _line, func), (_cc, _nc, tt, _ct, _callers) in pstats.Stats(prof).stats.items():
            if str(filename).startswith(root) and tt > best and "invoke_from_argv" not in str(filename):
                best, top = tt, f"{Path(filename).name}:{func}"
    out.write_text(json.dumps({
        "route": route,
        "reply_code": reply,
        "spawn_count": spawn_count() - s0,
        "self_ms": round((time.process_time() - c0) * 1000, 1),
        "wall_ms": round((time.perf_counter() - w0) * 1000, 1),
        "top_function": top,
        "spawned": labels,
    }), encoding="utf-8")
    return 0


# --------------------------------------------------------------------------- parent legs


def _child_env(source: Path) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(source) + os.pathsep + env.get("PYTHONPATH", "")
    env["COORDINATOR_ENGINE_ROOT"] = str(source)
    return env


def _run_child(op: str, source: Path, repo: Path, out: Path) -> dict:
    from coordinator_core.benchmarks.process_time import batched_process_time_ms

    cmd = [sys.executable, "-c", _BOOTSTRAP, str(Path(__file__).resolve()), op, str(out), str(repo)]
    timing = batched_process_time_ms(cmd, k=1, env=_child_env(source), cwd=str(repo))
    if timing["rc"] != 0 or not out.is_file():
        raise RuntimeError(f"child {op!r} exited rc={timing['rc']} without a result file")
    return {"tree_ms": float(timing["process_time_ms"]), **json.loads(out.read_text(encoding="utf-8"))}


def dominant_leg(result: dict, process_ms: float) -> str:
    """The cost leg: spawned children when their share exceeds the interpreter's own, else the hottest function."""
    spawned = result.get("spawned") or []
    if spawned and process_ms - result.get("self_ms", 0.0) > result.get("self_ms", 0.0):
        counts: dict[str, int] = {}
        for label in spawned:
            counts[label] = counts.get(label, 0) + 1
        top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:2]
        return "spawned:" + ",".join(f"{label}x{n}" for label, n in top)
    return result.get("top_function") or ("cold launcher" if result.get("route") == "cold" else "unattributed")


def measure_op(op: str, source: Path, repo: Path, scratch: Path) -> dict:
    """One default-argument run of `op` against `repo` from the `source` tree."""
    scratch.mkdir(parents=True, exist_ok=True)
    idle = _run_child(_IDLE_OP, source, repo, scratch / "idle.json")
    run = _run_child(op, source, repo, scratch / f"{op}.json")
    process_ms = round(max(0.0, run["tree_ms"] - idle["tree_ms"]), 1)
    return {
        "route": run["route"],
        "reply_code": run["reply_code"],
        "process_ms": process_ms,
        "spawn_count": run["spawn_count"],
        "dominant_leg": dominant_leg(run, process_ms),
        "wall_ms": run["wall_ms"],
    }


def installed_door(name: str = "coordinator-invoke") -> Optional[Path]:
    home = os.environ.get("COORDINATOR_SETTINGS_HOME") or os.path.join(
        os.environ.get("CLAUDE_HOME") or os.path.expanduser("~"), ".coordinator-claude-settings")
    for candidate in (Path(home) / "bin" / f"{name}.exe", Path(home) / "bin" / name):
        if candidate.is_file():
            return candidate
    return None


def _timed_call(cmd: list[str], env: dict) -> tuple[float, bool]:
    from coordinator_core.win_portability import no_console_creationflags

    t0 = time.perf_counter()
    done = subprocess.run(cmd, capture_output=True, env=env, timeout=_CALL_TIMEOUT_S, **no_console_creationflags())
    return (time.perf_counter() - t0) * 1000, done.returncode == 0


def summarize_rtt(samples: Sequence[tuple[float, bool]]) -> tuple[Optional[float], Optional[float], int]:
    """(p50, p95, failed) over the succeeded calls; failed calls are counted, never timed."""
    good = [ms for ms, ok in samples if ok]
    failed = len(samples) - len(good)
    if not good:
        return None, None, failed
    return round(percentile(good, 50), 1), round(percentile(good, 95), 1), failed


def measure_door(source: Path, callers: int) -> dict:
    """Door round-trip p50/p95, idle and under `callers` concurrent callers, through an isolated warm engine."""
    out: dict = {"callers": callers, "door_rtt_p50_ms_idle": None, "door_rtt_p95_ms_idle": None,
                 "door_rtt_p50_ms_load": None, "door_rtt_p95_ms_load": None,
                 "failed_idle": None, "failed_load": None, "unavailable": None}
    door = installed_door()
    if door is None:
        out["unavailable"] = "no installed coordinator-invoke door under the settings home"
        return out
    from coordinator_core.benchmarks.engine_op_overhead import isolated_warm_engine

    cmd = [str(door), "ping", "{}"]
    try:
        with isolated_warm_engine(source, entrypoints=["coordinator-invoke"]) as engine:
            for _ in range(DOOR_WARMUP_CALLS):
                _timed_call(cmd, engine.env)
            idle = [_timed_call(cmd, engine.env) for _ in range(DOOR_IDLE_CALLS)]
            with concurrent.futures.ThreadPoolExecutor(max_workers=callers) as pool:
                load = list(pool.map(lambda _i: _timed_call(cmd, engine.env), range(callers * DOOR_LOAD_ROUNDS)))
    except Exception as exc:
        out["unavailable"] = f"isolated warm engine failed: {type(exc).__name__}: {str(exc)[:200]}"
        return out
    out["door_rtt_p50_ms_idle"], out["door_rtt_p95_ms_idle"], out["failed_idle"] = summarize_rtt(idle)
    out["door_rtt_p50_ms_load"], out["door_rtt_p95_ms_load"], out["failed_load"] = summarize_rtt(load)
    return out


def measure(source_arg: str, workdir: Path, callers: int, with_door: bool = True) -> dict:
    """The full report for `source_arg` (`live` or a git ref) with all scratch under `workdir`."""
    root = repo_root()
    if source_arg == "live":
        source, source_label = root, "live"
    else:
        source = export_source(root, source_arg, workdir / "export")
        _git_root_marker(source)
        source_label = source_arg
    repo = ensure_ops_fixture(workdir)
    ops = {op: measure_op(op, source, repo, workdir / "scratch" / op) for op in OPS}
    door = measure_door(source, callers) if with_door else {"callers": callers, "unavailable": "door leg skipped (--no-door)"}
    return {"source": source_label, "ops": ops, "door": door}


def _parse(argv: list[str]) -> Optional[dict]:
    opts = {"source": "live", "callers": None, "workdir": None, "door": True}
    args = list(argv)
    while args:
        flag = args.pop(0)
        if flag == "--no-door":
            opts["door"] = False
        elif flag in ("--source", "--callers", "--workdir") and args:
            value = args.pop(0)
            if flag == "--callers":
                if not value.isdigit() or int(value) < 1:
                    return None
                opts["callers"] = int(value)
            else:
                opts[flag[2:]] = value
        else:
            return None
    return opts


def _remove_tree(path: Path) -> None:
    """rmtree that clears the read-only bit first: git object files are read-only on Windows."""
    def clear(func, target, _exc) -> None:
        os.chmod(target, 0o700)
        func(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=clear)
    else:
        shutil.rmtree(path, onerror=lambda f, p, e: clear(f, p, e))


def main(argv: list[str]) -> int:
    opts = _parse(argv)
    if opts is None:
        print("usage: update_docs_ops_budget [--source live|<sha>] [--callers N] [--workdir DIR] [--no-door]", file=sys.stderr)
        return 2
    from coordinator_core.benchmarks import declare_benchmark_origin

    declare_benchmark_origin()
    callers = opts["callers"] or default_callers()
    own_dir = opts["workdir"] is None
    if own_dir:
        scratch_root = repo_root() / "scratch"
        scratch_root.mkdir(exist_ok=True)
        from coordinator_core.temp_layout import mkdtemp_in

        workdir = mkdtemp_in("update-docs-ops-budget-", scratch_root)
    else:
        workdir = Path(opts["workdir"]).resolve()
        workdir.mkdir(parents=True, exist_ok=True)
    try:
        report = measure(opts["source"], workdir, callers, with_door=opts["door"])
    finally:
        if own_dir:
            _remove_tree(workdir)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
