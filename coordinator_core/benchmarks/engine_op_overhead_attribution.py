"""Attribute the warm engine's per-op overhead to dispatch, job_object, json, extra_process, residual.

    python -m coordinator_core.benchmarks.engine_op_overhead_attribution <dir> [--json-out PATH]

Process time (job accounting or in-driver `process_time` over hundreds of calls) and job process
counts only, never wall clock. The engine's own functions are driven here under job accounting; no
tracing dial is added to the production dispatch path. Windows only (job objects).

Segments per probe (`ping`, `doc-staleness`), ms per call:
  dispatch       `_pool_dispatch_worker` on a zero-work op (identity scope, settings-home repair,
                 validation, suspension, registry lookup, telemetry rows) plus, for an entrypoint
                 op, `_load_entrypoint_main`'s per-call `exec_module`.
  job_object     `run_git` with the charging Job forced on minus off, times the op's git spawns.
  json           the server's line encode/decode plus the pickle round trip of the request and
                 response across the pool boundary (real payloads).
  extra_process  unexplained job processes per call (warm minus in-process) times the measured cost
                 of one pool-worker start.
  residual       measured overhead minus the four above; never dropped.

Internal drivers (`--driver ...`) are the in-process halves the orchestrator measures through
`engine_op_overhead.in_process_sample_ms`; each takes `--calls N` (0 is its setup-only run).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import pickle
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Sequence

from coordinator_core.benchmarks import declare_benchmark_origin
from coordinator_core.benchmarks.engine_op_overhead import (
    CALLS_FLAG,
    OverheadSample,
    SEGMENTS,
    in_process_sample_ms,
    isolated_warm_engine,
)
from coordinator_core.win_portability import no_console_creationflags

#: Counted samples per probe and calls per sample. Question: "how many calls amortise the ~15.6ms
#: job tick?"
SAMPLES = 14
K_CALLS = 3
#: Calls per in-driver stage loop. Question: "how many loops put a ~0.1ms stage above the tick?"
_STAGE_CALLS = 300
#: Loops for the sub-0.1ms serialisation stages. Question: "how many loops lift a ~20us stage over the tick?"
_SERIALISE_CALLS = 5000
#: Spawns per git job-object driver run. Question: "how many spawns resolve a ~1ms job delta?"
_GIT_CALLS = 30
_GIT_ROUNDS = 5
#: Idle observation with no calls. Question: "does the engine charge process time to nobody's op?"
_IDLE_SECS = 12.0
_NAMING_CALLS = 15
#: Forced skew refreshes per driver run. Question: "how many refreshes resolve a multi-ms hash walk?"
_REFRESH_CALLS = 6
_CALL_TIMEOUT_S = 120

_ENTRYPOINT = "workweek-complete-doc-staleness"
_FIXTURE_COMMITS = 30
_MODULE = "coordinator_core.benchmarks.engine_op_overhead_attribution"
_PROBES = ("ping", "doc-staleness")
_GIT_SPAWNS_PER_CALL = {"ping": 0, "doc-staleness": 3}


def _source_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _door_dir() -> Path:
    home = os.environ.get("COORDINATOR_SETTINGS_HOME") or Path.home() / ".coordinator-claude-settings"
    return Path(home) / "bin"


def _git(repo: Path, *args: str) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "bench", "GIT_AUTHOR_EMAIL": "b@x", "GIT_COMMITTER_NAME": "bench", "GIT_COMMITTER_EMAIL": "b@x"}
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env=env, **no_console_creationflags())


def ensure_fixture(workdir: Path) -> Path:
    """A small repo with README.md and INSTALL.md and a few dozen commits; reused when present."""
    repo = workdir / "fixture"
    if (repo / ".git").is_dir():
        return repo
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    for n in range(_FIXTURE_COMMITS):
        for doc in ("README.md", "INSTALL.md"):
            if n % 7 == 0 or doc == "README.md":
                (repo / doc).write_text(f"# {doc}\n" + "".join(f"line {i} rev {n}\n" for i in range(20)), encoding="utf-8")
        (repo / f"src{n % 5}.txt").write_text(f"{n}\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", f"c{n}")
    return repo


# --------------------------------------------------------------------------- in-process drivers


def _calls(argv: Sequence[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--driver", required=True)
    p.add_argument("--repo", default=None)
    p.add_argument("--out", default=None)
    p.add_argument(CALLS_FLAG, type=int, default=0, dest="calls")
    return p.parse_args(list(argv))


def _driver_ping(calls: int) -> None:
    from coordinator_core.warm.entry_seam import reentrant_dispatch

    for _ in range(calls):
        reentrant_dispatch("ping", {})


def _driver_doc_staleness(calls: int, repo: str) -> None:
    from coordinator_core.ops.doc_registry import resolve_doc_registry_config
    from coordinator_core.ops.doc_staleness import build_doc_staleness_report

    for _ in range(calls):
        registry = resolve_doc_registry_config(repo)
        build_doc_staleness_report(
            repo, registry.human_facing_docs or [],
            registry.doc_staleness_commits, registry.doc_staleness_days,
        )


def _driver_pool_hop(calls: int, repo: str) -> None:
    """Job-diffed cost of a real one-worker pool round trip on an echo target (both processes)."""
    msg, response = _real_payload(repo)
    pool = concurrent.futures.ProcessPoolExecutor(max_workers=1)
    try:
        pool.submit(_echo, msg, None).result()
        for _ in range(calls):
            pool.submit(_echo, msg, None).result()
    finally:
        pool.shutdown()


def _driver_skew_refresh(calls: int) -> None:
    """Forced `ServerVersionState.refresh`: the throttled axis-2 check the accept path runs."""
    from coordinator_core.warm import skew

    state = skew.ServerVersionState()
    for _ in range(calls):
        state.refresh(force=True)


def _driver_skew_part(calls: int, part: str) -> None:
    """One leg of `ServerVersionState.refresh`: the stat stamp (every refresh) or the source hash (only when the stamp moved)."""
    from coordinator_core import lifecycle
    from coordinator_core.warm import skew

    legs = {
        "skew-stamp": lambda: skew._source_stamp(skew._source_pkg_dir()),
        "skew-hash": lifecycle._compute_core_version,
    }
    for _ in range(calls):
        legs[part]()


def _echo(msg, caller):
    return msg


def _driver_git_job(calls: int, repo: str, *, job: bool) -> None:
    from coordinator_core import ipc
    from coordinator_core.git.run import run_git

    token = ipc._op_cpu_secs_var.set([]) if job else None
    try:
        for _ in range(calls):
            run_git(["rev-parse", "HEAD"], cwd=repo)
    finally:
        if token is not None:
            ipc._op_cpu_secs_var.reset(token)


def _real_payload(repo: str) -> tuple[dict, dict]:
    """The request an `invoke.from_argv` door call carries and its real response."""
    from coordinator_core.ops.invoke_from_argv import _run_entrypoint

    argv = ["--root", repo]
    result = _run_entrypoint(_ENTRYPOINT, argv, repo, "")
    msg = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "invoke.from_argv",
        "params": {"argv": argv, "cwd": repo, "entrypoint": _ENTRYPOINT},
        "_engine_token": "sha:" + "0" * 40,
        "_caller": {"cwd": repo, "pid": "12345", "session_id": "0" * 36, "plugin_root": repo},
    }
    return msg, {"jsonrpc": "2.0", "id": 7, "result": result}


def _loop_ms(fn, calls: int) -> float:
    fn()
    start = time.process_time()
    for _ in range(calls):
        fn()
    return (time.process_time() - start) * 1000.0 / calls


def _driver_dispatch_stages(repo: str, out: str) -> None:
    """Per-call process time of each dispatch stage on a zero-work op, in this interpreter."""
    import asyncio

    from coordinator_core import ipc
    from coordinator_core.ops import invoke_from_argv as ifa
    from coordinator_core.warm import server
    from coordinator_core.warm.entry_seam import per_request_state

    ipc.allow_unstamped_dispatch()
    ipc.register_op("engine_overhead.noop", _noop_handler)
    msg = {"jsonrpc": "2.0", "id": 1, "method": "engine_overhead.noop", "params": {}}
    ipc_dispatch = ipc.dispatch_message
    server._worker_pristine_settings_home = os.environ.get("COORDINATOR_SETTINGS_HOME")
    resp_msg, response = _real_payload(repo)
    script = ifa._resolve_entrypoint_script(_ENTRYPOINT)

    def state():
        with per_request_state(env=None, session_id=None, settings_home=None, caller_pid=None,
                               diagnostics=[], warm_served=True, isolated=True):
            pass

    def impl():
        asyncio.run(ipc._dispatch_message_impl(msg))

    def wrapped():
        asyncio.run(ipc_dispatch(msg, caller="bench", corr_id="c"))

    def record():
        ipc.record_op_process_time(op="engine_overhead.noop", process_ms=1.0,
                                   measurement_scope=ipc.MEASUREMENT_SCOPE_PER_OP_PROCESS,
                                   source_path="pool_worker", t_start=time.time(), repo_root=Path(repo),
                                   corr_id="c", caller="bench")

    from coordinator_core.warm.caller_context import CallerContext

    declared_env = {k: v for k, v in os.environ.items() if k.startswith(("CLAUDE", "COORDINATOR"))}
    declared_env.setdefault("CLAUDE_PID", "12345")
    caller = CallerContext(
        plugin_root=repo, cwd=repo, session_id="0" * 8 + "-0000-0000-0000-" + "0" * 12,
        agent_id=None, pid="12345", settings_home=None, env=declared_env,
    )

    def whole_bare():
        server._pool_dispatch_worker(msg, None)

    def whole():
        server._pool_dispatch_worker(msg, caller)

    def reload():
        ifa._load_entrypoint_main(script, _ENTRYPOINT)

    def line_codec(m=resp_msg, r=response):
        server._parse_frame(json.dumps(m).encode() + b"\n")
        server._encode(r)

    def pickle_rt(m=resp_msg, r=response):
        pickle.loads(pickle.dumps((server._pool_dispatch_worker, m, None)))
        pickle.loads(pickle.dumps(r))

    from coordinator_core.warm import skew

    skew._REFRESH_INTERVAL_SECS = 1e9
    stamped = Path(out).parent / "stamped-root"
    (stamped / "coordinator_core").mkdir(parents=True, exist_ok=True)
    (stamped / "coordinator_core" / "_engine_stamp").write_text("sha:attribution\n", encoding="utf-8")
    version_state = skew.ServerVersionState(stamped)
    token = skew.compute_client_token(version_state._root)
    wire = json.dumps({**resp_msg, "_engine_token": token}).encode() + b"\n"
    stub_response = response

    def accept():
        server._serve_line(
            wire, write=lambda _b: None, version_state=version_state, server_sha=version_state.server_sha,
            close_listener=lambda: None, drain=lambda: None, release_in_flight=lambda: None,
            dispatch=lambda m, **kw: stub_response,
        )

    real_load = ifa._load_entrypoint_main

    def entrypoint_shell():
        ifa._load_entrypoint_main = lambda _s, _e: (lambda _argv=None: 0)
        try:
            ifa._run_entrypoint(_ENTRYPOINT, ["--root", repo], repo, "")
        finally:
            ifa._load_entrypoint_main = real_load

    stages = {
        "serve_line_accept_path": accept,
        "run_entrypoint_shell": entrypoint_shell,
        "per_request_state": state,
        "settings_home_repair": server._repair_settings_home_to_pristine,
        "dispatch_message_impl": impl,
        "dispatch_message": wrapped,
        "record_op_process_time": record,
        "pool_dispatch_worker_total": whole,
        "pool_dispatch_worker_bare": whole_bare,
        "entrypoint_reload": reload,
        "json_line_codec": line_codec,
        "pickle_round_trip": pickle_rt,
    }
    refused = asyncio.run(ipc_dispatch(msg, caller="bench", corr_id="c"))
    if "error" in refused:
        raise RuntimeError(f"zero-work op was refused, stages would time the refusal: {refused['error']}")
    result = {
        name: _loop_ms(fn, _SERIALISE_CALLS if name in ("json_line_codec", "pickle_round_trip") else _STAGE_CALLS)
        for name, fn in stages.items()
    }
    Path(out).write_text(json.dumps(result), encoding="utf-8")


async def _noop_handler(params, repo_root=None):
    return {}


def _run_driver(ns: argparse.Namespace) -> int:
    d = ns.driver
    if d == "ping":
        _driver_ping(ns.calls)
    elif d == "doc-staleness":
        _driver_doc_staleness(ns.calls, ns.repo)
    elif d == "pool-hop":
        _driver_pool_hop(ns.calls, ns.repo)
    elif d == "skew-refresh":
        _driver_skew_refresh(ns.calls)
    elif d in ("skew-stamp", "skew-hash"):
        _driver_skew_part(ns.calls, d)
    elif d == "git-job-on":
        _driver_git_job(ns.calls, ns.repo, job=True)
    elif d == "git-job-off":
        _driver_git_job(ns.calls, ns.repo, job=False)
    elif d == "dispatch-stages":
        _driver_dispatch_stages(ns.repo, ns.out)
    else:
        raise SystemExit(f"unknown driver {d!r}")
    return 0


# --------------------------------------------------------------------------- orchestrator


def _in_process(driver: str, repo: Path, k: int) -> tuple[float, float]:
    return in_process_sample_ms([sys.executable, "-m", _MODULE, "--driver", driver, "--repo", str(repo)], k=k)


def _dispatch_stage_driver(workdir: Path, repo: Path) -> dict:
    from coordinator_core.benchmarks.process_time import batched_process_time_ms

    out = workdir / "dispatch-stages.json"
    run = batched_process_time_ms(
        [sys.executable, "-m", _MODULE, "--driver", "dispatch-stages", "--repo", str(repo), "--out", str(out)],
        k=1, cwd=str(repo), env={**os.environ, "PYTHONPATH": str(_source_root())},
    )
    if run["rc"] != 0 or not out.is_file():
        raise RuntimeError(f"dispatch-stages driver failed rc={run['rc']}")
    stages = json.loads(out.read_text(encoding="utf-8"))
    stages["_driver_job_procs"] = run["procs_per_call"]
    return stages


def _door_cmd(probe: str, repo: Path) -> list:
    bins = _door_dir()
    if probe == "ping":
        return [str(bins / "coordinator-invoke.exe"), "ping", "{}"]
    return [str(bins / f"{_ENTRYPOINT}.exe"), "--root", str(repo)]


def _call(cmd: list, env: dict) -> None:
    done = subprocess.run(cmd, capture_output=True, env=env, timeout=_CALL_TIMEOUT_S, **no_console_creationflags())
    if done.returncode != 0:
        raise RuntimeError(f"door call rc={done.returncode}: {done.stderr[:300]!r}")


def _cpu_by_pid(engine_pid: int) -> dict:
    """pid -> user+system ms for the engine process and every live descendant (psutil, per pid)."""
    import psutil

    parent = psutil.Process(engine_pid)
    out = {}
    for proc in [parent, *parent.children(recursive=True)]:
        try:
            t = proc.cpu_times()
            out[proc.pid] = (t.user + t.system) * 1000.0
        except psutil.Error:
            continue
    return out


def _pool_children(pid: int) -> int:
    import psutil

    return len([c for c in psutil.Process(pid).children(recursive=True) if "conhost" not in c.name().lower()])


def _warm_side(repo: Path, source_root: Path) -> dict:
    from coordinator_core.benchmarks.process_time import LiveTreeAccountant, _JobImageNamer
    from coordinator_core.warm.server import DISPATCH_PROCESS_POOL_SIZE

    out: dict = {"samples": {}, "pool_size": DISPATCH_PROCESS_POOL_SIZE}
    with isolated_warm_engine(source_root, entrypoints=[_ENTRYPOINT]) as engine, \
            LiveTreeAccountant(engine.pid) as acct:
        before = acct.snapshot()
        _call(_door_cmd("ping", repo), engine.env)
        after = acct.snapshot()
        out["first_call"] = {
            "ms": after["process_time_ms"] - before["process_time_ms"],
            "procs": after["procs"] - before["procs"],
        }
        before = acct.snapshot()
        for probe in _PROBES:
            for _ in range(2):
                with concurrent.futures.ThreadPoolExecutor(DISPATCH_PROCESS_POOL_SIZE) as ex:
                    list(ex.map(lambda _i: _call(_door_cmd(probe, repo), engine.env), range(DISPATCH_PROCESS_POOL_SIZE)))
        after = acct.snapshot()
        out["warm_up"] = {
            "ms": after["process_time_ms"] - before["process_time_ms"],
            "procs": after["procs"] - before["procs"],
            "pool_children": _pool_children(engine.pid),
        }
        cb = _cpu_by_pid(engine.pid)
        b = acct.snapshot()
        time.sleep(_IDLE_SECS)
        a = acct.snapshot()
        ca = _cpu_by_pid(engine.pid)
        out["idle"] = {
            "ms_per_s": (a["process_time_ms"] - b["process_time_ms"]) / _IDLE_SECS,
            "procs_per_s": (a["procs"] - b["procs"]) / _IDLE_SECS,
            "accept_process_ms_per_s": (ca[engine.pid] - cb[engine.pid]) / _IDLE_SECS,
            "surviving_workers_ms_per_s": sum(ca[p] - cb[p] for p in ca if p in cb and p != engine.pid) / _IDLE_SECS,
        }
        for probe in _PROBES:
            cmd = _door_cmd(probe, repo)
            rows = []
            split = []
            walls = []
            for _ in range(SAMPLES):
                t0 = time.perf_counter()
                cb = _cpu_by_pid(engine.pid)
                b = acct.snapshot()
                for _i in range(K_CALLS):
                    _call(cmd, engine.env)
                a = acct.snapshot()
                ca = _cpu_by_pid(engine.pid)
                walls.append(time.perf_counter() - t0)
                rows.append(((a["process_time_ms"] - b["process_time_ms"]) / K_CALLS, (a["procs"] - b["procs"]) / K_CALLS))
                accept = (ca[engine.pid] - cb[engine.pid]) / K_CALLS
                workers = sum(ca[p] - cb[p] for p in ca if p in cb and p != engine.pid) / K_CALLS
                split.append((accept, workers))
            out["samples"][probe] = {
                "ms": statistics.fmean(r[0] for r in rows),
                "procs": statistics.fmean(r[1] for r in rows),
                "ms_median": statistics.median(r[0] for r in rows),
                "procs_median": statistics.median(r[1] for r in rows),
                "windows": [[r[0], r[1]] for r in rows],
                "window_secs_mean": statistics.fmean(walls),
                "accept_process_ms_median": statistics.median(s[0] for s in split),
                "surviving_workers_ms_median": statistics.median(s[1] for s in split),
            }
        out["naming"] = {}
        for probe in _PROBES:
            namer = _JobImageNamer(acct._job)
            namer.start()
            time.sleep(1.5)
            preexisting = set(namer._images)
            cmd = _door_cmd(probe, repo)
            b = acct.snapshot()
            for _ in range(_NAMING_CALLS):
                _call(cmd, engine.env)
            a = acct.snapshot()
            images = namer.stop()
            tally: dict = {}
            for pid, image in images.items():
                if pid not in preexisting:
                    tally[image] = tally.get(image, 0) + 1
            out["naming"][probe] = {
                "calls": _NAMING_CALLS,
                "job_procs": a["procs"] - b["procs"],
                "started_images": dict(sorted(tally.items())),
                "unnamed": (a["procs"] - b["procs"]) - sum(tally.values()),
            }
    return out


def _fit_extra_process(points: Sequence[tuple[float, float]]) -> tuple[float, float]:
    """(intercept ms, slope ms per unexplained process) of window overhead against window excess
    processes. Windows that caught a periodic engine-side process sit on the slope; the intercept
    is the overhead of a window that caught none. No spread in x means no slope: (mean y, 0)."""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    var = sum((x - mx) ** 2 for x in xs)
    if var < 1e-9:
        return my, 0.0
    slope = sum((x - mx) * (y - my) for x, y in points) / var
    slope = max(slope, 0.0)
    return my - slope * mx, slope


def attribute(workdir: Path) -> dict:
    workdir.mkdir(parents=True, exist_ok=True)
    repo = ensure_fixture(workdir)
    stages = _dispatch_stage_driver(workdir, repo)
    on_runs, off_runs = [], []
    for _ in range(_GIT_ROUNDS):
        on_runs.append(_in_process("git-job-on", repo, _GIT_CALLS)[0])
        off_runs.append(_in_process("git-job-off", repo, _GIT_CALLS)[0])
    on_ms, off_ms = statistics.median(on_runs), statistics.median(off_runs)
    job_delta = statistics.median(a - b for a, b in zip(on_runs, off_runs))
    skew_legs = {leg: _in_process(leg, repo, 3) for leg in ("skew-stamp", "skew-hash")}
    hop_ms, hop_procs = _in_process("pool-hop", repo, _STAGE_CALLS)
    refresh_ms, refresh_procs = _in_process("skew-refresh", repo, _REFRESH_CALLS)
    warm = _warm_side(repo, _source_root())
    first = warm["first_call"]

    rows = []
    fits: dict = {}
    for probe in _PROBES:
        in_ms, in_procs = _in_process(probe, repo, 3 if probe == "doc-staleness" else _STAGE_CALLS)
        sample = OverheadSample(probe, warm["samples"][probe]["ms"], in_ms, warm["samples"][probe]["procs"], in_procs)
        dispatch = (
            stages["serve_line_accept_path"]
            + stages["pool_dispatch_worker_total"]
            + ((stages["entrypoint_reload"] + stages["run_entrypoint_shell"]) if probe != "ping" else 0.0)
        )
        json_ms = stages["json_line_codec"] + stages["pickle_round_trip"]
        job_ms = max(job_delta, 0.0) * _GIT_SPAWNS_PER_CALL[probe]
        base_ms, ms_per_proc = _fit_extra_process(
            [(w[1] - in_procs, w[0] - in_ms) for w in warm["samples"][probe]["windows"]]
        )
        extra_ms = ms_per_proc * max(sample.unexplained_procs, 0.0)
        residual = sample.overhead_ms - dispatch - job_ms - json_ms - extra_ms
        fits[probe] = {"base_overhead_ms_no_extra_process": base_ms, "ms_per_unexplained_proc": ms_per_proc}
        # The idle rate times a window's duration apportions background engine work (no call
        # needed to trigger it) to that window; the duration only scales a rate, it is not a metric.
        idle_per_call = warm["idle"]["ms_per_s"] * warm["samples"][probe]["window_secs_mean"] / K_CALLS
        idle_beyond_extra = max(idle_per_call - extra_ms, 0.0)
        rows.append({
            "idle_tax_ms_per_call": idle_per_call,
            "residual_after_idle_tax_ms": residual - idle_beyond_extra,
            "probe": probe,
            "overhead_ms": sample.overhead_ms,
            "unexplained_procs": sample.unexplained_procs,
            "warm_op_ms": sample.warm_op_ms,
            "in_process_ms": sample.in_process_ms,
            "warm_procs": sample.warm_procs,
            "in_process_procs": sample.in_process_procs,
            "segments": dict(zip(SEGMENTS, (dispatch, job_ms, json_ms, extra_ms, residual))),
            "extra_process_procs": max(sample.unexplained_procs, 0.0),
        })
    return {
        "rows": rows,
        "stages": stages,
        "git_job": {"on_ms_per_spawn": on_ms, "off_ms_per_spawn": off_ms, "delta_ms_per_spawn": job_delta,
                    "delta_rounds": [round(a - b, 2) for a, b in zip(on_runs, off_runs)]},
        "skew_legs": {leg: {"ms": v[0], "procs": v[1]} for leg, v in skew_legs.items()},
        "pool_hop_total": {"ms_per_call": hop_ms, "procs_per_call": hop_procs,
                           "transport_beyond_pickle_ms": hop_ms - stages["pickle_round_trip"]},
        "skew_refresh": {"ms_per_refresh": refresh_ms, "procs_per_refresh": refresh_procs},
        "extra_process_fit": fits,
        "pool_fill": {"first_call": first, "warm_up": warm["warm_up"]},
        "idle": warm["idle"],
        "warm_windows": warm["samples"],
        "naming": warm["naming"],
        "pool_size": warm["pool_size"],
    }


def render(result: dict) -> str:
    lines = ["| probe | segment | ms/call | procs/call | instrument |", "|---|---|---|---|---|"]
    instrument = {
        "dispatch": f"in-driver process_time x{_STAGE_CALLS}, zero-work op",
        "job_object": f"job-diff run_git on/off x{_GIT_CALLS} spawns, times spawns/call",
        "json": f"in-driver process_time x{_STAGE_CALLS}, real payloads",
        "extra_process": "unexplained job procs x ms per proc (least-squares over warm windows)",
        "residual": "measured overhead minus the four",
    }
    for row in result["rows"]:
        for seg in SEGMENTS:
            procs = row["extra_process_procs"] if seg == "extra_process" else 0
            lines.append(f"| {row['probe']} | {seg} | {row['segments'][seg]:.2f} | {procs:.2f} | {instrument[seg]} |")
        lines.append(
            f"| {row['probe']} | (detail) engine idle tax apportioned to a call | {row['idle_tax_ms_per_call']:.2f} | 0.00 | "
            f"idle {result['idle']['ms_per_s']:.1f}ms/s, {result['idle']['procs_per_s']:.2f}procs/s x window |"
        )
        lines.append(
            f"| {row['probe']} | (detail) residual after idle tax | {row['residual_after_idle_tax_ms']:.2f} | 0.00 | "
            f"residual minus idle tax not already in extra_process |"
        )
        lines.append(
            f"| {row['probe']} | TOTAL overhead | {row['overhead_ms']:.2f} | {row['unexplained_procs']:.2f} | "
            f"warm {row['warm_op_ms']:.1f}ms/{row['warm_procs']:.2f}p - in-process "
            f"{row['in_process_ms']:.1f}ms/{row['in_process_procs']:.2f}p, samples={SAMPLES}x{K_CALLS} |"
        )
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    declare_benchmark_origin()
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--driver" in argv:
        return _run_driver(_calls(argv))
    p = argparse.ArgumentParser(prog="engine_op_overhead_attribution")
    p.add_argument("workdir", type=Path)
    p.add_argument("--json-out", type=Path, default=None)
    ns = p.parse_args(argv)
    if sys.platform != "win32":
        print("engine_op_overhead_attribution: job-object accounting is Windows-only", file=sys.stderr)
        return 0
    result = attribute(ns.workdir.resolve())
    print(render(result))
    print("DETAIL " + json.dumps({k: v for k, v in result.items() if k != "rows"}, sort_keys=True, default=str))
    if ns.json_out:
        ns.json_out.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
