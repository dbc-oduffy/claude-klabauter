"""coordinator_core.benchmarks.warm_restart_stall_bench -- the server-only-restart-under-load falsifier.

Purpose: answer "after a server-only restart under the incident's load shape, do ping and a light
read op answer under the 500ms bar, or does the replacement server wedge in `_await_pool_result`?"
Plan: docs/plans/2026-11/2026-11-24-hnd-warm-engine-pool-stall-root-ca-6ebb46.md (rows C2, C4).

TOPOLOGY. A hardlink clone of this tree is stamped as its own engine root under `scratch/`, then
three processes are booted from it: the http supervisor, a front door, and the warm server. A bare
server cannot reproduce poison held by the front door or supervisor, because `hang_restart.exit_server`
kills only the server pid and leaves both alive. The state dir is `breadcrumb.svc_dir(clone)`, so
claims, ack journal and discovery files persist across the restart exactly as in production.

FRONT DOOR. `front_door.main` binds the machine-global `FIXED_PORT`, and a bench door there would
hijack (or lose to) the box's real door. The bench therefore runs `_role_front_door`, which is
`front_door.main` with `elect_front_door(port=0)`: same election, context, handler and discovery
record, an ephemeral port.

LOAD. `--load-workers` serial loops of a slow mutating op (default `dispatch.emit`) exceed the pool
and build a queue, a ping stream runs throughout, then `hang_restart.exit_server(server_pid)` fires
mid-load and the bench starts the replacement server (the production respawn is a client's next
op; the bench spawns it itself so it stays inside the clone's cleanup job).

MEASURE. Verdict axes are server process time (`cpu_ms` rows in request telemetry for the replacement
pid) and spawn count (Windows job accounting over a quiet bracket); wall clock is printed for
diagnosis and is never a verdict. A probe with no answer inside `--probe-deadline` is an op parked
past its deadline and breaches on its own: a wedged server writes no telemetry row at all.

JOURNAL PATH. The stall under test is a slow ack-journal write on the pool's management thread
(`_pool_dispatch_untimed` -> `_stamp_pool_future` -> `AckStore.stamp` -> `dispatch_ack.record_outcome`),
so generic load alone is not proof. The bench (a) seeds today's `ack-journal/d<UTC day>/` bucket with
`--journal-bucket-files` files (the incident held ~140k) and (b) boots every server of the clone with a
`sitecustomize` shim, written under the clone's tmp parent and never into shipped code, that sleeps
`--journal-delay-ms` inside `dispatch_ack._write_json` for outcome entries in the accept process only
(pool workers are untouched). Each delayed write appends a trace row naming the writing thread and how
many threads were parked in `_await_pool_result` mid-delay; the report's `journal.wedge_confirmed`
is true only when a write on the pool's executor-manager thread (identified by its stack, since the
thread is unnamed) overlapped a parked `_await_pool_result` thread. `--source-root` points the clone at
a baseline export of an older sha; the bench copies itself into the clone so its roles still run.
The default read op is `diagnostics.always_succeeds`: COMPUTE_ONLY, cheap, and served by the dispatch
pool whose management thread the stamp blocks (`ping` rides the separate guard pool).

Standalone: `python coordinator_core/benchmarks/warm_restart_stall_bench.py [--json-out PATH]
[--assert-under-bar]`. Roles `load`, `probe` and `front-door` run inside the clone.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator, List, Optional

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.benchmarks import isolated_clone as ic
from coordinator_core.win_portability import no_console_creationflags

BAR_MS = 500.0
ENTRYPOINT = "coordinator-invoke"
_MODULE = "coordinator_core.benchmarks.warm_restart_stall_bench"
_BOOT_WAIT_SECS = 90.0
_TELEMETRY_WAIT_SECS = 12.0
_CONSECUTIVE_MISS_ABORT = 3

_EMIT_PARAMS = {
    "plan_path": "{work}/plan.md",
    "output_path": "{work}/out/emit-{n}.mjs",
    "target_root": "{work}",
    "repo_root": "{work}",
}


_SHIM_DELAY_ENV = "COORDINATOR_BENCH_JOURNAL_DELAY_MS"
_SHIM_TRACE_ENV = "COORDINATOR_BENCH_JOURNAL_TRACE"
_SHIM_SOURCE = '''
import importlib.abc, importlib.util, os, sys

_TARGET = "coordinator_core.warm.dispatch_ack"


def _patch(mod):
    import json, multiprocessing, threading, time
    delay = float(os.environ.get("%(delay)s") or 0) / 1000.0
    trace = os.environ.get("%(trace)s")
    if delay <= 0 or multiprocessing.parent_process() is not None:
        return
    real = mod._write_json

    def slow_write_json(journal_dir, path, payload):
        if not str(path).endswith(mod._JOURNAL_SUFFIX):
            return real(journal_dir, path, payload)
        me = threading.current_thread()
        names, frame = set(), sys._getframe(1)
        while frame is not None:
            names.add((os.path.basename(frame.f_code.co_filename), frame.f_code.co_name))
            frame = frame.f_back
        on_manager = ("process.py", "run") in names and ("_base.py", "_invoke_callbacks") in names
        time.sleep(delay / 2)
        parked = 0
        for tid, frame in sys._current_frames().items():
            while frame is not None:
                if frame.f_code.co_name == "_await_pool_result":
                    parked += 1
                    break
                frame = frame.f_back
        time.sleep(delay / 2)
        if trace:
            try:
                with open(trace, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"t": time.time(), "thread": me.name, "on_executor_manager": on_manager, "pid": os.getpid(),
                                         "delay_ms": delay * 1000.0, "parked_in_await_pool_result": parked}) + "\\n")
            except OSError:
                pass
        return real(journal_dir, path, payload)

    mod._write_json = slow_write_json


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name != _TARGET:
            return None
        sys.meta_path.remove(self)
        try:
            spec = importlib.util.find_spec(name)
        finally:
            sys.meta_path.insert(0, self)
        if spec is None or spec.loader is None:
            return spec
        real_exec = spec.loader.exec_module

        def exec_module(module):
            real_exec(module)
            _patch(module)

        spec.loader.exec_module = exec_module
        return spec


if os.environ.get("%(delay)s"):
    sys.meta_path.insert(0, _Finder())
''' % {"delay": _SHIM_DELAY_ENV, "trace": _SHIM_TRACE_ENV}


def _seed_journal_bucket(root: Path, files: int) -> tuple:
    """Fill today's UTC-day journal bucket under the clone's svc dir; returns `(bucket, count)`."""
    from coordinator_core.warm import breadcrumb, dispatch_ack

    bucket = Path(dispatch_ack.journal_dir_under(breadcrumb.svc_dir(root))) / dispatch_ack._bucket_name(time.time())
    bucket.mkdir(parents=True, exist_ok=True)
    for i in range(files):
        (bucket / f"seed-{i}{dispatch_ack._JOURNAL_SUFFIX}").write_bytes(b"{}")
    return bucket, files


def _journal_report(trace: Path, bucket: Path, seeded: int, delay_ms: float) -> dict:
    rows: List[dict] = []
    with contextlib.suppress(OSError):
        for line in trace.read_text(encoding="utf-8").splitlines():
            with contextlib.suppress(ValueError):
                rows.append(json.loads(line))
    mgmt = [r for r in rows if r.get("on_executor_manager")]
    overlap = [r for r in mgmt if r["parked_in_await_pool_result"] > 0]
    return {
        "bucket": str(bucket),
        "bucket_files_seeded": seeded,
        "write_delay_ms": delay_ms,
        "delayed_writes": len(rows),
        "writes_by_thread": {name: sum(1 for r in rows if r["thread"] == name) for name in sorted({r["thread"] for r in rows})},
        "management_thread_writes": len(mgmt),
        "max_parked_in_await_pool_result_during_write": max([r["parked_in_await_pool_result"] for r in rows], default=0),
        "wedge_confirmed": bool(overlap),
    }


def _percentile(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(round(pct / 100.0 * (len(ordered) - 1))))], 1)


def _fill(value: object, n: int, work: str) -> object:
    if isinstance(value, str):
        return value.replace("{n}", str(n)).replace("{work}", work)
    if isinstance(value, dict):
        return {k: _fill(v, n, work) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, n, work) for v in value]
    return value


def _role_client(args: argparse.Namespace) -> int:
    """`load` and `probe` roles: fire `method` through the warm client (never spawning a server) and
    print one JSON row per call. Runs inside the clone so the client resolves the clone's endpoint."""
    from coordinator_core.warm import client

    template = json.loads(args.params)
    end = time.time() + args.secs if args.secs else None
    misses = 0
    n = 0
    while (end is None or time.time() < end) and (not args.count or n < args.count):
        n += 1
        msg = {"jsonrpc": "2.0", "id": n, "method": args.method, "params": _fill(template, n, args.work)}
        t0 = time.time()
        resp = client.try_warm_dispatch_no_spawn(msg, read_deadline_secs=args.deadline)
        row = {
            "method": args.method,
            "t_send": t0,
            "wait_s": round(time.time() - t0, 3),
            "answered": resp is not None,
            "error": (resp or {}).get("error", {}).get("code") if isinstance(resp, dict) else None,
            "cold_reason": None if resp is not None else client.last_cold_reason(),
        }
        print(json.dumps(row), flush=True)
        misses = 0 if resp is not None else misses + 1
        if args.until_answered and resp is not None:
            break
        if args.role == "probe" and not args.until_answered and misses >= _CONSECUTIVE_MISS_ABORT:
            break
        if resp is None or args.interval:
            time.sleep(max(args.interval, 0.2 if resp is None else 0.0))
    return 0


