"""Per-test-file peak memory of an engine-launched pytest child, bounded by ``BOUND_BYTES``.

``measure_file`` / ``measure_files`` run pytest in a child built with ``guarded_child_env`` (BLAS
pools capped, nesting depth stamped) and poll the child TREE every ``POLL_S``. Windows sums each
live tree member's ``peak_wset`` (an over-estimate, never an under-count); POSIX sums the tree's
polled ``rss`` and takes the max over polls (``ru_maxrss`` spans every past child, not one run).

Trap: a tree member that starts and exits between two polls is not seen.
CLI: ``python -m coordinator_core.testing.peak_rss --scope <dir> [--scope ...] [--json] [--combined]``;
``--combined`` runs every target in ONE pytest process (the shape of an engine test-runner leg).
Exit status 1 when any result is over the bound.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

from coordinator_core.testing.nested_session_guard import guarded_child_env

BOUND_BYTES = 400 * 1024 * 1024
POLL_S = 0.25
TIMEOUT_S = 540
REPO_ROOT = Path(__file__).resolve().parents[2]
_WIN = sys.platform == "win32"


@dataclass(frozen=True)
class PeakResult:
    """Peak tree memory of one pytest run; ``path`` is a file, or ``<combined>`` for a joint run."""

    path: str
    peak_bytes: int
    returncode: int


def _import_psutil():
    import psutil

    return psutil


def _private_bytes(proc) -> int:
    if _WIN:
        return proc.memory_info().private
    return proc.memory_full_info().uss


def _tree_peak_poll(root, seen: dict) -> None:
    """Fold one sample of ``root`` and its descendants into ``seen`` (pid -> best figure so far)."""
    psutil = _import_psutil()
    try:
        members = [root, *root.children(recursive=True)]
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return
    for proc in members:
        try:
            info = proc.memory_info()
            figure = info.peak_wset if _WIN else info.rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        seen[proc.pid] = max(seen.get(proc.pid, 0), figure)


def _tree_total(seen: dict, snapshot_total: int) -> int:
    # Windows: peak_wset is per-process monotonic, so the sum of per-pid peaks bounds the tree.
    # POSIX: a sum of per-pid maxima would over-count non-overlapping children; use the per-poll total.
    return sum(seen.values()) if _WIN else snapshot_total


def _run_polled(argv: Sequence[str], env: Mapping[str, str], cwd: str, timeout_s: float, poll_s: float, sampler):
    """Run ``argv`` to completion calling ``sampler(psutil_proc)`` each poll; return (returncode, state)."""
    psutil = _import_psutil()
    child = subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=dict(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    deadline = time.monotonic() + timeout_s
    try:
        proc = psutil.Process(child.pid)
        while child.poll() is None:
            if time.monotonic() > deadline:
                _kill_tree(child)
                raise TimeoutError(f"child exceeded {timeout_s}s: {list(argv)}")
            sampler(proc)
            time.sleep(poll_s)
        child.communicate(timeout=30)
    finally:
        if child.poll() is None:
            _kill_tree(child)
            child.communicate()
    return child.returncode


def _kill_tree(child) -> None:
    psutil = _import_psutil()
    try:
        members = psutil.Process(child.pid).children(recursive=True)
    except psutil.Error:
        members = []
    for proc in members:
        try:
            proc.kill()
        except psutil.Error:
            pass
    try:
        child.kill()
    except OSError:
        pass


def peak_private_bytes(argv: Sequence[str], env: Mapping[str, str], *, cwd: Optional[str] = None,
                       timeout_s: float = 120, poll_s: float = 0.02) -> tuple:
    """Run ``argv``; return (returncode, peak private bytes of the root process while it ran).

    Raises TimeoutError past ``timeout_s`` (the tree is killed first).
    """
    psutil = _import_psutil()
    state = {"peak": 0}

    def sampler(proc) -> None:
        try:
            state["peak"] = max(state["peak"], _private_bytes(proc))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    rc = _run_polled(argv, env, cwd or str(REPO_ROOT), timeout_s, poll_s, sampler)
    return rc, state["peak"]


def _pytest_argv(paths: Sequence[str]) -> list:
    return [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:randomly", *paths]


def measure_files(paths: Sequence[str], *, env: Optional[Mapping[str, str]] = None, label: Optional[str] = None,
                  timeout_s: float = TIMEOUT_S, poll_s: float = POLL_S) -> PeakResult:
    """Run ``paths`` in ONE pytest child; return the child tree's peak memory and its exit code."""
    base = dict(os.environ if env is None else env)
    child_env = guarded_child_env(base)
    snapshot = {"max_total": 0}
    seen: dict = {}

    def sampler(proc) -> None:
        poll: dict = {}
        _tree_peak_poll(proc, poll)
        for pid, figure in poll.items():
            seen[pid] = max(seen.get(pid, 0), figure)
        snapshot["max_total"] = max(snapshot["max_total"], sum(poll.values()))

    rc = _run_polled(_pytest_argv(paths), child_env, str(REPO_ROOT), timeout_s, poll_s, sampler)
    return PeakResult(label if label is not None else ",".join(paths), _tree_total(seen, snapshot["max_total"]), rc)


def measure_file(path: str, *, env: Optional[Mapping[str, str]] = None, **kwargs) -> PeakResult:
    """Peak tree memory of pytest running the single file ``path`` alone."""
    return measure_files([str(path)], env=env, label=str(path), **kwargs)


def over_bound(results: Iterable[PeakResult], bound: int = BOUND_BYTES) -> list:
    """The results whose peak exceeds ``bound`` bytes."""
    return [r for r in results if r.peak_bytes > bound]


def discover_files(scopes: Iterable[str]) -> list:
    """Sorted repo-relative posix paths of every ``test_*.py`` under the scope directories."""
    out = set()
    for scope in scopes:
        base = Path(scope)
        base = base if base.is_absolute() else REPO_ROOT / base
        for path in base.rglob("test_*.py"):
            if "__pycache__" in path.parts:
                continue
            try:
                out.add(path.resolve().relative_to(REPO_ROOT).as_posix())
            except ValueError:
                out.add(path.resolve().as_posix())
    return sorted(out)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="peak_rss", description=__doc__.splitlines()[0])
    parser.add_argument("--scope", action="append", required=True, help="directory of test files (repeatable)")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--combined", action="store_true", help="run all targets in ONE process")
    parser.add_argument("--bound-mib", type=float, default=BOUND_BYTES / 2**20)
    args = parser.parse_args(argv)
    bound = int(args.bound_mib * 2**20)
    files = discover_files(args.scope)
    if args.combined:
        results = [measure_files(files, label="<combined>")] if files else []
    else:
        results = [measure_file(f) for f in files]
    bad = over_bound(results, bound)
    if args.as_json:
        print(json.dumps({
            "bound_bytes": bound,
            "results": [{"path": r.path, "peak_bytes": r.peak_bytes, "returncode": r.returncode} for r in results],
            "over_bound": [r.path for r in bad],
        }, indent=2))
    else:
        for r in sorted(results, key=lambda r: -r.peak_bytes):
            flag = "OVER" if r.peak_bytes > bound else "ok"
            print(f"{r.peak_bytes / 2**20:9.1f} MiB  rc={r.returncode}  {flag:4}  {r.path}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
