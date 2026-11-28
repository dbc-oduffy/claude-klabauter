"""Throttled sweeper for stale pytest basetemp dirs, run by the warm engine supervisor.

Reclaims ``<tempdir>/pytest-of-<user>/pytest-*`` dirs older than ``MAX_AGE_SECS`` whose
``.lock`` pid is dead. Only the top level of that one dir is listed; deletion proceeds in
batches of top-level children with a pause between batches; one child is removed depth-first
with no internal pause, so the throttle spaces children, not files.
Every failure is logged to stderr and swallowed: the sweeper never raises into its caller.

``sweep_coordinator_roots`` bounds ``<temp>/coordinator`` itself on the same tick: clone-keyed
pytest basetemps, run-temp dead owners, clean mirrors, and the ``_fleet`` publish-engine exports,
caches and session files. It is time-budgeted per tick and runs off the hook hot path.
"""
from __future__ import annotations

import getpass
import os
import re
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Optional, Sequence

from coordinator_core.temp_layout import (
    COORDINATOR_ROOT_WRITERS,
    FLEET_DIRNAME,
    OWNER_SIGNAL_LEASE,
    OWNER_SIGNAL_PID,
    PYTEST_KEPT_MARKER,
    RUN_TEMP_DIRNAME,
    SCOPE_FLEET,
    SCOPE_REPO,
    RootWriter,
    _commit_clone_leased,
    _reap_dead_owner_dirs,
    parse_pytest_run_dir,
    pytest_sweep_roots,
)

MAX_AGE_SECS = 6 * 3600
INTERVAL_SECS = 3600
BATCH_SIZE = 8
BATCH_PAUSE_SECS = 0.25

TICK_BUDGET_SECS = 300.0
UNPARSED_RUN_MAX_AGE_SECS = 86400
SURPLUS_MIN_AGE_SECS = 2 * 3600
RUN_TEMP_REAP_CAP = 64
KEY_COORDINATOR_ROOT_GIB = "temp_budget.coordinator_root_gib"
DEFAULT_COORDINATOR_ROOT_GIB = 10
GIB = 1 << 30

FLEET_SESSION_FILE_PREFIXES = next(
    w.prefixes for w in COORDINATOR_ROOT_WRITERS if w.name == "fleet-session-files"
)
FLEET_CACHE_DIR_PREFIXES = next(w.prefixes for w in COORDINATOR_ROOT_WRITERS if w.name == "fleet-caches")
_BASETEMP_RE = re.compile(r"^pytest-\d+$")


def coordinator_root_budget_bytes(get=None) -> int:
    """Size ceiling for ``<temp>/coordinator`` from machine-local `temp_budget.coordinator_root_gib`."""
    from coordinator_core.ops import claude_home_census as census

    raw = (get or census._registry_get)(KEY_COORDINATOR_ROOT_GIB)
    return int(census._positive_number(raw, DEFAULT_COORDINATOR_ROOT_GIB) * GIB)


def _tree_bytes(path: Path, expired: Callable[[], bool]) -> Optional[int]:
    """File bytes under ``path`` (lstat, no symlink follow); None when ``expired`` cut the walk short."""
    total = 0
    stack = [path]
    while stack:
        if expired():
            return None
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(Path(e.path))
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except NotADirectoryError:
            try:
                total += path.lstat().st_size
            except OSError:
                pass
        except OSError:
            continue
    return total


def _log(msg: str) -> None:
    print(f"[pytest-basetemp-sweep] {msg}", file=sys.stderr)


def _default_pid_alive(pid: int) -> bool:
    from coordinator_core.session.core import pid_alive

    return pid_alive(pid)


def _basetemp_root(temp_root: Optional[str]) -> Path:
    base = Path(temp_root) if temp_root else Path(tempfile.gettempdir())
    return base / f"pytest-of-{getpass.getuser()}"


def _lock_pid(basetemp: Path) -> Optional[int]:
    """Pid recorded in ``<basetemp>/.lock``; None when the file is absent or unparseable."""
    try:
        return int((basetemp / ".lock").read_text(encoding="utf-8", errors="replace").strip())
    except (OSError, ValueError):
        return None


def _is_link_or_junction(path: Path) -> bool:
    """invariant: a Windows junction reports is_dir(follow_symlinks=False) True, so it must never be recursed."""
    isjunction = getattr(os.path, "isjunction", None)
    return os.path.islink(path) or bool(isjunction and isjunction(path))


