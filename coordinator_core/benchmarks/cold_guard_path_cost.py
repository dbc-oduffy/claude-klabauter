"""Cold guard path cost: process time and spawn count of ONE hook call through the installed door against a
dead engine.

The door in hook mode falls through to a cold guard child when no engine answers, and bounds it to the 500ms
hook deadline (`door.c :: hook_fall_through`). This instrument bills that whole leg: the door plus the cold child
it waits on, as process time (user+kernel CPU across the job-object tree) and spawn count, never wall clock.
`door_dead_engine_cold_leg.py` bills a non-hook op and reports no spawn count; this one is the hook leg.

The engine is "dead" by construction: a stamped hardlink clone of the engine under `scratch/` with no server,
a private runtime base and a private settings home (nothing lands in the live fail-open log). The door's
respawn-on-miss is suppressed by occupying `<runtime>/coordinator` with a file, so no detached server is born
inside the measured job and billed as door cost.

Billing. The door needs its payload on stdin, which `process_time.batched_process_time_ms` does not feed, so the
measured command is a small wrapper that pipes the payload into the door. The wrapper's own interpreter is
measured with a no-op run of the same wrapper and subtracted: `process_ms` and `spawns` are net of it.

The deadline kills the cold child at 500ms wall, so the door figure is truncated at the deadline when the guard
needs longer. `decomposition.cold_entry_ms` is the same cold child run directly with no deadline: the guard's
full cost. `COORDINATOR_ENGINE_ROOT` pins the cold child to the clone: without it the entry hands the call to the
published engine's live server (`engine split`) and bills a warm answer as a cold guard. `decomposition` also splits it into interpreter start, import, and `evaluate_cold`.

Report keys (stable; dependants read them): `process_ms`, `spawns`, `verdict_real`, `under_bar`, `door_call`,
`decomposition`, `spawn_tally`, `fail_open_log`.

Windows only: the job-object tree and the door.c fall-through are the Windows door's. Run:
`python coordinator_core/benchmarks/cold_guard_path_cost.py [--k N] [--samples N] [--door PATH] [--enforce-bar]`.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.benchmarks import declare_benchmark_origin
from coordinator_core.benchmarks.door_hook_read_latency import (
    BENIGN_COMMAND,
    ENTRYPOINT,
    FAIL_OPEN_MARKERS,
    hook_payload,
    installed_door,
    is_fail_open,
)
from coordinator_core.win_portability import no_console_creationflags

#: The brightline process-time bar, ms (DR-344); also the door's hook read deadline.
BAR_MS = 500.0
DEFAULT_K = 3
DEFAULT_SAMPLES = 3
#: The door's loud-pass envelope names itself; `door_hook_read_latency.FAIL_OPEN_MARKERS` covers the others.
DOOR_PREFIX_MARKER = "coordinator-door:"
RUNTIME_BLOCKER = "coordinator"

#: The measured command. `door` pipes the payload into the door and stores its stdout; `noop` is the floor.
WRAPPER_SOURCE = (
    "import subprocess, sys\n"
    "mode, out_path, payload_path = sys.argv[1:4]\n"
    "if mode == 'noop':\n"
    "    raise SystemExit(0)\n"
    "flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)\n"
    "done = subprocess.run(sys.argv[4:], input=open(payload_path, 'rb').read(), stdout=subprocess.PIPE,\n"
    "                      stderr=subprocess.DEVNULL, creationflags=flags)\n"
    "open(out_path, 'wb').write(done.stdout)\n"
    "raise SystemExit(done.returncode)\n"
)

#: In-process evaluation of the hook event, no listener and no subprocess (`hook_http.evaluate_cold`).
EVALUATE_COLD_CODE = (
    "import json, sys\n"
    "from coordinator_core.warm.hook_http import evaluate_cold\n"
    "evaluate_cold(json.loads(sys.argv[1]))\n"
)
IMPORT_CODE = "import coordinator_core.warm.hook_http\n"

Measure = Callable[..., dict]


def is_real_verdict(stdout: str) -> bool:
    """True when a hook call's stdout is the guard's own answer, not a fail-open or empty envelope."""
    return not is_fail_open(stdout) and DOOR_PREFIX_MARKER not in stdout


def block_server_spawn(runtime: Path) -> None:
    """Make the door's respawn-on-miss unable to create its service directory (a file occupies the parent).

    `door.c :: door_maybe_spawn_server` returns at `door_ensure_dir_recursive_w` before any process is created."""
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / RUNTIME_BLOCKER).write_text("blocks respawn-on-miss under the measured job\n", encoding="utf-8")


def net_cost(gross: dict, floor: dict) -> dict:
    """`gross` minus the wrapper `floor`, both `{process_time_ms, procs_per_call}`, clamped at zero."""
    return {
        "process_ms": round(max(0.0, gross["process_time_ms"] - floor["process_time_ms"]), 1),
        "spawns": round(max(0.0, gross["procs_per_call"] - floor["procs_per_call"]), 2),
    }


def median_leg(samples: Sequence[dict]) -> dict:
    """Median `process_time_ms` and `procs_per_call` over batched samples, plus the raw sample figures."""
    return {
        "process_time_ms": round(statistics.median(s["process_time_ms"] for s in samples), 1),
        "procs_per_call": round(statistics.median(s["procs_per_call"] for s in samples), 2),
        "samples_ms": [round(s["process_time_ms"], 1) for s in samples],
        "rc": samples[-1]["rc"],
    }


def build_report(legs: dict, verdict: dict, k: int, samples: int, extra: Optional[dict] = None) -> dict:
    """The report object from measured legs: `door`, `floor`, `cold_entry`, `interpreter`, `import`,
    `evaluate_cold` (each a `median_leg`) and the unmeasured-call `verdict` facts."""
    door = net_cost(legs["door"], legs["floor"])
    cold = net_cost(legs["cold_entry"], legs["floor"])
    interpreter = legs["interpreter"]["process_time_ms"]
    imported = legs["import"]["process_time_ms"]
    evaluated = legs["evaluate_cold"]["process_time_ms"]
    report = {
        "op": ENTRYPOINT,
        "command": BENIGN_COMMAND,
        "bar_ms": BAR_MS,
        "k": k,
        "samples": samples,
        "process_ms": door["process_ms"],
        "spawns": door["spawns"],
        "under_bar": door["process_ms"] < BAR_MS,
        "verdict_real": verdict["real"],
        "door_call": {**legs["door"], "wrapper_floor_ms": legs["floor"]["process_time_ms"], "verdict": verdict},
        "decomposition": {
            "interpreter_ms": interpreter,
            "import_ms": round(max(0.0, imported - interpreter), 1),
            "evaluate_cold_ms": round(max(0.0, evaluated - imported), 1),
            "cold_entry_ms": cold["process_ms"],
            "cold_entry_spawns": cold["spawns"],
            "cold_entry_over_bar": cold["process_ms"] >= BAR_MS,
            "door_overhead_ms": round(max(0.0, door["process_ms"] - cold["process_ms"]), 1),
        },
    }
    report.update(extra or {})
    return report


def door_environment(root: Path, runtime: Path, settings: Path) -> dict:
    """The environment of one hook call: engine root pinned to the dead clone, private runtime and settings."""
    from coordinator_core.benchmarks.isolated_clone import engine_env

    env = engine_env(root)
    env.update({
        "COORDINATOR_DOOR_STDIN_MODE": "hook",
        "COORDINATOR_ENGINE_ROOT": str(root),
        "COORDINATOR_WARM_RUNTIME_BASE": str(runtime),
        "COORDINATOR_SETTINGS_HOME": str(settings),
    })
    return env


def measure_batched(cmd: Sequence[str], env: dict, cwd: str, k: int, samples: int, **kw) -> dict:
    """`samples` `process_time.batched_process_time_ms` batches of `k` calls, as one `median_leg`."""
    from coordinator_core.benchmarks.process_time import batched_process_time_ms

    return median_leg([batched_process_time_ms(list(cmd), k=k, env=env, cwd=cwd, **kw) for _ in range(samples)])


def spawn_tally(cmd: Sequence[str], env: dict, cwd: str) -> dict:
    """Per-image process tally of ONE call (`name_processes`): a diagnosis run, never a gating sample.

    Includes the wrapper itself; a process shorter-lived than the poll lands in `unattributed_procs`."""
    from coordinator_core.benchmarks.process_time import batched_process_time_ms

    res = batched_process_time_ms(list(cmd), k=1, env=env, cwd=cwd, name_processes=True)
    return {key: res[key] for key in ("process_tally", "procs_total", "unattributed_procs") if key in res}


def single_call_verdict(door_cmd: Sequence[str], env: dict, cwd: Path, payload: bytes) -> dict:
    """One unmeasured hook call: whether its stdout is a real verdict, and the stdout head."""
    done = subprocess.run(list(door_cmd), input=payload, capture_output=True, env=env, cwd=str(cwd),
                          timeout=60, **no_console_creationflags())
    out = done.stdout.decode("utf-8", "replace")
    return {"real": is_real_verdict(out), "rc": done.returncode, "stdout_head": out.strip()[:200],
            "fail_open_markers": list(FAIL_OPEN_MARKERS) + [DOOR_PREFIX_MARKER]}


def read_fail_open_log(settings: Path) -> dict:
    """Lines the door appended to the private settings home's fail-open log (empty when none)."""
    log = settings / "state" / "door-fail-open.log"
    lines = log.read_text(encoding="utf-8", errors="replace").splitlines() if log.is_file() else []
    return {"lines": len(lines), "last": lines[-1][:300] if lines else None}


