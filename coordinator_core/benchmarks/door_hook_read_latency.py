"""Hook-mode door read benchmark: process time and fail-open count of an installed-door Bash hook call.

Runs the installed POSIX door in hook mode (`COORDINATOR_DOOR_STDIN_MODE=hook`) against the entrypoint
`hooks.preuse_bash_dispatch` with a benign Bash `PreToolUse` payload, N times, and prints one JSON object:
`{"runs", "max_process_ms", "median_process_ms", "fail_open", "door"}`. Process ms is the door child's own
`wait4` rusage (user + system, its whole waited tree), never wall clock. A call is `fail_open` when the door
answered with its engine-failure allow envelope ("guard not evaluated") instead of the engine's answer, which
is what a stalled read to the hook deadline produces.

`measure()` is the entry the pin test drives. POSIX only. The bar is the 500ms brightline (DR-344).

Run: `python3 coordinator_core/benchmarks/door_hook_read_latency.py [--runs N] [--door PATH] [--enforce-bar]`.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.benchmarks import declare_benchmark_origin
from coordinator_core.win_portability import no_console_creationflags

#: The brightline process-time bar, ms.
BAR_MS = 500.0
DEFAULT_RUNS = 20
ENTRYPOINT = "hooks.preuse_bash_dispatch"
#: Text the door's engine-failure allow envelope carries (door_engine_failure_allow_envelope).
FAIL_OPEN_MARKER = "guard not evaluated"
#: Backstop above the 8s hook deadline so a wedged door cannot hang the benchmark.
_WATCHDOG_S = 60.0


def installed_door() -> Optional[Path]:
    """The installed `coordinator-invoke` under the settings home, or None when absent."""
    home = os.environ.get("COORDINATOR_SETTINGS_HOME") or os.path.join(
        os.environ.get("CLAUDE_HOME") or os.path.expanduser("~"), ".coordinator-claude-settings")
    door = Path(home) / "bin" / "coordinator-invoke"
    return door if door.is_file() else None


def hook_payload(cwd: Path) -> bytes:
    """A benign Bash PreToolUse hook payload."""
    return json.dumps({
        "hook_event_name": "PreToolUse", "session_id": "door-hook-read-latency-bench",
        "cwd": str(cwd), "tool_name": "Bash", "tool_input": {"command": "git status"},
    }).encode("utf-8")


def is_fail_open(stdout: str) -> bool:
    """True when the door's stdout is its engine-failure allow envelope, not the engine's answer."""
    return not stdout.strip() or FAIL_OPEN_MARKER in stdout


def _door_call(door: Path, cwd: Path) -> tuple[float, str]:
    env = dict(os.environ)
    env["COORDINATOR_DOOR_STDIN_MODE"] = "hook"
    proc = subprocess.Popen(
        [str(door), ENTRYPOINT], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=env, cwd=str(cwd), **no_console_creationflags(),
    )
    watchdog = threading.Timer(_WATCHDOG_S, proc.kill)
    watchdog.start()
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(hook_payload(cwd))
        proc.stdin.close()
        out = proc.stdout.read()
        proc.stdout.close()
        _, _status, ru = os.wait4(proc.pid, 0)
        proc.returncode = 0
    finally:
        watchdog.cancel()
    return (ru.ru_utime + ru.ru_stime) * 1000.0, out.decode("utf-8", "replace")


def summarize(samples_ms: list[float], fail_opens: int, door: str) -> dict:
    """The report object: runs, max/median process ms, fail-open count, door path."""
    return {
        "runs": len(samples_ms),
        "max_process_ms": round(max(samples_ms), 1) if samples_ms else None,
        "median_process_ms": round(statistics.median(samples_ms), 1) if samples_ms else None,
        "fail_open": fail_opens,
        "door": door,
    }


def measure(runs: int = DEFAULT_RUNS, door: Optional[Path] = None, cwd: Optional[Path] = None) -> dict:
    """Run `runs` hook-mode calls through the installed door and return the report object."""
    if os.name == "nt":
        raise RuntimeError("door_hook_read_latency drives the POSIX door; it does not run on Windows")
    door = door or installed_door()
    if door is None:
        raise RuntimeError("no installed coordinator-invoke door under the settings home")
    cwd = cwd or Path(__file__).resolve().parents[2]
    samples: list[float] = []
    fail_opens = 0
    for _ in range(runs):
        ms, out = _door_call(door, cwd)
        samples.append(ms)
        fail_opens += is_fail_open(out)
    return summarize(samples, fail_opens, str(door))


def main() -> int:
    declare_benchmark_origin()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    ap.add_argument("--door", type=Path, default=None)
    ap.add_argument("--enforce-bar", action="store_true",
                    help=f"exit 2 unless max_process_ms <= {BAR_MS:.0f} and fail_open == 0")
    args = ap.parse_args()
    report = measure(args.runs, args.door)
    print(json.dumps(report))
    ok = report["max_process_ms"] is not None and report["max_process_ms"] <= BAR_MS and report["fail_open"] == 0
    return 2 if args.enforce_bar and not ok else 0


if __name__ == "__main__":
    sys.exit(main())
