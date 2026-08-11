"""
coordinator_core.benchmarks.ambient_sampler — standalone ambient-load snapshotter.

Purpose: snapshots what the machine is carrying (live coordinator sessions, Claude
process count, CPU/RAM) at a fixed interval so op-latency records
(coordinator_core.telemetry.op_latency) can be joined against ambient conditions by
timestamp. Real-traffic measurement instrument granted for the load-norm
investigation (docs/wiki/machine-load-norm.md: 50-70 active LLMs is the AVERAGE) —
records facts only, remediates nothing.

Sink: <git_common_dir>/coordinator-sessions/logs/ambient-load.jsonl, one JSON line
per sample:
    {"t": float epoch, "live_sessions": int, "claude_procs": int,
     "cpu_pct": float|null, "ram_free_mb": float|null, "ram_total_mb": float|null}

Windows is first-class here (per repo doctrine: naked Python, no new .sh, no bash) —
process/CPU/RAM counts are gathered via a PowerShell query on Windows and `ps`/
`/proc` style facilities are NOT used. `kill -0` is never used to test liveness (it
lies about native Windows PIDs — state/lessons/kill-0-lies-about-native-windows-
pids.md); `live_sessions` instead reuses the existing, already-correct
coordinator_core.session.liveness.resolve_live_session_ids() resolver rather than
hand-rolling liveness.

Any single sample's process/CPU/RAM gathering failure degrades that field to
``null`` rather than raising — this sampler must survive indefinitely across
whatever the machine throws at it (a PowerShell hang, a WMI hiccup) since it is
itself long-running by design.

Standalone script: `python -m coordinator_core.benchmarks.ambient_sampler [--interval SECONDS]`.
Ctrl-C-clean (SystemExit/KeyboardInterrupt exit the loop without a traceback).
A minimum interval floor (10s) is enforced so this sampler cannot itself become a
load source — one PowerShell spawn per sample at 30s (the default) is acceptable,
per-second is not.

Spec backlink: state/handoffs/2026-08-08-engine-fails-the-load-norm.md
               docs/wiki/machine-load-norm.md
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from coordinator_core import atomic_append

MIN_INTERVAL_SECONDS = 10.0
DEFAULT_INTERVAL_SECONDS = 30.0

_NO_CONSOLE = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _sink_path(git_common_dir_path: Path) -> Path:
    return Path(git_common_dir_path) / "coordinator-sessions" / "logs" / "ambient-load.jsonl"


def _resolve_live_sessions_count() -> int:
    """Live coordinator-session count via the existing native liveness resolver.

    Never hand-rolls liveness — delegates to
    coordinator_core.session.liveness.resolve_live_session_ids(), the TTL-cached
    resolver already used for this purpose elsewhere in the engine. Returns 0 on
    any resolution failure (e.g. not inside a git repo) rather than raising.
    """
    try:
        from coordinator_core.session.liveness import resolve_live_session_ids

        return len(resolve_live_session_ids())
    except Exception:
        return 0


def _windows_process_and_memory_sample() -> "tuple[Optional[int], Optional[float], Optional[float], Optional[float]]":
    """Return (claude_procs, cpu_pct, ram_free_mb, ram_total_mb) via PowerShell on Windows.

    Every field independently degrades to None on failure — a single WMI/CIM
    hiccup must not blank out fields that DID resolve. `kill -0` / raw PID
    liveness is never used here (see module docstring).
    """
    claude_procs: Optional[int] = None
    cpu_pct: Optional[float] = None
    ram_free_mb: Optional[float] = None
    ram_total_mb: Optional[float] = None

    try:
        proc_cmd = (
            "(Get-Process | Where-Object { $_.ProcessName -match 'claude' } "
            "| Measure-Object).Count"
        )
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-Command", proc_cmd],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=_NO_CONSOLE,
        )
        if out.returncode == 0 and out.stdout.strip():
            claude_procs = int(out.stdout.strip())
    except Exception:
        claude_procs = None

    try:
        os_cmd = (
            "$os = Get-CimInstance Win32_OperatingSystem; "
            "Write-Output ($os.FreePhysicalMemory); "
            "Write-Output ($os.TotalVisibleMemorySize)"
        )
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-Command", os_cmd],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=_NO_CONSOLE,
        )
        if out.returncode == 0:
            lines = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
            if len(lines) >= 2:
                # Win32_OperatingSystem reports these fields in KB.
                ram_free_mb = float(lines[0]) / 1024.0
                ram_total_mb = float(lines[1]) / 1024.0
    except Exception:
        ram_free_mb = None
        ram_total_mb = None

    try:
        cpu_cmd = "(Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average"
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-Command", cpu_cmd],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=_NO_CONSOLE,
        )
        if out.returncode == 0 and out.stdout.strip():
            cpu_pct = float(out.stdout.strip())
    except Exception:
        cpu_pct = None

    return claude_procs, cpu_pct, ram_free_mb, ram_total_mb


def _non_windows_process_sample() -> "tuple[Optional[int], Optional[float], Optional[float], Optional[float]]":
    """Best-effort claude-process count via `ps` on POSIX; CPU/RAM left null.

    This box is Windows (repo doctrine notes it explicitly), so this branch is
    a graceful fallback only — not the primary tested path.
    """
    claude_procs: Optional[int] = None
    try:
        out = subprocess.run(
            ["ps", "-eo", "comm"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=_NO_CONSOLE,
        )
        if out.returncode == 0:
            claude_procs = sum(
                1 for line in out.stdout.splitlines() if "claude" in line.lower()
            )
    except Exception:
        claude_procs = None
    return claude_procs, None, None, None


def take_sample() -> dict:
    """Collect one ambient-load sample dict (not yet written to disk).

    Never raises — every field independently degrades to null/0 on failure.
    """
    live_sessions = _resolve_live_sessions_count()

    if platform.system() == "Windows":
        claude_procs, cpu_pct, ram_free_mb, ram_total_mb = _windows_process_and_memory_sample()
    else:
        claude_procs, cpu_pct, ram_free_mb, ram_total_mb = _non_windows_process_sample()

    return {
        "t": time.time(),
        "live_sessions": live_sessions,
        "claude_procs": claude_procs,
        "cpu_pct": cpu_pct,
        "ram_free_mb": ram_free_mb,
        "ram_total_mb": ram_total_mb,
    }


def append_sample(sink: Path, sample: dict) -> None:
    """Append one sample as a compact JSON line to ``sink``, atomically.

    Routed through coordinator_core.atomic_append.append_line — the same
    shared atomic-append primitive coordinator_core.telemetry.op_latency
    uses, not a second copy of the same mechanism. Failures are swallowed
    (this sampler must keep running).
    """
    try:
        os.makedirs(sink.parent, exist_ok=True)
        line = json.dumps(sample, separators=(",", ":")) + "\n"
        atomic_append.append_line(sink, line.encode("utf-8"))
    except OSError as exc:
        print(f"ambient_sampler: cannot write {sink}: {exc}", file=sys.stderr)


def _resolve_sink(repo_arg: Optional[str]) -> Path:
    from coordinator_core.lifecycle import find_repo_root, git_common_dir

    if repo_arg is not None:
        repo_root = Path(repo_arg).resolve()
    else:
        repo_root = find_repo_root()
    common_dir = git_common_dir(repo_root)
    return _sink_path(common_dir)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="coordinator_core.benchmarks.ambient_sampler",
        description="Standalone ambient-load snapshotter (load-norm measurement instrument).",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL_SECONDS,
        help=f"Sample interval in seconds (default {DEFAULT_INTERVAL_SECONDS}; "
        f"floor {MIN_INTERVAL_SECONDS} — this sampler must not itself become a load source).",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="Repo root (default: resolved from cwd via find_repo_root()).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        default=False,
        help="Take exactly one sample and exit (for testing/manual invocation).",
    )
    args = parser.parse_args(argv)

    interval = max(args.interval, MIN_INTERVAL_SECONDS)

    try:
        sink = _resolve_sink(args.repo)
    except RuntimeError as exc:
        print(f"ambient_sampler: cannot resolve repo root: {exc}", file=sys.stderr)
        return 1

    try:
        while True:
            sample = take_sample()
            append_sample(sink, sample)
            if args.once:
                return 0
            time.sleep(interval)
    except (KeyboardInterrupt, SystemExit):
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
