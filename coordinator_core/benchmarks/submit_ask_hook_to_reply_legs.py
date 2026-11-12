"""Submit-path hook-to-reply legs: where the reply time goes under concurrent run starts.

Boots a throwaway warm engine (a hardlink clone under a private settings home, built from
`isolated_clone`'s own clone, spawn and teardown steps, so nothing lands in the live engine's store or pipe),
fires `--concurrent-runs N` sizing run starts and one `{sizing}` refire through the installed `hook-run` door
in hook mode at once, and splits each submit's reply time into legs from the engine's own stamps:

- `connect_ms`: door spawn to the engine's accept return (door start, its pre-connect work, the pipe connect);
- `accept_to_enqueue_ms`: the accept return to the enqueue stamp;
- `queue_wait_ms`: accept-chain enqueue to a worker taking the connection (`_serve_queued`'s stamp);
- `deliver_ms`: a worker holding the connection until the op dispatches (the door's request write and the parse);
- `admission_wait_ms`: the pool admission gate (`_AdmissionGate.acquire`'s own return);
- `pool_dispatch_ms`: pool submit, the op in a pool worker, and the result coming back;
- `reply_ms`: the engine finishing to the door's first reply byte (`first_byte_ms` is spawn to that byte).

Each recorded round runs under `--load-workers` (12) synthetic CPU/IO processes, started for the round and killed after
it, standing in for the `-n 12` regate. `--against-installed` probes the installed door and the live engine with a
pass-through hook payload under the same load and prints the live `door-fail-open.log` lines written in the window
(each names `leg=`); it mints nothing.

Engine-side legs are wall waits. `evaluate_process_ms` is the hook's in-process CPU from
`submit_engine_ask_process_budget.measure_concurrent` under the same N, shown beside every row, never a wall
figure; `door_process_ms` is the door child's own CPU (Windows only). `spool` is True when the door answered
the spool envelope. `largest_leg` names the leg holding the time.

The stamps come from instrumentation appended to the throwaway clone's own `warm/server.py` (a copy, never the
hardlink: the live source is untouched) that wraps `_ServerContext` and `_AdmissionGate` in the engine process.
The figures are recorded, not enforced (`--enforce-bar` exits 2 when a refire spooled or any leg passes 500ms).

This mode measures and locates; it applies no fix to the leg it names.

Windows is first-class; the POSIX door shape is the same but unmeasured here.

Run: `python -m coordinator_core.benchmarks.submit_ask_hook_to_reply_legs [--concurrent-runs N] [--runs N]
[--warmups N] [--settle-secs S] [--door PATH] [--load-workers N] [--against-installed] [--enforce-bar]`. Scratch lives in managed temp dirs this script removes.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterator, Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.benchmarks import declare_benchmark_origin, fixture_settings_home
from coordinator_core.benchmarks.isolated_clone import (
    WarmEngineBootTimeout,
    _link_tree,
    clone_engine,
    close_clone_job,
    engine_env,
    mkdtemp_for_clone,
    reap_processes_under,
    rmtree_or_raise,
    spawn_clone_server,
)
from coordinator_core.benchmarks.submit_engine_ask_process_budget import (
    SIZING_REL,
    SPOOL_MARKER,
    _payload,
    _reset,
    build_fixture,
    measure_concurrent,
    start_sizing_rel,
)
from coordinator_core.win_portability import no_console_creationflags

#: The brightline bar for the reply reaching the hook, ms.
BAR_MS = 500.0
DEFAULT_CONCURRENT_RUNS = 3
DEFAULT_RUNS = 3
DEFAULT_WARMUPS = 1
#: The hook op, and the entrypoint whose installed door image the PreToolUse wiring runs.
OP = "hooks.submit_engine_ask"
ENTRYPOINT = "hook-run"
STAMP_ENV = "LEGS_STAMP_FILE"
#: Backstop above the door's own deadline so a wedged door cannot hang the benchmark.
_WATCHDOG_S = 60.0
#: Boot ends when the engine's pools are warm: the first burst measured is the first one a warm engine serves.
_BOOT_WAIT_S = 90.0
_LOAD_RAMP_S = 0.5
#: Legs a row names the largest of.
LEGS = ("connect_ms", "deliver_ms", "accept_to_enqueue_ms", "queue_wait_ms", "admission_wait_ms",
        "pool_dispatch_ms", "reply_ms")
#: Synthetic load standing in for the `-n 12` regate: CPU spin plus file writes in a scratch dir, bounded to the window.
DEFAULT_LOAD_WORKERS = 12
_LOAD_SRC = (
    "import hashlib,os,sys,time\n"
    "d,stop=sys.argv[1],sys.argv[2]\n"
    "p=os.path.join(d,'load-%d.bin'%os.getpid())\n"
    "buf=os.urandom(1<<20)\n"
    "while not os.path.exists(stop):\n"
    "    end=time.perf_counter()+0.05\n"
    "    while time.perf_counter()<end: hashlib.sha256(buf).digest()\n"
    "    with open(p,'wb') as f: f.write(buf)\n"
    "    with open(p,'rb') as f: f.read()\n"
)
_SERVER_TAIL = 'if __name__ == "__main__":\n    sys.exit(_process_entry())\n'

_INSTRUMENT = '''\
import json as _j
import os as _os
import re as _re
import threading as _th
import time as _t

_LEGS_OUT = _os.environ.get("LEGS_STAMP_FILE")
_LEGS_TAG = _re.compile(r"legs-(?:start-\\d+|refire)")
_legs_local = _th.local()
_legs_lock = _th.Lock()


def _legs_emit(rec):
    if not _LEGS_OUT:
        return
    with _legs_lock, open(_LEGS_OUT, "a", encoding="utf-8") as fh:
        fh.write(_j.dumps(rec) + "\\n")


_legs_pool_dispatch = _ServerContext._pool_dispatch
_legs_record_served_row = _ServerContext._record_served_row
_legs_acquire = _AdmissionGate.acquire
_legs_serve_queued = _ServerContext._serve_queued
_legs_enqueue = _ServerContext._enqueue_connection
_legs_connect_pipe = globals().get("_connect_pipe")
_legs_boot_prewarm = _ServerContext._boot_prewarm_run
_legs_accepts = {}


def _legs_connect_pipe_wrapped(handle):
    try:
        return _legs_connect_pipe(handle)
    finally:
        _legs_local.accepted = _t.time()


def _legs_enqueue_wrapped(self, io, **kw):
    _legs_accepts[id(io)] = getattr(_legs_local, "accepted", None)
    return _legs_enqueue(self, io, **kw)


def _legs_serve_queued_wrapped(self, io, *a, **k):
    _legs_local.accept_ts = _legs_accepts.pop(id(io), None)
    return _legs_serve_queued(self, io, *a, **k)


def _legs_pool_dispatch_wrapped(self, msg, **kw):
    found = _LEGS_TAG.search(_j.dumps(msg, default=str))
    _legs_local.tag = found.group(0) if found else None
    wall0 = _t.time()
    t0 = _t.perf_counter()
    resp = None
    try:
        resp = _legs_pool_dispatch(self, msg, **kw)
        return resp
    finally:
        err = resp.get("error") if isinstance(resp, dict) else "no-response"
        _legs_emit({"kind": "pool_dispatch", "tag": _legs_local.tag, "ms": (_t.perf_counter() - t0) * 1000.0,
                    "start_ts": wall0, "error": None if err is None else _j.dumps(err, default=str)[:300]})


def _legs_acquire_wrapped(self, *a, **k):
    waited = _legs_acquire(self, *a, **k)
    _legs_emit({"kind": "admission", "tag": getattr(_legs_local, "tag", None),
                "wait_ms": None if waited is None else waited * 1000.0})
    return waited


def _legs_record_served_row_wrapped(self, row, lane, enqueue_ts, start_ts, busy_at_enqueue):
    _legs_emit({"kind": "served", "tag": getattr(_legs_local, "tag", None), "op": row.get("op"), "lane": lane,
                "accept_ts": getattr(_legs_local, "accept_ts", None),
                "enqueue_ts": enqueue_ts, "start_ts": start_ts, "end_ts": _t.time(), "busy": busy_at_enqueue})
    _legs_local.tag = None
    _legs_local.accept_ts = None
    return _legs_record_served_row(self, row, lane, enqueue_ts, start_ts, busy_at_enqueue)


def _legs_boot_prewarm_wrapped(self):
    try:
        return _legs_boot_prewarm(self)
    finally:
        _legs_emit({"kind": "boot_prewarm_done"})


_ServerContext._boot_prewarm_run = _legs_boot_prewarm_wrapped
_ServerContext._pool_dispatch = _legs_pool_dispatch_wrapped
_ServerContext._record_served_row = _legs_record_served_row_wrapped
_ServerContext._serve_queued = _legs_serve_queued_wrapped
_ServerContext._enqueue_connection = _legs_enqueue_wrapped
_AdmissionGate.acquire = _legs_acquire_wrapped
if _legs_connect_pipe is not None:
    _connect_pipe = _legs_connect_pipe_wrapped

'''


def installed_door() -> Optional[Path]:
    """The installed `hook-run` door (the image the PreToolUse hook wiring runs) under the settings home, or None."""
    home = os.environ.get("COORDINATOR_SETTINGS_HOME") or os.path.join(
        os.environ.get("CLAUDE_HOME") or os.path.expanduser("~"), ".coordinator-claude-settings")
    for name in (f"{ENTRYPOINT}.exe", ENTRYPOINT):
        door = Path(home) / "bin" / name
        if door.is_file():
            return door
    return None


def instrumented_server_source(source: str) -> str:
    """`warm/server.py`'s text with the stamp instrumentation inserted ahead of its `__main__` guard.

    Raises `ValueError` when the guard is not the file's tail (the instrumentation would not run)."""
    if not source.rstrip().endswith(_SERVER_TAIL.rstrip()):
        raise ValueError("warm/server.py no longer ends with its `__main__` guard; the stamp instrumentation has no seam")
    head = source.rstrip()[: -len(_SERVER_TAIL.rstrip())]
    return head + _INSTRUMENT + _SERVER_TAIL