def _role_front_door() -> int:
    from http.server import ThreadingHTTPServer

    from coordinator_core.warm import front_door as fd
    from coordinator_core.warm import skew

    root = fd.current_engine_clone()
    sock = fd.elect_front_door(engine_root=root, port=0)

    class _NotYetBound:
        pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _NotYetBound, bind_and_activate=False)
    httpd.socket.close()
    httpd.socket = sock
    httpd.server_address = sock.getsockname()
    ctx = fd._FrontDoorContext(httpd=httpd, engine_root=root)
    httpd.RequestHandlerClass = fd._make_handler(ctx)
    ctx.dials.flush()
    fd.write_discovery(
        port=httpd.server_address[1],
        pid=os.getpid(),
        stable_pid_start_epoch=fd._self_stable_pid_start_epoch() or 0,
        engine_sha=skew.compute_client_token(root),
        engine_root=root,
    )
    try:
        httpd.serve_forever()
    finally:
        ctx.ctx_shutdown()
    return 0


def _spawn_module(root: Path, env: dict, module: str) -> tuple:
    """Start `python -m module` in `root` inside its own kill-on-close job (Windows), so the process
    and everything it spawns die with the bench even when no `finally` runs. Returns `(proc, job)`."""
    argv = [sys.executable, "-m", module]
    if module == _MODULE:
        argv += ["--role", "front-door"]
    common = dict(
        cwd=str(root), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, close_fds=True,
    )
    if sys.platform != "win32":
        return subprocess.Popen(argv, **common, **no_console_creationflags()), None

    from ctypes import WinError, get_last_error, wintypes

    from coordinator_core.benchmarks import process_time as pt

    job = pt._windows_create_kill_on_close_job()
    proc = subprocess.Popen(argv, creationflags=pt._CREATE_SUSPENDED | pt._CREATE_NO_WINDOW, **common)
    try:
        if not pt._k32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(int(proc._handle))):
            raise WinError(get_last_error())
        pt._resume_all_threads(proc.pid)
    except BaseException:
        proc.kill()
        ic.close_clone_job(job)
        raise
    return proc, job


