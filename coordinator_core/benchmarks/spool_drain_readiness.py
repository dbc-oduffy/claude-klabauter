"""Spool-to-started-run legs: where a spooled ask's drain time goes before its run starts.

Spools one ask into a repo whose drain already ran once (the shape after the door could not mint in time), then
sends the later request that re-arms `warm.server._DrainTrigger.maybe_drain`, and splits the path by the engine's
own stamps into legs (process ms, never wall clock):

- `spool_write`: `ask_spool.record.spool_ask`, in the benchmark process;
- `trigger_wait`: the engine's process CPU between the spool write and the drain being spawned;
- `drain_start`: the spawn to the drain's first mint (spawn latency plus the drain thread's claim and lookup CPU);
- `evaluate`: the drain thread's CPU inside `drain._mint` (the submit hook's emit);
- `run_started`: the drain thread's CPU from the mint returning to the fire being requested (notice, re-listing, the
  remote failover tail);
- `fire`: the drain thread's CPU inside the fire callable. The engine mode stubs the fire so no workflow child
  launches; its cost is that of the registry call's return only.

TRAP: an idle wait burns no CPU, so process time cannot see it. `trigger_wait` therefore also reports
`requests_before_drain_start` (engine requests served after the spool write before the drain spawned) and
`released_by` (the event that ended each leg); `wall_ms_diag` is a monotonic-gap diagnostic, never acceptance.
`idle_drains_started` counts drains the engine started with no request in the probe window.

`measure(repo_root)` drives the real `_DrainTrigger` and `drain.run` in this process with a stub evaluate
(the unit-test entry); `measure_engine()` boots a throwaway warm engine (a hardlink clone under a private settings
home) whose `warm/server.py` copy carries the stamps, and runs the real submit hook as the drain's evaluate.
Default CLI runs the engine mode. Figures are recorded, not enforced (`--enforce-bar` exits 2 when the total
passes the 10s readiness budget or a leg passes the 500ms bar).

Run: `python coordinator_core/benchmarks/spool_drain_readiness.py [--inproc] [--cold-boot] [--idle-probe-s S]
[--enforce-bar]`. Scratch lives in managed temp dirs this script removes.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.benchmarks import declare_benchmark_origin, fixture_settings_home
from coordinator_core.win_portability import no_console_creationflags

#: The door's hook-total readiness budget (`submit_engine_ask._DOOR_HOOK_TOTAL_S`) in process ms.
READINESS_BUDGET_MS = 10_000.0
#: DR-344: no single engine op leg passes this.
BAR_MS = 500.0
LEGS = ("spool_write", "trigger_wait", "drain_start", "evaluate", "run_started", "fire")
DEFAULT_IDLE_PROBE_S = 2.0
STAMP_ENV = "SPOOL_DRAIN_STAMP_FILE"
_BOOT_WAIT_S = 90.0
_DRAIN_WAIT_S = 60.0
_SESSION = "sess-spool-drain-readiness"
_SERVER_TAIL = 'if __name__ == "__main__":\n    sys.exit(_process_entry())\n'
_SNIPPET = (
    "if __name__ == \"__main__\":\n"
    "    from coordinator_core.benchmarks import spool_drain_readiness as _sdr\n"
    "    _sdr.install_engine_stamps(globals())\n\n\n"
)
#: What ends each leg, named in the report so a blocked leg says what released it.
_RELEASED_BY = {"spool_write": "spool_ask returned", "trigger_wait": "request", "drain_start": "mint_start",
                "evaluate": "mint_end", "run_started": "fire_start", "fire": "fire_end"}
_ONESHOT = (
    "import json,sys\nfrom coordinator_core.warm import client\n"
    "r=client.try_warm_dispatch(json.loads(sys.argv[1]),read_deadline_secs=8.0,hook_path=True)\n"
    "print(json.dumps({'ok':isinstance(r,dict) and 'result' in r}))"
)


def stub_fire(_root: Any, run_id: str) -> dict:
    """A fire that launches nothing: the handle a started run's registry call returns."""
    return {"fire_id": f"bench-{run_id}", "state": "running", "execution_started": True}