def _instrument_clone(root: Path) -> None:
    """Replace the clone's hardlinked `warm/server.py` with an instrumented copy; the live source is untouched."""
    server = root / "coordinator_core" / "warm" / "server.py"
    text = server.read_text(encoding="utf-8")
    server.unlink()
    server.write_text(instrumented_server_source(text), encoding="utf-8", newline="\n")


@contextlib.contextmanager
def instrumented_engine(source_root: Path, stamps: Path) -> Iterator[dict]:
    """Yield the env of a booted throwaway warm engine whose own stamps append to `stamps`.

    `isolated_clone.booted_warm_engine`'s steps with the two changes it cannot take: the clone's server is
    instrumented, and `COORDINATOR_ENGINE_ROOT` names the clone (a clone under this repo's scratch resolves, by
    self-location, to the repo root, which the entrypoint's engine-root ladder refuses)."""
    from coordinator_core.warm import breadcrumb

    tmp_parent = mkdtemp_for_clone(source_root, prefix="legs-")
    root = tmp_parent / "clone"
    proc: Optional[subprocess.Popen] = None
    job = None
    try:
        clone_engine(source_root, root, entrypoint=ENTRYPOINT)
        _link_tree(source_root / "coordinator" / "lib" / "resolve-claude-klabauter", root / "coordinator" / "lib" / "resolve-claude-klabauter",
                   skip_dirs=("__pycache__", "tests"))
        _instrument_clone(root)
        env = {**engine_env(root), "COORDINATOR_ENGINE_ROOT": str(root), STAMP_ENV: str(stamps)}
        proc, job = spawn_clone_server(root, env)
        deadline = time.time() + _BOOT_WAIT_S
        crumb = None
        while time.time() < deadline:
            crumb = breadcrumb.read_breadcrumb(engine_root=root)
            if crumb and crumb.get("pid"):
                break
            time.sleep(0.2)
        if not (crumb and crumb.get("pid")):
            raise WarmEngineBootTimeout(f"throwaway engine did not boot within {_BOOT_WAIT_S}s")
        while time.time() < deadline and not any(r.get("kind") == "boot_prewarm_done" for r in read_stamps(stamps)):
            time.sleep(0.2)
        yield env
    finally:
        crumb = breadcrumb.read_breadcrumb(engine_root=root)
        if crumb and crumb.get("pid"):
            try:
                import psutil

                server = psutil.Process(int(crumb["pid"]))
                workers = server.children(recursive=True)
                for w in workers:
                    w.terminate()
                server.terminate()
                psutil.wait_procs([server, *workers], timeout=5)
            except Exception:  # noqa: BLE001 -- teardown is best effort; the reaper and the leak raise follow
                pass
        elif proc is not None and proc.poll() is None:
            proc.terminate()
        close_clone_job(job)
        reaped = reap_processes_under(tmp_parent)
        shutil.rmtree(breadcrumb.svc_dir(engine_root=root), ignore_errors=True)
        rmtree_or_raise(tmp_parent, label="submit-legs-engine", reaped=reaped)