def _wait_server_pid(root: Path, *, not_pid: Optional[int]) -> int:
    from coordinator_core.warm import breadcrumb

    deadline = time.time() + _BOOT_WAIT_SECS
    while time.time() < deadline:
        crumb = breadcrumb.read_breadcrumb(engine_root=root)
        if crumb and crumb.get("pid") and int(crumb["pid"]) != not_pid:
            return int(crumb["pid"])
        time.sleep(0.2)
    raise ic.WarmEngineBootTimeout(f"warm server did not publish a breadcrumb within {_BOOT_WAIT_SECS}s")


def _spawn_client(root: Path, env: dict, role: str, method: str, params: dict, *, work: Path, secs: float = 0.0,
                  count: int = 0, interval: float = 0.0, deadline: float = 30.0,
                  until_answered: bool = False) -> subprocess.Popen:
    argv = [
        sys.executable, "-m", _MODULE, "--role", role, "--method", method, "--params", json.dumps(params),
        "--work", str(work), "--secs", str(secs), "--count", str(count), "--interval", str(interval),
        "--deadline", str(deadline),
    ]
    if until_answered:
        argv.append("--until-answered")
    return subprocess.Popen(
        argv, cwd=str(root), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, **no_console_creationflags(),
    )


def _rows(proc: subprocess.Popen, timeout: float) -> List[dict]:
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
    rows = []
    for line in (out or "").splitlines():
        with contextlib.suppress(ValueError):
            rows.append(json.loads(line))
    return rows