def scripting_evaluate(root: Path) -> Callable[[dict], dict]:
    """A healthy engine's submit hook: mints one run (run dir, submission record, workflow script) and allows."""
    from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT

    calls = [0]

    def evaluate(payload: dict) -> dict:
        calls[0] += 1
        run_id = f"ask-20261104T14{calls[0]:04d}-{calls[0]:06x}"
        run_dir = root / RUN_DIR_ROOT / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "submission.json").write_text(json.dumps({
            "run_id": run_id, "session_id": payload.get("session_id"), "tool_use_id": payload.get("tool_use_id"),
        }), encoding="utf-8")
        (root / RUN_DIR_ROOT / f"{run_id}.workflow.mjs").write_text("// script", encoding="utf-8")
        return {"hookSpecificOutput": {
            "permissionDecision": "allow", "updatedInput": {"scriptPath": "x"},
            "permissionDecisionReason": f"Submitted to the engine as run {run_id}.",
        }}

    return evaluate


def _patch(owner: Any, name: str, make: Callable, undo: list) -> None:
    table = isinstance(owner, dict)
    orig = owner[name] if table else getattr(owner, name)
    if table:
        owner[name] = make(orig)
        undo.append(lambda: owner.__setitem__(name, orig))
    else:
        setattr(owner, name, make(orig))
        undo.append(lambda: setattr(owner, name, orig))


def install_stamps(ns: dict, emit: Callable[[dict], None], *, evaluate: Optional[Callable] = None,
                   fire: Optional[Callable] = None) -> Callable[[], None]:
    """Wrap the drain path in `ns` (`warm.server`'s namespace) and `ask_spool.drain` so each step calls `emit(stamp)`.

    A stamp is `{event, wall, cpu, thread_cpu, thread, spawns}`: epoch seconds, process CPU seconds, this thread's CPU
    seconds, the thread id, and subprocess spawns so far. `evaluate` is injected into `drain.run` when given; `fire`
    replaces the drain's default fire. Returns the function that restores every patched name."""
    from coordinator_core.ask_spool import drain

    spawns = [0]
    undo: list = []

    def stamp(event: str) -> None:
        emit({"event": event, "wall": time.time(), "cpu": time.process_time(), "thread_cpu": time.thread_time(),
              "thread": threading.get_ident(), "spawns": spawns[0]})

    def before(event: str) -> Callable:
        def make(orig: Callable) -> Callable:
            def wrapped(*a: Any, **k: Any) -> Any:
                stamp(event)
                return orig(*a, **k)
            return wrapped
        return make

    def around(start: str, end: str) -> Callable:
        def make(orig: Callable) -> Callable:
            def wrapped(*a: Any, **k: Any) -> Any:
                stamp(start)
                try:
                    return orig(*a, **k)
                finally:
                    stamp(end)
            return wrapped
        return make

    def counting(orig: Callable) -> Callable:
        def wrapped(self: Any, *a: Any, **k: Any) -> None:
            spawns[0] += 1
            orig(self, *a, **k)
        return wrapped

    def injecting(orig: Callable) -> Callable:
        def wrapped(*a: Any, **k: Any) -> Any:
            if evaluate is not None:
                k.setdefault("evaluate", evaluate)
            return orig(*a, **k)
        return wrapped

    def firing(orig: Callable) -> Callable:
        def make(inner: Callable) -> Callable:
            def wrapped(root: Any, run_id: str) -> Any:
                stamp("fire_start")
                try:
                    return (fire or inner)(root, run_id)
                finally:
                    stamp("fire_end")
            return wrapped
        return make(orig)

    ctx = ns["_ServerContext"]
    _patch(subprocess.Popen, "__init__", counting, undo)
    _patch(ctx, "record_served_repo", before("request"), undo)
    _patch(ctx, "_boot_prewarm_run", around("boot_prewarm_start", "boot_prewarm_done"), undo)
    _patch(ns["_DrainTrigger"], "maybe_drain", before("maybe_drain"), undo)
    _patch(ns, "_spawn_background", before("drain_spawn"), undo)
    _patch(ns, "_run_ask_spool_drain", around("drain_thread_start", "drain_end"), undo)
    _patch(drain, "run", injecting, undo)
    _patch(drain, "_mint", around("mint_start", "mint_end"), undo)
    _patch(drain, "_default_fire", firing, undo)

    def restore() -> None:
        for step in reversed(undo):
            step()

    return restore


