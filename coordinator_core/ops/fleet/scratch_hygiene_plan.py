"""Read-only purge planner for ``fleet.scratch_hygiene``: classify each top-level entry of
``<repo>/scratch`` and ``coordinator_temp_root(repo)`` into the contract's action set.

Negative spec: never deletes, never follows a link, never spawns a process, never walks
scratch-hold, ``_fleet`` or system Temp.
"""

from __future__ import annotations

import os
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from coordinator_core.group_em import session_registry
from coordinator_core.install.junction import is_junction
from coordinator_core.ops.fleet.scratch_hygiene_nag import hold_pending_marker
from coordinator_core.ops.fleet.scratch_hygiene_records import purge_record
from coordinator_core.temp_layout import coordinator_temp_root

_DAY = 86400.0
_PYTEST_DIRNAME = "pytest"


def _norm(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(path))


def _is_link(path: str | os.PathLike[str]) -> bool:
    return os.path.islink(path) or is_junction(path)


@dataclass
class _Context:
    """Per-plan caches: the registry is read once, the POSIX open-file set is built at most once."""

    registry_dir: Optional[Path] = None
    _live: Optional[list[session_registry.RegistryRow]] = field(default=None, repr=False)
    _open_files: Optional[set[str]] = field(default=None, repr=False)

    def live_rows(self) -> list[session_registry.RegistryRow]:
        if self._live is None:
            self._live = [
                r for r in session_registry.read_rows(self.registry_dir) if session_registry.pid_alive(r.pid)
            ]
        return self._live

    def open_files(self) -> set[str]:
        if self._open_files is None:
            found: set[str] = set()
            try:
                import psutil
            except ImportError:
                psutil = None
            if psutil is not None:
                for row in self.live_rows():
                    try:
                        for of in psutil.Process(row.pid).open_files():
                            found.add(_norm(of.path))
                    except (psutil.Error, OSError):
                        continue
            self._open_files = found
        return self._open_files


@dataclass
class _Walk:
    files: int = 0
    bytes: int = 0
    newest: float = 0.0
    young: bool = False
    paths: list[str] = field(default_factory=list)


def _entry_is_junction(e: os.DirEntry) -> bool:
    probe = getattr(e, "is_junction", None)
    return probe() if probe is not None else is_junction(e.path)


def _walk(top: str, cutoff: float) -> _Walk:
    """No-follow scandir walk; stops at the first node newer than ``cutoff``.

    A nested link is skipped entirely: its own mtime is not a write into the entry.
    """
    w = _Walk()
    try:
        st = os.lstat(top)
    except OSError:
        return w
    w.newest = st.st_mtime
    if st.st_mtime > cutoff:
        w.young = True
        return w
    if not stat.S_ISDIR(st.st_mode):
        w.files, w.bytes, w.paths = 1, st.st_size, [top]
        return w
    stack = [top]
    while stack:
        try:
            it = os.scandir(stack.pop())
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    if e.is_symlink() or _entry_is_junction(e):
                        continue
                    s = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                if s.st_mtime > w.newest:
                    w.newest = s.st_mtime
                if s.st_mtime > cutoff:
                    w.young = True
                    return w
                if e.is_dir(follow_symlinks=False):
                    stack.append(e.path)
                    continue
                w.files += 1
                w.bytes += s.st_size
                w.paths.append(e.path)
    return w


def _tree_totals(path: str) -> tuple[int, int]:
    w = _walk(path, float("inf"))
    return w.files, w.bytes


if os.name == "nt":

    def _busy(paths: list[str]) -> bool:
        """True when any file refuses a share-mode-0 open: held by a process."""
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        invalid = wintypes.HANDLE(-1).value
        generic_read, open_existing = 0x80000000, 3
        for p in paths:
            full = os.path.abspath(p)
            h = k32.CreateFileW(full if full.startswith("\\\\") else "\\\\?\\" + full, generic_read, 0, None, open_existing, 0x80, None)
            if h == invalid or h is None:
                return True
            k32.CloseHandle(h)
        return False

    def _has_open_handle(entry: str, walk: _Walk, ctx: _Context) -> bool:
        return _busy(walk.paths)