def read_stamps(path: Path) -> list[dict]:
    """The stamp rows the engine wrote, in order; a torn last line is dropped."""
    rows: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return rows
    for line in text.splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _span(later: Optional[float], earlier: Optional[float]) -> Optional[float]:
    return None if later is None or earlier is None else max(0.0, (later - earlier) * 1000.0)


def split_legs(door_total_ms: float, stamps: list[dict], tag: str,
               spawn_wall: Optional[float] = None, first_byte_wall: Optional[float] = None) -> dict:
    """The legs of the submit tagged `tag`, from the engine's stamp rows and the door's own wall stamps.

    `spawn_wall` is the epoch second the door child was spawned, `first_byte_wall` the epoch second its first reply
    byte arrived. Legs: `connect_ms` (door spawn to the engine's accept return), `accept_to_enqueue_ms`,
    `queue_wait_ms`, `deliver_ms` (a worker holding the connection until the op dispatches: the door's request
    write and the parse), `admission_wait_ms`, `pool_dispatch_ms` (the op in the pool worker after the gate),
    `reply_ms` (the engine finishing to the door's first byte). A leg whose stamps are missing is None, and
    a request the engine never stamped has every engine leg None. `largest_leg` is the largest present leg."""
    served = next((r for r in stamps if r.get("kind") == "served" and r.get("tag") == tag), None)
    admission = next((r for r in stamps if r.get("kind") == "admission" and r.get("tag") == tag), None)
    dispatched = next((r for r in stamps if r.get("kind") == "pool_dispatch" and r.get("tag") == tag), None)
    served = served or {}
    admission_wait = admission.get("wait_ms") if admission else None
    pool_dispatch = None
    if dispatched is not None:
        pool_dispatch = max(0.0, dispatched["ms"] - (admission_wait or 0.0))
    engine_span = _span(served.get("end_ts"), served.get("enqueue_ts"))
    legs = {
        "connect_ms": _span(served.get("accept_ts"), spawn_wall),
        "deliver_ms": _span(dispatched.get("start_ts") if dispatched else None, served.get("start_ts")),
        "accept_to_enqueue_ms": _span(served.get("enqueue_ts"), served.get("accept_ts")),
        "queue_wait_ms": _span(served.get("start_ts"), served.get("enqueue_ts")),
        "admission_wait_ms": admission_wait,
        "pool_dispatch_ms": pool_dispatch,
        "reply_ms": _span(first_byte_wall, served.get("end_ts")),
    }
    present = {k: v for k, v in legs.items() if v is not None}
    return {
        **{k: None if v is None else round(v, 1) for k, v in legs.items()},
        "door_total_ms": round(door_total_ms, 1),
        "first_byte_ms": _span(first_byte_wall, spawn_wall) if spawn_wall is not None else None,
        "engine_span_ms": None if engine_span is None else round(engine_span, 1),
        "lane": served.get("lane"),
        "largest_leg": max(present, key=present.get) if present else None,
    }