def install_engine_stamps(ns: dict) -> None:
    """The throwaway engine's entry: stamps appended as JSON lines to the file `STAMP_ENV` names, fire stubbed."""
    import os

    path = os.environ.get(STAMP_ENV)
    if not path:
        return
    lock = threading.Lock()

    def emit(rec: dict) -> None:
        with lock, open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")

    install_stamps(ns, emit, fire=stub_fire)


def _span(a: Optional[dict], b: Optional[dict], key: str) -> Optional[float]:
    return None if a is None or b is None else max(0.0, (b[key] - a[key]) * 1000.0)


def _sum(a: Optional[float], b: Optional[float]) -> Optional[float]:
    return None if a is None or b is None else a + b


def legs_from_stamps(stamps: list, *, spool_ms: float, spool_wall_ms: float, spool_wall: float, engine_cpu_ms: float,
                     base_spawns: int) -> dict:
    """The legs of one spool-to-started-run path from the stamps taken after the spool write.

    `engine_cpu_ms` is the engine process's CPU at the spool write, `spool_wall` the epoch second it finished.
    A leg whose bounding stamps are missing is None, and `started` is False unless the fire returned. The report
    carries `legs` (process ms plus `total`), `spawns`, `wall_ms_diag`, `released_by`, `requests_before_drain_start`,
    `largest_leg` (by process ms) and `largest_wait_leg` (by wall time beyond process time)."""
    def first(event: str) -> Optional[dict]:
        return next((s for s in stamps if s["event"] == event), None)

    spawn, t0, m0, m1, f0, f1 = (first(e) for e in ("drain_spawn", "drain_thread_start", "mint_start", "mint_end",
                                                    "fire_start", "fire_end"))
    requests = [s for s in stamps if s["event"] == "request" and (spawn is None or s["wall"] <= spawn["wall"])]
    cpu: dict[str, Any] = {
        "spool_write": spool_ms,
        "trigger_wait": None if spawn is None else max(0.0, spawn["cpu"] * 1000.0 - engine_cpu_ms),
        "drain_start": _sum(_span(spawn, t0, "cpu"), _span(t0, m0, "thread_cpu")),
        "evaluate": _span(m0, m1, "thread_cpu"),
        "run_started": _span(m1, f0, "thread_cpu"),
        "fire": _span(f0, f1, "thread_cpu"),
    }
    wall: dict[str, Any] = {
        "spool_write": spool_wall_ms,
        "trigger_wait": None if spawn is None else max(0.0, (spawn["wall"] - spool_wall) * 1000.0),
        "drain_start": _span(spawn, m0, "wall"), "evaluate": _span(m0, m1, "wall"),
        "run_started": _span(m1, f0, "wall"), "fire": _span(f0, f1, "wall"),
    }
    spawn_counts: dict[str, Any] = {
        "spool_write": 0,
        "trigger_wait": None if spawn is None else spawn["spawns"] - base_spawns,
        "drain_start": None if spawn is None or m0 is None else m0["spawns"] - spawn["spawns"],
        "evaluate": None if m0 is None or m1 is None else m1["spawns"] - m0["spawns"],
        "run_started": None if m1 is None or f0 is None else f0["spawns"] - m1["spawns"],
        "fire": None if f0 is None or f1 is None else f1["spawns"] - f0["spawns"],
    }
    present = {k: v for k, v in cpu.items() if v is not None}
    waits = {k: wall[k] - cpu[k] for k in LEGS if wall[k] is not None and cpu[k] is not None}
    released: dict[str, Any] = dict(_RELEASED_BY)
    released["trigger_wait"] = f"request#{len(requests)}" if spawn is not None and requests else None
    total = sum(present.values())
    return {
        "started": f1 is not None,
        "legs": {**{k: None if v is None else round(v, 1) for k, v in cpu.items()}, "total": round(total, 1)},
        "spawns": {**spawn_counts, "total": sum(v for v in spawn_counts.values() if v)},
        "wall_ms_diag": {k: None if v is None else round(v, 1) for k, v in wall.items()},
        "released_by": released,
        "requests_before_drain_start": len(requests) if spawn is not None else None,
        "largest_leg": max(present, key=lambda k: present[k]) if present else None,
        "largest_wait_leg": max(waits, key=lambda k: waits[k]) if waits else None,
        "within_budget": f1 is not None and total <= READINESS_BUDGET_MS,
        "over_bar_legs": sorted(k for k, v in present.items() if v > BAR_MS),
    }