def _telemetry_rows(root: Path, pid: int, since: float) -> List[dict]:
    from coordinator_core.warm import breadcrumb

    rows: List[dict] = []
    for path in sorted(breadcrumb.svc_dir(root).glob("request-telemetry*.jsonl")):
        with contextlib.suppress(OSError):
            for line in path.read_text(encoding="utf-8").splitlines():
                with contextlib.suppress(ValueError):
                    row = json.loads(line)
                    if row.get("pid") == pid and (row.get("enqueue_ts") or 0) >= since:
                        rows.append(row)
    return rows


def _op_stats(rows: List[dict], op: str, probes: List[dict]) -> dict:
    cpu = [float(r["cpu_ms"]) for r in rows if r.get("op") == op and r.get("cpu_ms") is not None]
    span = [(r["end_ts"] - r["enqueue_ts"]) * 1000.0 for r in rows
            if r.get("op") == op and r.get("end_ts") and r.get("enqueue_ts")]
    mine = [p for p in probes if p["method"] == op]
    unanswered = sum(1 for p in mine if not p["answered"])
    return {
        "probes": len(mine),
        "unanswered": unanswered,
        "telemetry_rows": len(cpu),
        "cpu_ms_p50": _percentile(cpu, 50),
        "cpu_ms_p95": _percentile(cpu, 95),
        "diagnostic_wall_ms_p95": _percentile(span, 95),
        "breach": unanswered > 0 or (_percentile(cpu, 95) or 0.0) > BAR_MS,
    }


def _spawn_bracket(root: Path, env: dict, accountant, method: str, work: Path, n: int) -> dict:
    """Spawn count and process time of `n` back-to-back calls on a quiet engine, divided by `n`."""
    if accountant is None:
        return {"spawns_per_op": None, "process_ms_per_op": None, "source": "job accounting is Windows-only"}
    before = accountant.snapshot()
    rows = _rows(_spawn_client(root, env, "probe", method, {}, work=work, count=n, deadline=15.0), 15.0 * n + 30)
    after = accountant.snapshot()
    answered = sum(1 for r in rows if r["answered"])
    if not answered:
        return {"spawns_per_op": None, "process_ms_per_op": None, "source": "no call answered"}
    return {
        "spawns_per_op": round((after["procs"] - before["procs"]) / answered, 2),
        "process_ms_per_op": round((after["process_time_ms"] - before["process_time_ms"]) / answered, 1),
        "source": "server job accounting (includes the probe's own request handling)",
        "answered": answered,
    }