@contextlib.contextmanager
def synthetic_load(workers: int, scratch: Path) -> Iterator[None]:
    """`workers` CPU-plus-file-write processes standing in for the `-n 12` regate, killed on exit.

    Stdlib-only children (no benchmark re-import); the stop file is a courtesy, the kill is the guarantee."""
    stop = scratch / "load.stop"
    procs: list[subprocess.Popen] = []
    try:
        for _ in range(workers):
            procs.append(subprocess.Popen(
                [sys.executable, "-I", "-c", _LOAD_SRC, str(scratch), str(stop)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                **no_console_creationflags()))
        yield
    finally:
        stop.write_text("stop", encoding="utf-8", newline="\n")
        for proc in procs:
            proc.kill()
        for proc in procs:
            proc.wait()


def _door_cpu_ms(proc: subprocess.Popen) -> Optional[float]:
    """The door child's own user plus kernel CPU ms on Windows (GetProcessTimes), else None."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        times = [wintypes.FILETIME() for _ in range(4)]
        if not ctypes.windll.kernel32.GetProcessTimes(wintypes.HANDLE(int(proc._handle)), *map(ctypes.byref, times)):  # type: ignore[attr-defined]
            return None
        ticks = sum((t.dwHighDateTime << 32) | t.dwLowDateTime for t in times[2:])
        return round(ticks / 10_000.0, 1)
    except Exception:  # noqa: BLE001 -- an unreadable CPU figure is omitted, never fatal
        return None


def _door_call(door: Path, env: dict, repo: Path, payload: dict) -> dict:
    """One hook-mode submit through the door: total wall ms, door CPU ms, stdout."""
    env = {**env, "COORDINATOR_DOOR_STDIN_MODE": "hook"}
    spawn_wall = time.time()
    started = time.monotonic()
    proc = subprocess.Popen(
        [str(door), OP], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=env, cwd=str(repo), **no_console_creationflags(),
    )
    watchdog = threading.Timer(_WATCHDOG_S, proc.kill)
    watchdog.start()
    try:
        proc.stdin.write(json.dumps(payload).encode("utf-8"))
        proc.stdin.close()
        first = proc.stdout.read(1)
        first_byte_wall = time.time()
        out = first + proc.stdout.read()
        proc.wait()
    finally:
        watchdog.cancel()
    return {
        "door_total_ms": (time.monotonic() - started) * 1000.0,
        "door_process_ms": _door_cpu_ms(proc),
        "spawn_wall": spawn_wall,
        "first_byte_wall": first_byte_wall,
        "stdout": out.decode("utf-8", "replace"),
    }


def _tagged(repo: Path, rel: str, tag: str) -> dict:
    payload = _payload(repo, {"sizing": rel})
    payload["tool_use_id"] = tag
    return payload


def _fire_round(door: Path, env: dict, stamps: Path, repo: Path, concurrent_runs: int) -> list[dict]:
    """The N run starts and the refire, released together; one result row per submit."""
    jobs = [(f"legs-start-{i}", start_sizing_rel(i), "start") for i in range(concurrent_runs)]
    jobs.append(("legs-refire", SIZING_REL, "refire"))
    barrier = threading.Barrier(len(jobs))
    results: dict[str, dict] = {}
    before = len(read_stamps(stamps))

    def run(tag: str, rel: str) -> None:
        payload = _tagged(repo, rel, tag)
        barrier.wait()
        results[tag] = _door_call(door, env, repo, payload)

    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        list(pool.map(lambda job: run(job[0], job[1]), jobs))
    new = read_stamps(stamps)[before:]
    return [{
        "tag": tag, "role": role,
        **split_legs(results[tag]["door_total_ms"], new, tag, results[tag]["spawn_wall"], results[tag]["first_byte_wall"]),
        "door_process_ms": results[tag]["door_process_ms"], "spool": SPOOL_MARKER in results[tag]["stdout"],
        "reply": results[tag]["stdout"][:300],
    } for tag, _rel, role in jobs]


def _load_window(load_workers: int, scratch: Path) -> contextlib.AbstractContextManager:
    """The synthetic load for exactly one measured round: started, given `_LOAD_RAMP_S` to saturate, killed after."""
    if load_workers <= 0:
        return contextlib.nullcontext()
    stack = contextlib.ExitStack()
    stack.enter_context(synthetic_load(load_workers, scratch))
    time.sleep(_LOAD_RAMP_S)
    return stack


def measure(concurrent_runs: int = DEFAULT_CONCURRENT_RUNS, runs: int = DEFAULT_RUNS,
            warmups: int = DEFAULT_WARMUPS, door: Optional[Path] = None,
            settle_secs: float = 0.0, load_workers: int = DEFAULT_LOAD_WORKERS) -> dict:
    """Boot the throwaway engine, warm it, then record `runs` rounds of `concurrent_runs` run starts plus a refire.

    Each recorded round runs under `load_workers` synthetic CPU/IO workers (0 for none), started for that round and
    killed after it. Returns `{rounds: [[row, ...], ...], evaluate_process_ms, refire_worst_ms, largest_leg,
    spooled, door, bar_ms, load_workers}`; each row carries every `LEGS` figure, `first_byte_ms`,
    `evaluate_process_ms`, `spool` and `largest_leg`. The report's `largest_leg` is the leg with the greatest
    refire figure over the recorded rounds."""
    door = door or installed_door()
    if door is None:
        raise RuntimeError(f"no installed {ENTRYPOINT} door under the settings home")
    from coordinator_core.temp_layout import managed_tempdir

    source_root = Path(__file__).resolve().parents[2]
    scratch = Path(tempfile.mkdtemp(prefix="submit-legs-", dir=str(managed_tempdir())))
    rounds: list[list[dict]] = []
    try:
        repo = scratch / "repo"
        repo.mkdir()
        loaddir = scratch / "load"
        loaddir.mkdir()
        build_fixture(repo, starts=concurrent_runs)
        stamps = scratch / "stamps.jsonl"
        with fixture_settings_home(scratch):
            evaluate = measure_concurrent(repo, concurrent_runs, runs=1)["max_process_ms"]
            with instrumented_engine(source_root, stamps) as env:
                time.sleep(settle_secs)
                for i in range(warmups + runs):
                    _reset(repo)
                    with _load_window(load_workers if i >= warmups else 0, loaddir):
                        rows = _fire_round(door, env, stamps, repo, concurrent_runs)
                    if i >= warmups:
                        for row in rows:
                            row["evaluate_process_ms"] = evaluate
                        rounds.append(rows)
    finally:
        rmtree_or_raise(scratch, label="submit-legs")
    refires = [row for rows in rounds for row in rows if row["role"] == "refire"]
    worst = {leg: max((r[leg] or 0.0) for r in refires) for leg in LEGS} if refires else {}
    return {
        "bar_ms": BAR_MS, "concurrent_runs": concurrent_runs, "door": str(door), "load_workers": load_workers,
        "evaluate_process_ms": evaluate, "rounds": rounds,
        "refire_worst_ms": worst,
        "largest_leg": max(worst, key=worst.get) if worst else None,
        "spooled": any(r["spool"] for r in refires),
    }


def _fail_open_log() -> Path:
    home = os.environ.get("COORDINATOR_SETTINGS_HOME") or os.path.join(
        os.environ.get("CLAUDE_HOME") or os.path.expanduser("~"), ".coordinator-claude-settings")
    return Path(home) / "state" / "door-fail-open.log"


def measure_installed(runs: int = DEFAULT_RUNS, door: Optional[Path] = None,
                      load_workers: int = DEFAULT_LOAD_WORKERS) -> dict:
    """The installed door and the live warm engine, one pass-through hook probe per run under `load_workers`.

    The probe is a non-engine-ask PreToolUse payload, so the live engine mints nothing; it exercises the door's
    connect, delivery and first-byte legs only (the live engine carries no stamps). Returns
    `{door, load_workers, rows: [{door_total_ms, first_byte_ms, door_process_ms, fail_open}], fail_open_lines}`;
    `fail_open_lines` are the live `door-fail-open.log` lines appended during the window, each carrying
    `leg=` and `leg_ms=` once the installed door is a build of this tree."""
    door = door or installed_door()
    if door is None:
        raise RuntimeError(f"no installed {ENTRYPOINT} door under the settings home")
    from coordinator_core.temp_layout import managed_tempdir

    log = _fail_open_log()
    before = log.stat().st_size if log.exists() else 0
    scratch = Path(tempfile.mkdtemp(prefix="submit-legs-inst-", dir=str(managed_tempdir())))
    rows: list[dict] = []
    try:
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "session_id": "legs-installed-probe",
                   "cwd": str(scratch), "tool_use_id": "legs-installed-probe", "tool_input": {"command": "true"}}
        for _ in range(runs):
            with _load_window(load_workers, scratch):
                call = _door_call(door, dict(os.environ), scratch, payload)
            rows.append({"door_total_ms": round(call["door_total_ms"], 1),
                         "first_byte_ms": round((call["first_byte_wall"] - call["spawn_wall"]) * 1000.0, 1),
                         "door_process_ms": call["door_process_ms"],
                         "fail_open": "not evaluated" in call["stdout"] or SPOOL_MARKER in call["stdout"]})
    finally:
        rmtree_or_raise(scratch, label="submit-legs-installed")
    lines: list[str] = []
    if log.exists():
        with open(log, "rb") as fh:
            fh.seek(before)
            lines = fh.read().decode("utf-8", "replace").splitlines()
    return {"door": str(door), "load_workers": load_workers, "rows": rows, "fail_open_lines": lines}


def main() -> int:
    declare_benchmark_origin()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--concurrent-runs", type=int, default=DEFAULT_CONCURRENT_RUNS)
    ap.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    ap.add_argument("--warmups", type=int, default=DEFAULT_WARMUPS)
    ap.add_argument("--door", type=Path, default=None)
    ap.add_argument("--settle-secs", type=float, default=0.0, help="wait after engine boot before the first round")
    ap.add_argument("--enforce-bar", action="store_true", help=f"exit 2 when a refire spooled or any refire leg passes {BAR_MS:.0f}ms")
    ap.add_argument("--load-workers", type=int, default=DEFAULT_LOAD_WORKERS,
                    help="synthetic CPU/IO processes per measured round (0 for none)")
    ap.add_argument("--against-installed", action="store_true",
                    help="probe the installed door and the live engine (door-side legs and the fail-open log only)")
    args = ap.parse_args()
    if args.against_installed:
        installed = measure_installed(args.runs, args.door, args.load_workers)
        for row in installed["rows"]:
            print("installed: " + " ".join(f"{k}={v}" for k, v in row.items()))
        for line in installed["fail_open_lines"]:
            print("fail-open: " + line)
        print(json.dumps({k: v for k, v in installed.items() if k not in ("rows", "fail_open_lines")}))
        over = any(r["fail_open"] or r["first_byte_ms"] > BAR_MS for r in installed["rows"])
        return 2 if args.enforce_bar and over else 0
    report = measure(args.concurrent_runs, args.runs, args.warmups, args.door, args.settle_secs, args.load_workers)
    for n, rows in enumerate(report["rounds"]):
        for row in rows:
            print(f"round {n} {row['tag']}: " + " ".join(
                f"{k}={row[k]}" for k in (*LEGS, "first_byte_ms", "evaluate_process_ms", "door_process_ms", "spool", "largest_leg")))
    print(json.dumps({k: v for k, v in report.items() if k != "rounds"}))
    over = report["spooled"] or any(v > BAR_MS for v in report["refire_worst_ms"].values())
    return 2 if args.enforce_bar and over else 0


if __name__ == "__main__":
    sys.exit(main())
