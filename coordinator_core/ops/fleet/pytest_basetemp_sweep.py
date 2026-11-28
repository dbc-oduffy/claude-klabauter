"""Cross-repo sweep of dead-pid pytest run dirs under every ``<temp base>/coordinator/*/pytest/`` (``temp_layout.pytest_sweep_roots``).

Negative spec: never deletes a run dir owned by a live pid or this process, one carrying
``PYTEST_KEPT_MARKER``, one whose name does not parse, anything under ``_fleet``, or a link.
No subprocess is spawned.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional, Sequence

from coordinator_core.group_em import session_registry
from coordinator_core.ops.fleet.scratch_hygiene_plan import _is_link
from coordinator_core.temp_layout import (
    FLEET_DIRNAME,
    PYTEST_KEPT_MARKER,
    _KEPT_MAX_AGE_S,
    parse_pytest_run_dir,
    pytest_sweep_roots,
)

_PYTEST_DIRNAME = "pytest"
_REAP_DIRNAME = ".reap"


def _children(path: Path) -> list[str]:
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def _candidates() -> list[tuple[Path, Path, int]]:
    """``(repo temp dir, run dir, owning pid)`` for every unmarked, parseable, non-link run dir."""
    found: list[tuple[Path, Path, int]] = []
    for root in pytest_sweep_roots():
        _scan_root(root, found)
    return found


def _scan_root(root: Path, found: list[tuple[Path, Path, int]]) -> None:
    for repo_name in _children(root):
        repo_dir = root / repo_name
        if repo_name == FLEET_DIRNAME or _is_link(repo_dir):
            continue
        pytest_dir = repo_dir / _PYTEST_DIRNAME
        if _is_link(pytest_dir) or not pytest_dir.is_dir():
            continue
        for run_name in _children(pytest_dir):
            pid = parse_pytest_run_dir(run_name)
            run_dir = pytest_dir / run_name
            if pid is None or _is_link(run_dir) or not run_dir.is_dir():
                continue
            if (run_dir / PYTEST_KEPT_MARKER).exists():
                continue
            found.append((repo_dir, run_dir, pid))


def _pytest_dirs(roots: Optional[Sequence[Path]]) -> list[Path]:
    """One listdir of each coordinator root, then of each repo: the pytest dirs, never their children."""
    found: list[Path] = []
    for root in pytest_sweep_roots() if roots is None else roots:
        if _is_link(root):
            continue
        for repo_name in _children(root):
            repo_dir = root / repo_name
            if repo_name == FLEET_DIRNAME or _is_link(repo_dir):
                continue
            pytest_dir = repo_dir / _PYTEST_DIRNAME
            if _is_link(pytest_dir) or not pytest_dir.is_dir():
                continue
            found.append(pytest_dir)
    return found


def _kept_fresh(run_dir: Path, now: float) -> bool:
    marker = run_dir / PYTEST_KEPT_MARKER
    if not marker.exists():
        return False
    try:
        return now - marker.stat().st_mtime < _KEPT_MAX_AGE_S
    except OSError:
        return True


def tombstone_one(path: Path) -> Optional[Path]:
    """``os.replace`` ``path`` into its parent's ``.reap`` dir. No walk. None when it did not move."""
    path = Path(path)
    try:
        if not path.exists() or path.is_symlink():
            return None
    except OSError:
        return None
    reap = path.parent / _REAP_DIRNAME
    try:
        reap.mkdir(exist_ok=True)
        dest = reap / path.name
        if dest.exists():
            dest = reap / f"{path.name}-{os.getpid()}"
        os.replace(path, dest)
    except OSError:
        return None
    return dest


def tombstone_dead_runs(
    *,
    roots: Optional[Sequence[Path]] = None,
    now: Optional[float] = None,
    check=lambda: False,
) -> int:
    """Rename dead-pid run dirs into ``<pytest>/.reap/<name>``. One listdir per pytest dir; no walk.

    A kept marker younger than ``temp_layout._KEPT_MAX_AGE_S`` stays. ``check()`` True stops the scan.
    ``.reap`` does not parse as a run dir, so other sweepers skip it.
    """
    now = time.time() if now is None else now
    own_pid = os.getpid()
    moved = 0
    for pytest_dir in _pytest_dirs(roots):
        if check():
            break
        for run_name in _children(pytest_dir):
            if check():
                return moved
            if run_name.startswith("."):
                continue
            pid = parse_pytest_run_dir(run_name)
            run_dir = pytest_dir / run_name
            if pid is None or _is_link(run_dir) or not run_dir.is_dir():
                continue
            if _kept_fresh(run_dir, now):
                continue
            if pid == own_pid or session_registry.pid_alive(pid):
                continue
            if tombstone_one(run_dir) is not None:
                moved += 1
    return moved