else:

    def _has_open_handle(entry: str, walk: _Walk, ctx: _Context) -> bool:
        """POSIX unlink never blocks on an open file; the gate is registry-live pids holding a file under the entry."""
        prefix = _norm(entry) + os.sep
        return any(p.startswith(prefix) or p == prefix[:-1] for p in ctx.open_files())


def classify_entry(
    entry: str | os.PathLike[str],
    *,
    repo_root: str | os.PathLike[str],
    quiescence_hours: float = 24,
    verify_copy: Optional[Mapping[str, str]] = None,
    registry_dir: Optional[Path] = None,
    now: Optional[float] = None,
    _ctx: Optional[_Context] = None,
) -> dict[str, Any]:
    """Gate one top-level entry and return its purge record (``would-delete`` or one ``skipped-*``).

    Usable as apply's ``recheck``: it reads current disk state on every call.
    """
    entry = os.fspath(entry)
    repo_root = Path(repo_root)
    repo = repo_root.name
    ctx = _ctx or _Context(registry_dir=registry_dir)
    now = time.time() if now is None else now

    def rec(action: str, w: Optional[_Walk] = None, reason: str = "") -> dict[str, Any]:
        newest = w.newest if w is not None and w.newest else now
        return purge_record(
            repo, entry, action,
            bytes=w.bytes if w else 0,
            files=w.files if w else 0,
            age_days=round(max(now - newest, 0.0) / _DAY, 2),
            reason=reason,
            repo_root=repo_root,
        )

    if _is_link(entry):
        return rec("skipped-link", reason="link-not-followed")

    if hold_pending_marker(entry) is not None:
        return rec("skipped-hold-pending", reason="move-to-hold-marker")

    here = _norm(entry)
    for row in ctx.live_rows():
        cwd = _norm(row.cwd) if row.cwd else ""
        if cwd and (cwd == here or cwd.startswith(here + os.sep)):
            return rec("skipped-live", reason=f"live-session-cwd:{row.session_id}")

    cutoff = now - quiescence_hours * 3600.0
    w = _walk(entry, cutoff)
    if w.young:
        return rec("skipped-young", w, reason=f"written-within-{quiescence_hours:g}h")

    if verify_copy and _norm(verify_copy.get("source", "")) == here:
        src = _tree_totals(entry)
        dst = _tree_totals(verify_copy.get("dest", ""))
        if src != dst:
            return rec("skipped-unverified", w, reason=f"copy-mismatch:files={src[0]}/{dst[0]},bytes={src[1]}/{dst[1]}")

    if _has_open_handle(entry, w, ctx):
        return rec("skipped-live", w, reason="open-handle")

    return rec("would-delete", w)


def _entries(root: Path) -> list[str]:
    try:
        return sorted(os.path.join(root, n) for n in os.listdir(root))
    except OSError:
        return []


def plan_purge(
    repo_root: str | os.PathLike[str],
    *,
    quiescence_hours: float = 24,
    verify_copy: Optional[Mapping[str, str]] = None,
    registry_dir: Optional[Path] = None,
    now: Optional[float] = None,
) -> list[dict[str, Any]]:
    """Purge records for every top-level entry in ``<repo>/scratch`` and ``coordinator_temp_root(repo)``.

    Deletes nothing. A missing root yields no records. ``pytest/`` under the Temp root is not an
    entry: its per-run children are.
    """
    repo_root = Path(repo_root)
    ctx = _Context(registry_dir=registry_dir)
    now = time.time() if now is None else now
    temp_root = coordinator_temp_root(repo_root)
    paths = _entries(repo_root / "scratch")
    for p in _entries(temp_root):
        if os.path.basename(p) == _PYTEST_DIRNAME and not _is_link(p) and os.path.isdir(p):
            paths.extend(_entries(Path(p)))
        else:
            paths.append(p)
    return [
        classify_entry(
            p,
            repo_root=repo_root,
            quiescence_hours=quiescence_hours,
            verify_copy=verify_copy,
            now=now,
            _ctx=ctx,
        )
        for p in paths
    ]