@contextlib.contextmanager
def _topology(source_root: Path, *, journal_files: int = 0, journal_delay_ms: float = 0.0) -> Iterator[tuple]:
    """Yield `(root, env, work, procs, jobs, journal)` for a booted supervisor + front door + server clone,
    its state dir redirected under the clone's own tmp parent so the box's warm dir is untouched.
    `env` carries the journal-delay shim (when `journal_delay_ms`), so every server spawned from it,
    the replacement included, writes slowly; `journal` is `(bucket, seeded, trace_path)`."""
    tmp_parent = ic.mkdtemp_for_clone(source_root, prefix="rsb-")
    root = tmp_parent / "clone"
    jobs: list = []
    procs: dict = {}
    saved_base = os.environ.get("COORDINATOR_WARM_RUNTIME_BASE")
    try:
        ic.clone_engine(source_root, root, entrypoint=ENTRYPOINT)
        own = root / "coordinator_core" / "benchmarks" / Path(__file__).name
        if not own.exists():
            shutil.copyfile(__file__, own)
        env = {
            **ic.engine_env(root),
            "COORDINATOR_ENGINE_ROOT": str(root),
            "COORDINATOR_CLONE_ROOT": str(root),
            "COORDINATOR_WARM_RUNTIME_BASE": str(tmp_parent / "rt"),
        }
        os.environ["COORDINATOR_WARM_RUNTIME_BASE"] = env["COORDINATOR_WARM_RUNTIME_BASE"]
        bucket, seeded = _seed_journal_bucket(root, journal_files)
        trace = tmp_parent / "journal-trace.jsonl"
        if journal_delay_ms > 0:
            shim = tmp_parent / "shim"
            shim.mkdir()
            (shim / "sitecustomize.py").write_text(_SHIM_SOURCE, encoding="utf-8")
            env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(shim), env.get("PYTHONPATH"))))
            env[_SHIM_DELAY_ENV] = str(journal_delay_ms)
            env[_SHIM_TRACE_ENV] = str(trace)
        work = tmp_parent / "work"
        (work / "out").mkdir(parents=True)
        shutil.copyfile(next((source_root / "docs" / "plans").rglob("*hnd-warm-engine-pool-stall-root-ca-*.md")),
                        work / "plan.md")
        subprocess.run(["git", "init", "-q", str(work)], check=True, **no_console_creationflags())
        for name, module in (("supervisor", "coordinator_core.warm.supervisor"), ("front_door", _MODULE),
                             ("server", "coordinator_core.warm.server")):
            procs[name], job = _spawn_module(root, env, module)
            jobs.append(job)
        _wait_server_pid(root, not_pid=None)
        yield root, env, work, procs, jobs, (bucket, seeded, trace)
    finally:
        for proc in procs.values():
            with contextlib.suppress(Exception):
                import psutil

                parent = psutil.Process(proc.pid)
                kids = parent.children(recursive=True)
                for p in (*kids, parent):
                    p.terminate()
                psutil.wait_procs([parent, *kids], timeout=5)
        for job in jobs:
            ic.close_clone_job(job)
        reaped = ic.reap_processes_under(tmp_parent)
        if saved_base is None:
            os.environ.pop("COORDINATOR_WARM_RUNTIME_BASE", None)
        else:
            os.environ["COORDINATOR_WARM_RUNTIME_BASE"] = saved_base
        ic.rmtree_or_raise(tmp_parent, label="warm-restart-stall-bench", reaped=reaped)