def _drain_tree(root: str, check, *, keep_root: bool) -> int:
    """Unlink or rmdir one entry at a time, children before parents. ``check()`` True stops."""
    removed = 0
    stack: list[tuple[str, bool]] = [(root, False)]
    while stack:
        if check():
            break
        cur, seen = stack.pop()
        is_root = os.path.normpath(cur) == os.path.normpath(root)
        if not is_root and (_is_link(cur) or not os.path.isdir(cur)):
            try:
                os.unlink(cur)
                removed += 1
            except OSError:
                pass
            if check():
                break
            continue
        if not seen:
            try:
                kids = [entry.path for entry in os.scandir(cur)]
            except OSError:
                continue
            stack.append((cur, True))
            for kid in reversed(kids):
                stack.append((kid, False))
            continue
        if keep_root and is_root:
            continue
        try:
            os.rmdir(cur)
            removed += 1
        except OSError:
            pass
        if check():
            break
    return removed


def drain_tombstones(*, roots: Optional[Sequence[Path]] = None, check=lambda: False) -> int:
    """Delete ``.reap`` contents one entry at a time. ``check()`` True leaves the rest for the next pass."""
    removed = 0
    for pytest_dir in _pytest_dirs(roots):
        if check():
            break
        reap = pytest_dir / _REAP_DIRNAME
        if _is_link(reap) or not reap.is_dir():
            continue
        removed += _drain_tree(str(reap), check, keep_root=True)
    return removed


def pytest_backlog(*, roots: Optional[Sequence[Path]] = None, now: Optional[float] = None) -> bool:
    """True when a pytest dir still has a ``.reap`` child or an untombstoned dead run. No walk inside."""
    now = time.time() if now is None else now
    own_pid = os.getpid()
    for pytest_dir in _pytest_dirs(roots):
        for name in _children(pytest_dir):
            path = pytest_dir / name
            if name == _REAP_DIRNAME:
                if not _is_link(path) and any(_children(path)):
                    return True
                continue
            pid = parse_pytest_run_dir(name)
            if pid is None or _is_link(path) or not path.is_dir():
                continue
            if _kept_fresh(path, now) or pid == own_pid or session_registry.pid_alive(pid):
                continue
            return True
    return False


def sweep_dead_runs(
    *,
    apply: bool = True,
    quiescence_hours: float = 1.0,
    registry_dir: Optional[Path] = None,
    now: Optional[float] = None,
) -> list[dict[str, Any]]:
    """Purge records for dead-pid run dirs.

    ``apply`` tombstones then drains with no budget (CLI contract: dirs are gone, action ``deleted``).
    ``quiescence_hours`` and ``registry_dir`` are accepted and unused: liveness is the pid, not a walk.
    """
    del quiescence_hours, registry_dir
    now = time.time() if now is None else now
    own_pid = os.getpid()
    records: list[dict[str, Any]] = []
    for repo_dir, run_dir, pid in _candidates():
        if pid == own_pid or session_registry.pid_alive(pid):
            continue
        records.append({"repo": repo_dir.name, "path": str(run_dir), "action": "would-delete"})
    if apply:
        tombstone_dead_runs(now=now, check=lambda: False)
        drain_tombstones(check=lambda: False)
        for rec in records:
            rec["action"] = "deleted" if not Path(rec["path"]).exists() else "delete-failed"
    return records


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="pytest_basetemp_sweep")
    parser.add_argument("--dry-run", action="store_true", help="classify only; delete nothing")
    args = parser.parse_args(argv)
    for rec in sweep_dead_runs(apply=not args.dry_run):
        sys.stdout.write(json.dumps(rec) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