def _rmtree_bounded(path: Path, expired: Callable[[], bool]) -> bool:
    """Depth-first delete that checks ``expired`` between entries; True when ``path`` is gone."""
    with os.scandir(path) as it:
        entries = [(Path(e.path), e.is_dir(follow_symlinks=False)) for e in it]
    for child, is_dir in entries:
        if expired():
            return False
        if _is_link_or_junction(child):
            try:
                os.unlink(child)
            except OSError:
                os.rmdir(child)
        elif is_dir:
            if not _rmtree_bounded(child, expired):
                return False
        else:
            try:
                child.unlink()
            except PermissionError:
                os.chmod(child, stat.S_IWRITE)
                child.unlink()
    if expired():
        return False
    try:
        path.rmdir()
    except PermissionError:
        os.chmod(path, stat.S_IWRITE)
        path.rmdir()
    return True


def _restore_mtime(path: Path, original) -> None:
    """Deleting children bumps the parent's mtime; an unfinished reap keeps its age so the next tick resumes it."""
    if original is None:
        return
    try:
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    except OSError:
        pass


def _remove_in_batches(
    basetemp: Path,
    *,
    batch_size: int,
    pause_secs: float,
    sleep: Callable[[float], None],
    expired: Callable[[], bool] = lambda: False,
) -> None:
    """Delete ``basetemp`` one top-level child at a time, pausing after each batch.

    Stops between children once ``expired()``; the emptied remainder is left for the next tick.
    """
    try:
        original = basetemp.lstat()
    except OSError:
        original = None
    with os.scandir(basetemp) as it:
        children = [Path(e.path) for e in it]
    for i, child in enumerate(children, 1):
        if expired():
            _restore_mtime(basetemp, original)
            return
        if child.is_dir() and not child.is_symlink():
            if not _rmtree_bounded(child, expired):
                _restore_mtime(basetemp, original)
                return
        else:
            child.unlink()
        if i % batch_size == 0:
            sleep(pause_secs)
    if expired():
        _restore_mtime(basetemp, original)
    else:
        basetemp.rmdir()


def sweep_once(
    *,
    temp_root: Optional[str] = None,
    now: Optional[float] = None,
    max_age_secs: float = MAX_AGE_SECS,
    pid_alive: Optional[Callable[[int], bool]] = None,
    sleep: Callable[[float], None] = time.sleep,
    batch_size: int = BATCH_SIZE,
    pause_secs: float = BATCH_PAUSE_SECS,
) -> dict:
    """One sweep; returns ``{"removed": [...], "kept": int, "failed": [...]}``. Never raises."""
    report: dict = {"removed": [], "kept": 0, "failed": []}
    try:
        root = _basetemp_root(temp_root)
        clock = time.time() if now is None else now
        alive = pid_alive or _default_pid_alive
        try:
            with os.scandir(root) as it:
                entries = [e for e in it if _BASETEMP_RE.match(e.name)]
        except OSError:
            return report
        for entry in entries:
            try:
                if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    continue
                if clock - entry.stat(follow_symlinks=False).st_mtime < max_age_secs:
                    report["kept"] += 1
                    continue
                path = Path(entry.path)
                pid = _lock_pid(path)
                if pid is not None and alive(pid):
                    report["kept"] += 1
                    continue
                _remove_in_batches(path, batch_size=batch_size, pause_secs=pause_secs, sleep=sleep)
                report["removed"].append(entry.name)
                sleep(pause_secs)
            except Exception as exc:  # noqa: BLE001 -- one bad dir must not stop the sweep
                report["failed"].append(entry.name)
                _log(f"failed to remove {entry.name}: {exc!r}")
    except Exception as exc:  # noqa: BLE001 -- never raise into the supervisor
        _log(f"sweep aborted: {exc!r}")
    return report