def run_bench(source_root: Path, *, load_workers: int, load_op: str, load_params: dict, warmup_secs: float,
              pings: int, reads: int, read_op: str, probe_deadline: float, spawn_ops: int,
              boot_wait: float, journal_files: int, journal_delay_ms: float) -> dict:
    from coordinator_core.benchmarks import process_time as pt
    from coordinator_core.warm import hang_restart

    with _topology(source_root, journal_files=journal_files, journal_delay_ms=journal_delay_ms) as (
        root, env, work, procs, jobs, (bucket, seeded, trace),
    ):
        first_pid = _wait_server_pid(root, not_pid=None)
        load = [_spawn_client(root, env, "load", load_op, load_params, work=work, secs=600.0, deadline=120.0)
                for _ in range(load_workers)]
        stream = _spawn_client(root, env, "load", "ping", {}, work=work, secs=600.0, interval=0.1, deadline=10.0)
        time.sleep(warmup_secs)

        restart_at = time.time()
        hang_restart.exit_server(first_pid)
        procs["server"].wait(timeout=30)
        boot_started = time.time()
        procs["server"], job = _spawn_module(root, env, "coordinator_core.warm.server")
        jobs.append(job)
        new_pid = _wait_server_pid(root, not_pid=first_pid)
        accountant = pt.LiveTreeAccountant(new_pid) if pt.IS_WINDOWS else None

        first = _rows(_spawn_client(root, env, "probe", "ping", {}, work=work, secs=boot_wait, interval=0.5,
                                    deadline=10.0, until_answered=True), boot_wait + 30)
        answered_first = next((r for r in first if r["answered"]), None)
        first_answer_s = None if answered_first is None else round(answered_first["t_send"] + answered_first["wait_s"] - boot_started, 1)

        probes: List[dict] = []
        for method, count in (("ping", pings), (read_op, reads)):
            probes += _rows(_spawn_client(root, env, "probe", method, {}, work=work, count=count,
                                          deadline=probe_deadline), probe_deadline * count + 30)
        for proc in (*load, stream):
            proc.kill()
        load_rows = [r for proc in load for r in _rows(proc, 10)]
        stream_rows = _rows(stream, 10)

        quiet = {}
        if accountant is not None:
            time.sleep(2.0)
            quiet = {op: _spawn_bracket(root, env, accountant, op, work, spawn_ops) for op in ("ping", read_op)}
            accountant.close()

        deadline = time.time() + _TELEMETRY_WAIT_SECS
        rows: List[dict] = []
        while time.time() < deadline:
            rows = _telemetry_rows(root, new_pid, restart_at)
            if sum(1 for r in rows if r.get("op") == "ping") >= sum(1 for p in probes if p["method"] == "ping" and p["answered"]):
                break
            time.sleep(1.0)

        report = {
            "load": {"op": load_op, "workers": load_workers, "calls": len(load_rows),
                     "answered": sum(1 for r in load_rows if r["answered"]), "errors": sum(1 for r in load_rows if r["error"])},
            "ping_stream": {"calls": len(stream_rows), "answered": sum(1 for r in stream_rows if r["answered"])},
            "server_pid_before": first_pid,
            "server_pid_after": new_pid,
            "replacement_first_answer_s": first_answer_s,
            "replacement_never_answered": first_answer_s is None,
            "supervisor_alive": procs["supervisor"].poll() is None,
            "front_door_alive": procs["front_door"].poll() is None,
            "journal": _journal_report(trace, bucket, seeded, journal_delay_ms),
            "ops": {"ping": _op_stats(rows, "ping", probes), read_op: _op_stats(rows, read_op, probes)},
            "quiet_bracket": quiet,
            "probe_sample": probes[:2] + [p for p in probes if not p["answered"]][:2],
            "bar_ms": BAR_MS,
            "unit": "server process time (cpu_ms rows) and spawn count; diagnostic_wall_ms is never a verdict",
        }
    report["breach"] = report["replacement_never_answered"] or any(s["breach"] for s in report["ops"].values())
    return report


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--role", default="orchestrate", choices=("orchestrate", "load", "probe", "front-door"))
    p.add_argument("--method", default="ping")
    p.add_argument("--params", default="{}")
    p.add_argument("--work", default="")
    p.add_argument("--secs", type=float, default=0.0)
    p.add_argument("--count", type=int, default=0)
    p.add_argument("--interval", type=float, default=0.0)
    p.add_argument("--deadline", type=float, default=30.0)
    p.add_argument("--until-answered", action="store_true")
    p.add_argument("--boot-wait", type=float, default=60.0)
    p.add_argument("--load-workers", type=int, default=6)
    p.add_argument("--load-op", default="dispatch.emit")
    p.add_argument("--load-params", default=json.dumps(_EMIT_PARAMS))
    p.add_argument("--read-op", default="diagnostics.always_succeeds")
    p.add_argument("--source-root", default="", help="tree to clone (default: this one); a baseline export "
                   "of an older sha measures the pre-fix server, the bench copies itself into the clone")
    p.add_argument("--journal-bucket-files", type=int, default=20000)
    p.add_argument("--journal-delay-ms", type=float, default=3000.0)
    p.add_argument("--warmup-secs", type=float, default=8.0)
    p.add_argument("--pings", type=int, default=20)
    p.add_argument("--reads", type=int, default=10)
    p.add_argument("--probe-deadline", type=float, default=10.0)
    p.add_argument("--spawn-ops", type=int, default=10)
    p.add_argument("--json-out", default="")
    p.add_argument("--assert-under-bar", action="store_true")
    args = p.parse_args(argv)

    if args.role == "front-door":
        return _role_front_door()
    if args.role in ("load", "probe"):
        return _role_client(args)

    report = run_bench(
        Path(args.source_root).resolve() if args.source_root else Path(__file__).resolve().parents[2], load_workers=args.load_workers, load_op=args.load_op,
        load_params=json.loads(args.load_params), warmup_secs=args.warmup_secs, pings=args.pings,
        reads=args.reads, read_op=args.read_op, probe_deadline=args.probe_deadline, spawn_ops=args.spawn_ops,
        boot_wait=args.boot_wait, journal_files=args.journal_bucket_files, journal_delay_ms=args.journal_delay_ms,
    )
    text = json.dumps(report, indent=2)
    print(text)
    if args.json_out:
        Path(args.json_out).write_text(text, encoding="utf-8")
    return 1 if args.assert_under_bar and report["breach"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