def head_sha(repo_root: Path) -> Optional[str]:
    """The repo's HEAD sha, or None when it cannot be read."""
    from coordinator_core.git.run import run_git

    res = run_git(["-C", str(repo_root), "rev-parse", "HEAD"])
    return res.stdout.strip() if res.ok else None


def run(repo_root: Path, door: Path, k: int, samples: int, measure: Measure = measure_batched) -> dict:
    """The full report: build the dead clone, run the legs, tear the clone down."""
    if os.name != "nt":
        raise SystemExit("cold_guard_path_cost: measures the Windows door's hook fall-through and runs only on "
                         "Windows (POSIX: door_hook_read_latency.py --dead-engine)")
    from coordinator_core.benchmarks import isolated_clone

    declare_benchmark_origin()
    parent = isolated_clone.mkdtemp_for_clone(repo_root, prefix="cgp-")
    try:
        root = parent / "clone"
        isolated_clone.clone_engine(repo_root, root, entrypoint="coordinator-invoke")
        shim = Path("coordinator") / "lib" / "resolve-claude-klabauter"
        isolated_clone._link_tree(repo_root / shim, root / shim)
        root = root.resolve()
        runtime, settings = parent / "runtime", parent / "settings"
        settings.mkdir()
        block_server_spawn(runtime)
        payload = hook_payload(repo_root)
        payload_path, wrapper_path, out_path = parent / "payload.json", parent / "wrapper.py", parent / "out.bin"
        payload_path.write_bytes(payload)
        wrapper_path.write_text(WRAPPER_SOURCE, encoding="utf-8")
        env = door_environment(root, runtime, settings)
        cwd = str(repo_root)
        py = sys.executable
        cold_cmd = [py, str(root / "coordinator" / "bin" / "coordinator-invoke.py"), ENTRYPOINT]

        def wrapped(mode: str, cmd: Sequence[str]) -> list:
            return [py, str(wrapper_path), mode, str(out_path), str(payload_path), *cmd]

        verdict = single_call_verdict([str(door), ENTRYPOINT], env, repo_root, payload)
        cold_verdict = single_call_verdict(cold_cmd, env, repo_root, payload)
        legs = {
            "floor": measure(wrapped("noop", []), env, cwd, k, samples),
            "door": measure(wrapped("door", [str(door), ENTRYPOINT]), env, cwd, k, samples),
            "cold_entry": measure(wrapped("door", cold_cmd), env, cwd, k, samples),
            "interpreter": measure([py, "-c", "pass"], env, str(root), k, samples),
            "import": measure([py, "-c", IMPORT_CODE], env, str(root), k, samples),
            "evaluate_cold": measure([py, "-c", EVALUATE_COLD_CODE, payload.decode("utf-8")], env, str(root),
                                     k, samples),
        }
        tally = spawn_tally(wrapped("door", [str(door), ENTRYPOINT]), env, cwd) if measure is measure_batched else {}
        extra = {
            "head_sha": head_sha(repo_root),
            "platform": sys.platform,
            "door": str(door),
            "engine_clone": "stamped hardlink clone of the working tree; no server; respawn-on-miss blocked",
            "cold_entry_verdict": cold_verdict,
            "spawn_tally": tally,
            "fail_open_log": read_fail_open_log(settings),
        }
        return build_report(legs, verdict, k, samples, extra)
    finally:
        isolated_clone.rmtree_or_raise(parent, label="cold_guard_path_cost")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--k", type=int, default=DEFAULT_K, help="calls per batch")
    ap.add_argument("--samples", type=int, default=DEFAULT_SAMPLES, help="batches per leg")
    ap.add_argument("--door", type=Path, default=None, help="door image (default: the installed one)")
    ap.add_argument("--enforce-bar", action="store_true",
                    help=f"exit 2 unless process_ms is under {BAR_MS:.0f} and the call gave a real verdict")
    args = ap.parse_args(argv)
    door = args.door or installed_door_exe()
    if door is None:
        raise SystemExit("cold_guard_path_cost: no installed coordinator-invoke door under the settings home")
    report = run(Path(__file__).resolve().parents[2], door, args.k, args.samples)
    print(json.dumps(report, indent=2))
    return 2 if args.enforce_bar and not (report["under_bar"] and report["verdict_real"]) else 0


def installed_door_exe() -> Optional[Path]:
    """The installed door image: `coordinator-invoke.exe` on Windows, else the POSIX one."""
    posix = installed_door()
    if posix is not None:
        return posix
    home = os.environ.get("COORDINATOR_SETTINGS_HOME") or os.path.join(
        os.environ.get("CLAUDE_HOME") or os.path.expanduser("~"), ".coordinator-claude-settings")
    exe = Path(home) / "bin" / "coordinator-invoke.exe"
    return exe if exe.is_file() else None


if __name__ == "__main__":
    sys.exit(main())