class _Reaper:
    """Accumulates one coordinator-root sweep: removals, failures, and the per-tick time budget."""

    def __init__(self, *, now, alive, sleep, batch_size, pause_secs, budget_secs, size_budget_bytes) -> None:
        self.size_budget_bytes = size_budget_bytes
        self.survivors: list[tuple[float, Path, int, bool]] = []
        self.size_known = True
        self.now = now
        self.alive = alive
        self.sleep = sleep
        self.batch_size = batch_size
        self.pause_secs = pause_secs
        self.deadline = time.monotonic() + budget_secs
        self.report: dict = {"removed": [], "failed": []}

    def expired(self) -> bool:
        return time.monotonic() >= self.deadline

    def age(self, path: Path) -> Optional[float]:
        try:
            return self.now - path.lstat().st_mtime
        except OSError:
            return None

    def remove(self, path: Path) -> None:
        """Delete one entry (file, link, or tree in throttled batches); a failure is recorded, never raised."""
        if self.expired():
            return
        try:
            if path.is_dir() and not path.is_symlink():
                _remove_in_batches(
                    path, batch_size=self.batch_size, pause_secs=self.pause_secs, sleep=self.sleep,
                    expired=self.expired,
                )
            else:
                path.unlink()
            if not path.exists() and not path.is_symlink():
                self.report["removed"].append(str(path))
            self.sleep(self.pause_secs)
        except Exception as exc:  # noqa: BLE001 -- one bad entry must not stop the sweep
            self.report["failed"].append(str(path))
            _log(f"failed to remove {path}: {exc!r}")

    def children(self, parent: Path, *, dirs_only: bool = False) -> list[Path]:
        try:
            with os.scandir(parent) as it:
                return [
                    Path(e.path)
                    for e in it
                    if not dirs_only or (e.is_dir(follow_symlinks=False))
                ]
        except OSError:
            return []

    def note(self, path: Path, age: float, *, evictable: bool) -> None:
        """Record a survivor's size once, during the age pass; ``evictable`` marks dead-owned entries."""
        if self.size_budget_bytes is None or not self.size_known:
            return
        size = _tree_bytes(path, self.expired)
        if size is None:
            self.size_known = False
            return
        self.survivors.append((age, path, size, evictable))

    def reap_ranked(self, parent: Path, w: RootWriter) -> None:
        """Age bound, keep-latest surplus (past the grace) and survivor sizing for one writer's entries."""
        dated = []
        for child in self.children(parent):
            if not w.matches(child.name):
                continue
            age = self.age(child)
            if age is not None:
                dated.append((age, child))
        dated.sort(key=lambda t: t[0])
        lease = _commit_clone_leased if w.owner == OWNER_SIGNAL_LEASE else None
        for rank, (age, child) in enumerate(dated):
            if lease is not None and lease(child):
                self.note(child, age, evictable=False)
                continue
            surplus = w.keep_latest is not None and rank >= w.keep_latest and age >= SURPLUS_MIN_AGE_SECS
            if age >= w.max_age_s or surplus:
                self.remove(child)
            else:
                self.note(child, age, evictable=True)

    def reap_pytest_runs(self, pytest_dir: Path, w: RootWriter) -> None:
        for run in self.children(pytest_dir, dirs_only=True):
            if run.is_symlink():
                continue
            age = self.age(run)
            if age is None:
                continue
            evictable = True
            if (run / PYTEST_KEPT_MARKER).exists():
                limit = w.kept_max_age_s or UNPARSED_RUN_MAX_AGE_SECS
                evictable = False
            else:
                pid = parse_pytest_run_dir(run.name)
                if pid is None:
                    limit = UNPARSED_RUN_MAX_AGE_SECS
                elif pid == os.getpid() or self.alive(pid):
                    self.note(run, age, evictable=False)
                    continue
                else:
                    limit = w.max_age_s
            if age >= limit:
                self.remove(run)
            else:
                self.note(run, age, evictable=evictable)

    def reap_run_temp(self, run_temp: Path) -> None:
        _reap_dead_owner_dirs(run_temp, cap=RUN_TEMP_REAP_CAP)
        for run in self.children(run_temp, dirs_only=True):
            age = self.age(run)
            if age is not None:
                self.note(run, age, evictable=False)

    def sweep_writer(self, owner_dir: Path, w: RootWriter) -> None:
        directory = w.directory(owner_dir)
        if w.owner == OWNER_SIGNAL_PID and w.container == (RUN_TEMP_DIRNAME,):
            self.reap_run_temp(directory)
        elif w.owner == OWNER_SIGNAL_PID:
            self.reap_pytest_runs(directory, w)
        else:
            self.reap_ranked(directory, w)

    def enforce_size_budget(self) -> None:
        """Evict dead-owned survivors oldest-first until the recorded total fits the budget."""
        if self.size_budget_bytes is None or not self.size_known:
            return
        total = sum(size for _, _, size, _ in self.survivors)
        for _age, path, size, evictable in sorted(self.survivors, key=lambda t: -t[0]):
            if total <= self.size_budget_bytes:
                break
            if evictable and not self.expired():
                self.remove(path)
                if not path.exists():
                    total -= size

    def sweep_root(self, root: Path) -> None:
        fleet_containers = {w.container for w in COORDINATOR_ROOT_WRITERS if w.scope == SCOPE_FLEET and w.container}
        for repo in self.children(root, dirs_only=True):
            if repo.is_symlink():
                continue
            is_fleet = repo.name == FLEET_DIRNAME
            for w in COORDINATOR_ROOT_WRITERS:
                if w.scope == SCOPE_FLEET and not is_fleet:
                    continue
                if w.scope == SCOPE_REPO and is_fleet and w.container in fleet_containers:
                    continue
                self.sweep_writer(repo, w)


