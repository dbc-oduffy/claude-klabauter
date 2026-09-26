
from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
from pathlib import Path
from typing import Optional

from coordinator_core import atomic_append

MIN_INTERVAL_SECONDS = 10.0
DEFAULT_INTERVAL_SECONDS = 30.0


def _sink_path(git_common_dir_path: Path) -> Path:
    return Path(git_common_dir_path) / "coordinator-sessions" / "logs" / "ambient-load.jsonl"


def _resolve_live_sessions_count() -> Optional[int]:
    try:
        from coordinator_core.session.liveness import resolve_live_session_ids

        return len(resolve_live_session_ids())
    except Exception:
        return None


def _windows_process_and_memory_sample() -> "tuple[Optional[int], Optional[float], Optional[float], Optional[float]]":
    import psutil

    claude_procs: Optional[int] = None
    cpu_pct: Optional[float] = None
    ram_free_mb: Optional[float] = None
    ram_total_mb: Optional[float] = None

    try:
        claude_procs = sum(
            1
            for p in psutil.process_iter(["name"])
            if "claude" in (p.info["name"] or "").lower()
        )
    except Exception:
        claude_procs = None

    try:
        vm = psutil.virtual_memory()
        ram_free_mb = vm.available / 1048576.0
        ram_total_mb = vm.total / 1048576.0
    except Exception:
        ram_free_mb = None
        ram_total_mb = None

    try:
        cpu_pct = psutil.cpu_percent(interval=0.3)
    except Exception:
        cpu_pct = None

    return claude_procs, cpu_pct, ram_free_mb, ram_total_mb


def _non_windows_process_sample() -> "tuple[Optional[int], Optional[float], Optional[float], Optional[float]]":
    claude_procs: Optional[int] = None
    try:
        import psutil

        claude_procs = sum(
            1
            for proc in psutil.process_iter(["name"])
            if "claude" in (proc.info.get("name") or "").lower()
        )
    except Exception:
        claude_procs = None
    return claude_procs, None, None, None


def take_sample() -> dict:
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
