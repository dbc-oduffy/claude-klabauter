"""Hook-mode door read benchmark: process time and fail-open count of an installed-door hook call.

Runs the installed POSIX door in hook mode (`COORDINATOR_DOOR_STDIN_MODE=hook`) against the entrypoint
`hooks.preuse_bash_dispatch`, N times, and prints one JSON object. Process ms is the door child's own `wait4`
rusage (user + system, its whole waited tree), never wall clock; the engine's own CPU is not in it unless the
door fell through to a cold guard. A call is `fail_open` when the door answered with its engine-failure allow
envelope ("guard not evaluated") instead of the engine's answer.

Legs: `ops` drives the live engine with a benign Bash command (`hooks.preuse_bash_dispatch`) and a commit-shaped
one (`validate-commit`, the guard that composes git work on a `git commit`); `--guard-delay-ms N` points the door
at a throwaway engine whose socket answers the guard after N ms; `--dead-engine` points it at a socket path with
no listener. The throwaway engines run under a private settings home and runtime base, so nothing lands in the
live fail-open log.

`measure()` is the entry the pin test drives. POSIX only. The bar is the 500ms brightline (DR-344).

Run: `python3 coordinator_core/benchmarks/door_hook_read_latency.py [--runs N] [--door PATH]
[--guard-delay-ms N] [--dead-engine] [--enforce-bar]`.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
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
#: Op names reported per leg; validate-commit is a guard inside the preuse_bash_dispatch chain.
OP_PREUSE = ENTRYPOINT
OP_VALIDATE_COMMIT = "validate-commit"
BENIGN_COMMAND = "git status"
COMMIT_COMMAND = "git commit -m door-hook-read-latency-bench"
#: Texts of the door's two fail-open envelopes: engine failure after delivery ("guard not evaluated") and an
#: unreachable engine whose cold guard gave no verdict ("guard did not run").
FAIL_OPEN_MARKERS = ("guard not evaluated", "guard did not run")
#: Backstop above the 8s hook deadline so a wedged door cannot hang the benchmark.
_WATCHDOG_S = 60.0
#: The engine's allow answer for a Bash PreToolUse, as the door's response envelope carries it.
_ALLOW_REPLY = (
    '{"jsonrpc":"2.0","id":1,"result":{"stdout":"{\\"hookSpecificOutput\\":{\\"hookEventName\\":'
    '\\"PreToolUse\\",\\"permissionDecision\\":\\"allow\\"}}\\n","stderr":"","exit_code":0}}\n'
).encode("utf-8")


def installed_door() -> Optional[Path]:
    """The installed `coordinator-invoke` under the settings home, or None when absent."""
    home = os.environ.get("COORDINATOR_SETTINGS_HOME") or os.path.join(
        os.environ.get("CLAUDE_HOME") or os.path.expanduser("~"), ".coordinator-claude-settings")
    door = Path(home) / "bin" / "coordinator-invoke"
    return door if door.is_file() else None


def hook_payload(cwd: Path, command: str = BENIGN_COMMAND) -> bytes:
    """A Bash PreToolUse hook payload for `command`."""
    return json.dumps({
        "hook_event_name": "PreToolUse", "session_id": "door-hook-read-latency-bench",
        "cwd": str(cwd), "tool_name": "Bash", "tool_input": {"command": command},
    }).encode("utf-8")


def is_fail_open(stdout: str) -> bool:
    """True when the door's stdout is its engine-failure allow envelope, not the engine's answer."""
    return not stdout.strip() or any(m in stdout for m in FAIL_OPEN_MARKERS)


def _door_call(door: Path, cwd: Path, command: str = BENIGN_COMMAND,
               env_extra: Optional[dict] = None) -> tuple[float, str, float]:
    """One hook-mode call: (door process ms, stdout, wall ms)."""
    env = dict(os.environ)
    env["COORDINATOR_DOOR_STDIN_MODE"] = "hook"
    env.update(env_extra or {})
    started = time.monotonic()
    proc = subprocess.Popen(
        [str(door), ENTRYPOINT], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=env, cwd=str(cwd), **no_console_creationflags(),
    )
    watchdog = threading.Timer(_WATCHDOG_S, proc.kill)
    watchdog.start()
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(hook_payload(cwd, command))
        proc.stdin.close()
        out = proc.stdout.read()
        proc.stdout.close()
        _, _status, ru = os.wait4(proc.pid, 0)
        proc.returncode = 0
    finally:
        watchdog.cancel()
    wall_ms = (time.monotonic() - started) * 1000.0
    return (ru.ru_utime + ru.ru_stime) * 1000.0, out.decode("utf-8", "replace"), wall_ms


def summarize(samples_ms: list[float], fail_opens: int, door: str) -> dict:
    """The report object: runs, max/median process ms, fail-open count, door path."""
    return {
        "runs": len(samples_ms),
        "max_process_ms": round(max(samples_ms), 1) if samples_ms else None,
        "median_process_ms": round(statistics.median(samples_ms), 1) if samples_ms else None,
        "fail_open": fail_opens,
        "door": door,
    }


def _require_posix_door(door: Optional[Path]) -> Path:
    if os.name == "nt":
        raise RuntimeError("door_hook_read_latency drives the POSIX door; it does not run on Windows")
    door = door or installed_door()
    if door is None:
        raise RuntimeError("no installed coordinator-invoke door under the settings home")
    return door


def _repo_cwd(cwd: Optional[Path]) -> Path:
    return cwd or Path(__file__).resolve().parents[2]


def _measure_leg(runs: int, door: Path, cwd: Path, command: str, env_extra: Optional[dict] = None) -> dict:
    """The report for `runs` calls of `command`, plus the slowest wall ms (`max_elapsed_ms`)."""
    samples: list[float] = []
    walls: list[float] = []
    fail_opens = 0
    for _ in range(runs):
        ms, out, wall = _door_call(door, cwd, command, env_extra)
        samples.append(ms)
        walls.append(wall)
        fail_opens += is_fail_open(out)
    report = summarize(samples, fail_opens, str(door))
    report["max_elapsed_ms"] = round(max(walls), 1) if walls else None
    return report


def measure(runs: int = DEFAULT_RUNS, door: Optional[Path] = None, cwd: Optional[Path] = None) -> dict:
    """Run `runs` benign hook-mode calls through the installed door and return the report object."""
    door = _require_posix_door(door)
    report = _measure_leg(runs, door, _repo_cwd(cwd), BENIGN_COMMAND)
    report.pop("max_elapsed_ms")
    return report


def measure_ops(runs: int = DEFAULT_RUNS, door: Optional[Path] = None, cwd: Optional[Path] = None) -> dict:
    """Per-op reports against the live engine: `hooks.preuse_bash_dispatch` and `validate-commit`."""
    door = _require_posix_door(door)
    cwd = _repo_cwd(cwd)
    return {
        OP_PREUSE: _measure_leg(runs, door, cwd, BENIGN_COMMAND),
        OP_VALIDATE_COMMIT: _measure_leg(runs, door, cwd, COMMIT_COMMAND),
    }


class _FakeEngine:
    """A throwaway engine root plus a Unix socket the door can elect; optionally answers after a delay.

    The root carries a unique `_engine_stamp` (what the door hashes into its token) and a cold script
    that exits at once, so a dead-engine door falls through to an instantly-empty cold guard. The socket
    lives under a short `/tmp` runtime base: `sun_path` is 104 bytes on macOS and a pytest-style tmp root
    overflows it.
    """

    def __init__(self, delay_ms: Optional[int]) -> None:
        self.delay_ms = delay_ms
        self.base = Path(tempfile.mkdtemp(prefix="dhrl", dir="/tmp"))
        self.root = self.base / "e"
        self.runtime = self.base / "r"
        self.settings = self.base / "s"
        (self.root / "coordinator_core").mkdir(parents=True)
        (self.root / "coordinator_core" / "_engine_stamp").write_text(
            f"door-hook-read-latency-{os.getpid()}-{time.time_ns()}\n", encoding="utf-8")
        (self.root / "coordinator" / "bin").mkdir(parents=True)
        (self.root / "coordinator" / "bin" / "coordinator-invoke.py").write_text(
            "raise SystemExit(0)\n", encoding="utf-8")
        self.settings.mkdir()
        self.root = self.root.resolve()
        self.sock_path = self._elected_socket_path()
        self.sock_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.sock_path.parent, 0o700)
        os.chmod(self.sock_path.parent.parent, 0o755)
        self._sock: Optional[socket.socket] = None
        self._stop = threading.Event()
        if delay_ms is not None:
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._sock.bind(str(self.sock_path))
            self._sock.listen(8)
            threading.Thread(target=self._accept_loop, daemon=True).start()

    def _elected_socket_path(self) -> Path:
        from coordinator_core.warm import breadcrumb, election, skew

        prior = os.environ.get(breadcrumb.RUNTIME_BASE_ENV)
        os.environ[breadcrumb.RUNTIME_BASE_ENV] = str(self.runtime)
        try:
            return election.socket_path(skew.compute_client_token(self.root), engine_clone=self.root)
        finally:
            if prior is None:
                os.environ.pop(breadcrumb.RUNTIME_BASE_ENV, None)
            else:
                os.environ[breadcrumb.RUNTIME_BASE_ENV] = prior

    def _accept_loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._answer, args=(conn,), daemon=True).start()

    def _answer(self, conn: socket.socket) -> None:
        try:
            received = b""
            while b"\n" not in received:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                received += chunk
            time.sleep((self.delay_ms or 0) / 1000.0)
            conn.sendall(_ALLOW_REPLY)
        except OSError:
            pass
        finally:
            conn.close()

    def env(self) -> dict:
        return {
            "COORDINATOR_DOOR_ENGINE_ROOT": str(self.root),
            "COORDINATOR_WARM_RUNTIME_BASE": str(self.runtime),
            "COORDINATOR_SETTINGS_HOME": str(self.settings),
        }

    def close(self) -> None:
        self._stop.set()
        if self._sock is not None:
            self._sock.close()
        shutil.rmtree(self.base, ignore_errors=True)


def measure_slow_guard(delay_ms: int, runs: int = DEFAULT_RUNS, door: Optional[Path] = None,
                       cwd: Optional[Path] = None) -> dict:
    """Report for a live engine whose guard answers `delay_ms` after the request arrives.

    `fail_open` is the count of calls where the door gave up before the guard's verdict arrived.
    """
    door = _require_posix_door(door)
    engine = _FakeEngine(delay_ms)
    try:
        report = _measure_leg(runs, door, _repo_cwd(cwd), BENIGN_COMMAND, engine.env())
    finally:
        engine.close()
    report["guard_delay_ms"] = delay_ms
    return report


def measure_dead_engine(runs: int = DEFAULT_RUNS, door: Optional[Path] = None,
                        cwd: Optional[Path] = None) -> dict:
    """Report for an engine socket path nothing listens on: the door should fail open fast."""
    door = _require_posix_door(door)
    engine = _FakeEngine(None)
    try:
        return _measure_leg(runs, door, _repo_cwd(cwd), BENIGN_COMMAND, engine.env())
    finally:
        engine.close()


def main() -> int:
    declare_benchmark_origin()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    ap.add_argument("--door", type=Path, default=None)
    ap.add_argument("--guard-delay-ms", type=int, default=None,
                    help="also run a leg against a throwaway engine whose guard answers after N ms")
    ap.add_argument("--dead-engine", action="store_true",
                    help="also run a leg against a socket path with no listener")
    ap.add_argument("--enforce-bar", action="store_true",
                    help=f"exit 2 unless every op has max_process_ms <= {BAR_MS:.0f} and fail_open == 0 "
                         "(and a slow-guard leg has fail_open == 0, a dead-engine leg max_elapsed_ms <= bar)")
    args = ap.parse_args()
    report: dict = {"ops": measure_ops(args.runs, args.door)}
    if args.guard_delay_ms is not None:
        report["slow_guard"] = measure_slow_guard(args.guard_delay_ms, args.runs, args.door)
    if args.dead_engine:
        report["dead_engine"] = measure_dead_engine(args.runs, args.door)
    print(json.dumps(report))
    ok = all(r["max_process_ms"] is not None and r["max_process_ms"] <= BAR_MS and r["fail_open"] == 0
             for r in report["ops"].values())
    if "slow_guard" in report:
        ok = ok and report["slow_guard"]["fail_open"] == 0
    if "dead_engine" in report:
        ok = ok and report["dead_engine"]["max_elapsed_ms"] is not None \
            and report["dead_engine"]["max_elapsed_ms"] <= BAR_MS
    return 2 if args.enforce_bar and not ok else 0


if __name__ == "__main__":
    sys.exit(main())