def sweep_coordinator_roots(
    *,
    roots: Optional[Sequence[Path]] = None,
    temp_root: Optional[str] = None,
    now: Optional[float] = None,
    pid_alive: Optional[Callable[[int], bool]] = None,
    sleep: Callable[[float], None] = time.sleep,
    batch_size: int = BATCH_SIZE,
    pause_secs: float = BATCH_PAUSE_SECS,
    budget_secs: float = TICK_BUDGET_SECS,
    size_budget_bytes: Optional[int] = None,
    **_ignored,
) -> dict:
    """Reap every ``COORDINATOR_ROOT_WRITERS`` output in ``<temp>/coordinator``; returns ``{"removed": [...], "failed": [...]}``.

    ``temp_root`` pins the one root ``<temp_root>/coordinator``; otherwise ``roots`` or
    ``temp_layout.pytest_sweep_roots()``. Bounds each writer by age and keep-latest, then evicts dead-owned
    survivors oldest-first past ``size_budget_bytes`` (default: settings-home `temp_budget.coordinator_root_gib`).
    Stops at ``budget_secs`` and resumes next tick. Never raises.
    """
    try:
        budget_bytes = coordinator_root_budget_bytes() if size_budget_bytes is None else size_budget_bytes
    except Exception as exc:  # noqa: BLE001 -- an unreadable registry leaves the size bound off, not the sweep
        _log(f"size budget unreadable: {exc!r}")
        budget_bytes = None
    reaper = _Reaper(
        now=time.time() if now is None else now,
        alive=pid_alive or _default_pid_alive,
        sleep=sleep,
        batch_size=batch_size,
        pause_secs=pause_secs,
        budget_secs=budget_secs,
        size_budget_bytes=budget_bytes,
    )
    try:
        if roots is None:
            roots = [Path(temp_root) / "coordinator"] if temp_root else pytest_sweep_roots()
        for root in roots:
            reaper.sweep_root(root)
        reaper.enforce_size_budget()
    except Exception as exc:  # noqa: BLE001 -- never raise into the supervisor
        _log(f"coordinator-root sweep aborted: {exc!r}")
    return reaper.report


def sweep_loop(
    *,
    stop: Optional[threading.Event] = None,
    interval_secs: float = INTERVAL_SECS,
    **sweep_kwargs,
) -> None:
    """Sweep once now, then every ``interval_secs`` until ``stop`` is set."""
    stop = stop or threading.Event()
    while not stop.is_set():
        report = sweep_once(**sweep_kwargs)
        if report["removed"] or report["failed"]:
            _log(f"removed {len(report['removed'])}, kept {report['kept']}, failed {len(report['failed'])}")
        coord = sweep_coordinator_roots(**sweep_kwargs)
        if coord["removed"] or coord["failed"]:
            _log(f"coordinator root: removed {len(coord['removed'])}, failed {len(coord['failed'])}")
        stop.wait(interval_secs)


def start_sweeper() -> threading.Thread:
    """Start the sweep loop on a daemon thread and return it."""
    t = threading.Thread(target=sweep_loop, daemon=True, name="warm-pytest-basetemp-sweep")
    t.start()
    return t