def _wait_for(pred: Callable[[], bool], deadline_s: float) -> bool:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def _count(stamps: Callable[[], list], event: str, start: int = 0) -> int:
    return sum(1 for s in stamps()[start:] if s["event"] == event)


def _scenario(repo: Path, payload: dict, *, send_request: Callable[[], None], stamps: Callable[[], list],
              engine_cpu_ms: Callable[[], float], idle_probe_s: float, wait_s: float) -> dict:
    """Serve the repo once (an empty drain), spool one ask, idle `idle_probe_s`, send the later request, wait for the run."""
    from coordinator_core.ask_spool import record

    send_request()
    _wait_for(lambda: _count(stamps, "drain_end") >= 1, wait_s)
    mark = len(stamps())
    base_spawns = stamps()[-1]["spawns"] if mark else 0
    cpu0, wall0 = time.process_time(), time.time()
    record.spool_ask(str(repo), json.dumps(payload).encode("utf-8"), session_id=payload["session_id"],
                     tool_use_id=payload["tool_use_id"], reason="engine_unreachable")
    spool_ms, spool_wall = (time.process_time() - cpu0) * 1000.0, time.time()
    cpu_at_spool = engine_cpu_ms()
    time.sleep(idle_probe_s)
    idle_started = _count(stamps, "drain_spawn", mark)
    send_request()
    _wait_for(lambda: _count(stamps, "drain_end", mark) >= 1, wait_s)
    report = legs_from_stamps(stamps()[mark:], spool_ms=spool_ms, spool_wall_ms=(spool_wall - wall0) * 1000.0,
                              spool_wall=spool_wall, engine_cpu_ms=cpu_at_spool, base_spawns=base_spawns)
    report["idle_drains_started"] = idle_started
    report["idle_probe_s"] = idle_probe_s
    return report


def _bare_payload(repo: Path) -> dict:
    return {"tool_name": "Workflow", "session_id": _SESSION, "tool_use_id": "toolu_spool_drain_bench", "cwd": str(repo),
            "tool_input": {"name": "coordinator:engine-ask",
                           "args": {"ask": "spool drain readiness bench ask", "source": "pm"}}}


def measure(repo_root: Any, *, evaluate: Optional[Callable] = None, fire: Optional[Callable] = None,
            idle_probe_s: float = 0.2, wait_s: float = _DRAIN_WAIT_S) -> dict:
    """The legs with the real `_DrainTrigger` and `drain.run` in this process, under `repo_root`.

    `evaluate` defaults to `scripting_evaluate` (a stub mint) and `fire` to `stub_fire`. The later request is
    `_ServerContext.record_served_repo`, the call `warm.server` makes per request. The caller scopes the settings
    home (`fixture_settings_home`). Returns `legs_from_stamps`'s report with `mode: "inproc"`."""
    from coordinator_core.warm import server

    repo = Path(repo_root)
    stamps: list = []
    restore = install_stamps(vars(server), stamps.append, evaluate=evaluate or scripting_evaluate(repo),
                             fire=fire or stub_fire)
    ctx = server._ServerContext(name="spool-drain-readiness", sid="bench", version_state=None)  # type: ignore[arg-type]
    try:
        report = _scenario(repo, _bare_payload(repo), send_request=lambda: ctx.record_served_repo(repo),
                           stamps=lambda: list(stamps), engine_cpu_ms=lambda: time.process_time() * 1000.0,
                           idle_probe_s=idle_probe_s, wait_s=wait_s)
        _wait_for(lambda: not ctx._drain_trigger._running, wait_s)
    finally:
        restore()
    report["mode"] = "inproc"
    return report


def instrumented_server_source(source: str) -> str:
    """`warm/server.py`'s text with the stamp snippet inserted ahead of its `__main__` guard.

    Raises `ValueError` when the guard is not the file's tail (the snippet would not run)."""
    if not source.rstrip().endswith(_SERVER_TAIL.rstrip()):
        raise ValueError("warm/server.py no longer ends with its `__main__` guard; the stamp snippet has no seam")
    return source.rstrip()[: -len(_SERVER_TAIL.rstrip())] + _SNIPPET + _SERVER_TAIL


def read_stamps(path: Path) -> list:
    """The stamp rows the engine wrote, in order; a torn last line is dropped."""
    rows: list = []
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


@contextlib.contextmanager
def _engine(source_root: Path, stamps: Path, *, wait_prewarm: bool) -> Iterator[tuple]:
    """Yield `(clone_root, server_pid, env)` of a booted throwaway warm engine whose own stamps append to `stamps`."""
    from coordinator_core.benchmarks.isolated_clone import (
        WarmEngineBootTimeout, clone_engine, close_clone_job, engine_env, mkdtemp_for_clone, reap_processes_under,
        rmtree_or_raise, spawn_clone_server,
    )
    from coordinator_core.warm import breadcrumb

    tmp_parent = mkdtemp_for_clone(source_root, prefix="spooldrain-")
    root = tmp_parent / "clone"
    proc: Optional[subprocess.Popen] = None
    job = None
    try:
        clone_engine(source_root, root, entrypoint="hook-run")
        server = root / "coordinator_core" / "warm" / "server.py"
        text = server.read_text(encoding="utf-8")
        server.unlink()
        server.write_text(instrumented_server_source(text), encoding="utf-8", newline="\n")
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
        if wait_prewarm:
            _wait_for(lambda: any(r["event"] == "boot_prewarm_done" for r in read_stamps(stamps)), deadline - time.time())
        yield root, int(crumb["pid"]), env
    finally:
        crumb = breadcrumb.read_breadcrumb(engine_root=root)
        if crumb and crumb.get("pid"):
            try:
                import psutil

                server_proc = psutil.Process(int(crumb["pid"]))
                workers = server_proc.children(recursive=True)
                for w in workers:
                    w.terminate()
                server_proc.terminate()
                psutil.wait_procs([server_proc, *workers], timeout=5)
            except Exception:  # noqa: BLE001 -- teardown is best effort; the reaper and the leak raise follow
                pass
        elif proc is not None and proc.poll() is None:
            proc.terminate()
        close_clone_job(job)
        reaped = reap_processes_under(tmp_parent)
        import shutil

        shutil.rmtree(breadcrumb.svc_dir(engine_root=root), ignore_errors=True)
        rmtree_or_raise(tmp_parent, label="spool-drain-engine", reaped=reaped)


def _client_request(env: dict, root: Path, repo: Path) -> bool:
    """One hook request for `repo` through the warm client, from a fresh process rooted at the clone."""
    msg = {"jsonrpc": "2.0", "id": 1, "method": "hooks.preuse_bash_dispatch", "_origin_worktree": str(repo),
           "params": {"hook_event_name": "PreToolUse", "session_id": _SESSION, "cwd": str(repo), "tool_name": "Bash",
                      "tool_input": {"command": "git status"}}}
    from coordinator_core.warm.client import CONFIGURED_ROOT_ENV

    done = subprocess.run([sys.executable, "-c", _ONESHOT, json.dumps(msg)], cwd=str(root),
                          env={**env, CONFIGURED_ROOT_ENV: str(root)},
                          capture_output=True, text=True, timeout=120, **no_console_creationflags())
    return '"ok": true' in done.stdout


def measure_engine(*, cold_boot: bool = False, idle_probe_s: float = DEFAULT_IDLE_PROBE_S) -> dict:
    """Boot the throwaway engine and measure one spooled ask over the real submit hook.

    `cold_boot` sends the requests while the boot prewarm is still running (the guard-pool-prewarm suspect); the
    default waits for it. The fixture repo carries one sizing; the spooled payload is its `{sizing}` start. Returns
    `legs_from_stamps`'s report plus `mode`, `cold_boot` and `request_ok` (every client request answered)."""
    from coordinator_core.benchmarks.isolated_clone import rmtree_or_raise
    from coordinator_core.benchmarks.submit_engine_ask_process_budget import (
        _git, _payload, build_fixture, start_sizing_rel,
    )
    from coordinator_core.temp_layout import make_managed_temp_dir

    source_root = Path(__file__).resolve().parents[2]
    scratch = make_managed_temp_dir(prefix="spool-drain-")
    try:
        repo = scratch / "repo"
        repo.mkdir()
        build_fixture(repo, starts=1)
        _git(repo, "config", "core.longpaths", "true")
        stamps_file = scratch / "stamps.jsonl"
        payload = _payload(repo, {"sizing": start_sizing_rel(0)})
        payload["tool_use_id"] = "toolu_spool_drain_bench"
        answered: list = []
        with fixture_settings_home(scratch):
            with _engine(source_root, stamps_file, wait_prewarm=not cold_boot) as (root, pid, env):
                import psutil

                server = psutil.Process(pid)

                def cpu_ms() -> float:
                    times = server.cpu_times()
                    return (times.user + times.system) * 1000.0

                report = _scenario(
                    repo, payload, send_request=lambda: answered.append(_client_request(env, root, repo)),
                    stamps=lambda: read_stamps(stamps_file), engine_cpu_ms=cpu_ms,
                    idle_probe_s=idle_probe_s, wait_s=_DRAIN_WAIT_S)
    finally:
        rmtree_or_raise(scratch, label="spool-drain")
    report.update(mode="engine", cold_boot=cold_boot, request_ok=all(answered))
    return report


def render(report: dict) -> list:
    """The report's printable lines: one per leg, the total, `largest_leg`, and the JSON of the whole report."""
    lines = []
    for leg in LEGS:
        lines.append(f"leg {leg}: process_ms={report['legs'][leg]} spawns={report['spawns'][leg]} "
                     f"wall_ms_diag={report['wall_ms_diag'][leg]} released_by={report['released_by'][leg]}")
    lines.append(f"total: process_ms={report['legs']['total']} spawns={report['spawns']['total']} "
                 f"budget_ms={READINESS_BUDGET_MS:.0f} within_budget={report['within_budget']} started={report['started']}")
    lines.append(f"trigger_wait: requests_before_drain_start={report['requests_before_drain_start']} "
                 f"idle_drains_started={report['idle_drains_started']} (idle probe {report['idle_probe_s']}s)")
    lines.append(f"largest_leg={report['largest_leg']} largest_wait_leg={report['largest_wait_leg']}")
    lines.append(json.dumps(report))
    return lines


def main() -> int:
    declare_benchmark_origin()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--inproc", action="store_true", help="the real trigger and drain in this process with stub evaluate and fire")
    ap.add_argument("--cold-boot", action="store_true", help="send the requests while the engine's boot prewarm still runs")
    ap.add_argument("--idle-probe-s", type=float, default=DEFAULT_IDLE_PROBE_S,
                    help="seconds idle after the spool write to count drains that start with no request")
    ap.add_argument("--enforce-bar", action="store_true",
                    help=f"exit 2 when the run did not start, the total passes {READINESS_BUDGET_MS / 1000:.0f}s or a leg passes {BAR_MS:.0f}ms")
    args = ap.parse_args()
    if args.inproc:
        from coordinator_core.benchmarks.isolated_clone import rmtree_or_raise
        from coordinator_core.temp_layout import make_managed_temp_dir

        scratch = make_managed_temp_dir(prefix="spool-drain-inproc-")
        try:
            (scratch / "repo").mkdir()
            with fixture_settings_home(scratch):
                report = measure(scratch / "repo", idle_probe_s=args.idle_probe_s)
        finally:
            rmtree_or_raise(scratch, label="spool-drain-inproc")
    else:
        report = measure_engine(cold_boot=args.cold_boot, idle_probe_s=args.idle_probe_s)
    print("\n".join(render(report)))
    bad = not report["within_budget"] or bool(report["over_bar_legs"])
    return 2 if args.enforce_bar and bad else 0


if __name__ == "__main__":
    sys.exit(main())
