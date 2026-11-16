"""Read-only purge planner for ``fleet.scratch_hygiene``: classify each top-level entry of
``<repo>/scratch``, ``<repo>/state/scratch`` and ``coordinator_temp_root(repo)`` into the
contract's action set. Nothing under ``state/queue-grind`` is ever a candidate.

``scratch/warp/`` is not an entry: each run's directory and its ``.workflow.mjs`` and
``.workflow.mjs.emitted.json`` files are. An entry of an open run is ``skipped-live``
(``open-run-close-job:<run_id>``) while its close-job record holds a ``queued`` leg inside
the record's deadline or is not a valid record; with no record it is ``skipped-live``
(``open-run-no-close-job:<name>``) until quiet for 7 days.

Negative spec: never deletes, never follows a link, never spawns a process, never walks
scratch-hold, ``_fleet`` or system Temp.
"""

from __future__ import annotations

import json
import os
import re
import stat
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from coordinator_core.group_em import session_registry
from coordinator_core.install.junction import is_junction
from coordinator_core.ops.fleet.scratch_hygiene_nag import hold_pending_marker
from coordinator_core.ops.fleet.scratch_hygiene_records import purge_record
from coordinator_core.temp_layout import coordinator_temp_root

_DAY = 86400.0
_PYTEST_DIRNAME = "pytest"
_WARP_DIRNAME = "warp"
_RUN_FILE_SUFFIXES = (".workflow.mjs.emitted.json", ".workflow.mjs")
OPEN_RUN_NO_RECORD_QUIESCENCE_HOURS = 168
CURSOR_NAME = ".scratch-hygiene-cursor"
#: ``skipped-budget`` reason for an entry cut by its own allowance; it was walked, so the cursor passes it.
ENTRY_OVER_ALLOWANCE = "entry-over-allowance"


_PLANS_RELDIR = ("docs", "plans")
_PLAN_TERMINAL_STATUSES = frozenset({"implemented", "landed", "complete", "completed", "superseded", "abandoned"})
_STATUS_RE = re.compile(r"^status:[ \t]*[\"']?([A-Za-z_-]+)", re.MULTILINE)
_SCRATCH_CITE_RE = re.compile(r"scratch/(?:warp/)?[\w.\-]+")


def _plan_files(plans: Path) -> list[tuple[Path, str]]:
    """``(directory, file name)`` for ``docs/plans`` and each month subdirectory beneath it."""
    found: list[tuple[Path, str]] = []
    try:
        top = sorted(os.scandir(plans), key=lambda e: e.name)
    except OSError:
        return found
    for entry in top:
        if entry.is_dir(follow_symlinks=False):
            try:
                found.extend((Path(entry.path), n) for n in sorted(os.listdir(entry.path)))
            except OSError:
                continue
        else:
            found.append((plans, entry.name))
    return found


def _collect(text: str, name: str, cited: dict[str, str]) -> None:
    for m in _SCRATCH_CITE_RE.finditer(text):
        start = m.start()
        lead = "state/" if text.startswith("state/", start - 6) and start >= 6 else ""
        cited.setdefault((lead + m.group(0)).rstrip(".").lower(), name)


def _plan_scratch_citations(repo_root: Path) -> dict[str, str]:
    """Scratch entries (repo-relative, posix) cited by a non-terminal plan body or its evidence
    file, each mapped to one citing file's name. One in-process read of ``docs/plans`` and its
    month subdirectories, no spawn. A plan with no readable status counts as non-terminal."""
    cited: dict[str, str] = {}
    files = _plan_files(repo_root.joinpath(*_PLANS_RELDIR))
    terminal: dict[Path, bool] = {}
    for folder, name in files:
        if not name.endswith(".md"):
            continue
        try:
            with open(folder / name, encoding="utf-8", errors="replace") as fh:
                head = fh.read(2000)
                m = _STATUS_RE.search(head)
                is_terminal = m is not None and m.group(1).lower() in _PLAN_TERMINAL_STATUSES
                terminal[folder / name[:-3]] = is_terminal
                text = "" if is_terminal else head + fh.read()
        except OSError:
            continue
        _collect(text, name, cited)
    for folder, name in files:
        if not name.endswith(".evidence.yaml") or terminal.get(folder / name[: -len(".evidence.yaml")], False):
            continue
        try:
            text = (folder / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        _collect(text, name, cited)
    return cited


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
    _plan_cites: Optional[dict[str, str]] = field(default=None, repr=False)

    def plan_cites(self, repo_root: Path) -> dict[str, str]:
        if self._plan_cites is None:
            self._plan_cites = _plan_scratch_citations(repo_root)
        return self._plan_cites

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
    out_of_time: bool = False
    paths: list[str] = field(default_factory=list)


def _entry_is_junction(e: os.DirEntry) -> bool:
    probe = getattr(e, "is_junction", None)
    return probe() if probe is not None else is_junction(e.path)


def _walk(top: str, cutoff: float, deadline: Optional[float] = None) -> _Walk:
    """No-follow scandir walk; stops at the first node newer than ``cutoff``, or sets
    ``out_of_time`` at the first directory read past ``deadline``.

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
        if deadline is not None and time.monotonic() >= deadline:
            w.out_of_time = True
            return w
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

    def _busy(paths: list[str], deadline: Optional[float] = None) -> Optional[bool]:
        """True when any file refuses a share-mode-0 open: held by a process. None when
        ``deadline`` passes before every file is probed."""
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
        for i, p in enumerate(paths):
            if deadline is not None and not i % 256 and time.monotonic() >= deadline:
                return None
            full = os.path.abspath(p)
            h = k32.CreateFileW(full if full.startswith("\\\\") else "\\\\?\\" + full, generic_read, 0, None, open_existing, 0x80, None)
            if h == invalid or h is None:
                return True
            k32.CloseHandle(h)
        return False

    def _has_open_handle(entry: str, walk: _Walk, ctx: _Context, deadline: Optional[float] = None) -> Optional[bool]:
        return _busy(walk.paths, deadline)

else:

    def _has_open_handle(entry: str, walk: _Walk, ctx: _Context, deadline: Optional[float] = None) -> Optional[bool]:
        """POSIX unlink never blocks on an open file; the gate is registry-live pids holding a file under the entry."""
        prefix = _norm(entry) + os.sep
        return any(p.startswith(prefix) or p == prefix[:-1] for p in ctx.open_files())


def _open_run_state(entry: str, repo_root: Path, now: float) -> Optional[tuple[str, str]]:
    """For an entry directly under ``<repo>/scratch/warp``: ``("record", run_id)`` when its
    close-job record marks the run open, ``("none", name)`` when no record exists, and None
    for any other entry or a run whose legs all have an outcome.

    ``run_close`` is imported here, not at module top, and only after the cheap directory
    check: the dispatch_emit package pulls the ops tree into this fleet module.
    """
    if _norm(os.path.dirname(entry)) != _norm(repo_root / "scratch" / _WARP_DIRNAME):
        return None
    from coordinator_core.ops.dispatch_emit.run_close import STATUSES, close_job_path

    name = os.path.basename(entry)
    for suffix in _RUN_FILE_SUFFIXES:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    run_id = f"{name}.workflow"
    try:
        with open(repo_root / close_job_path(run_id), encoding="utf-8") as fh:
            record = json.load(fh)
    except FileNotFoundError:
        return ("none", name)
    except (OSError, ValueError):
        return ("record", run_id)
    legs = record.get("legs") if isinstance(record, dict) else None
    if not isinstance(legs, dict):
        return ("record", run_id)
    unfinished = any(
        not (isinstance(leg, dict) and leg.get("status") in STATUSES and leg.get("status") != "queued")
        for leg in legs.values()
    )
    if not unfinished:
        return None
    try:
        deadline = datetime.fromisoformat(str(record.get("deadline", "")))
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        late = datetime.fromtimestamp(now, timezone.utc) > deadline
    except (ValueError, TypeError):
        late = True
    return None if late else ("record", run_id)


def classify_entry(
    entry: str | os.PathLike[str],
    *,
    repo_root: str | os.PathLike[str],
    quiescence_hours: float = 24,
    verify_copy: Optional[Mapping[str, str]] = None,
    registry_dir: Optional[Path] = None,
    now: Optional[float] = None,
    deadline: Optional[float] = None,
    entry_allowance: Optional[float] = None,
    _ctx: Optional[_Context] = None,
) -> dict[str, Any]:
    """Gate one top-level entry and return its purge record (``would-delete`` or one ``skipped-*``).

    Usable as apply's ``recheck``: it reads current disk state on every call. ``deadline`` is a
    ``time.monotonic()`` instant: past it the entry is reported ``skipped-budget`` unwalked
    (``budget-exhausted``), since the walk and the probe both scale with the entry's file count.
    ``entry_allowance`` (seconds) caps this one entry's walk and probe: an entry that outruns it is
    ``skipped-budget`` with ``ENTRY_OVER_ALLOWANCE``, so the shared ``deadline`` is never spent by it.
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

    if deadline is not None and time.monotonic() >= deadline:
        return rec("skipped-budget", reason="budget-exhausted")

    if _is_link(entry):
        return rec("skipped-link", reason="link-not-followed")

    if hold_pending_marker(entry) is not None:
        return rec("skipped-hold-pending", reason="move-to-hold-marker")

    open_run = _open_run_state(entry, repo_root, now)
    if open_run is not None and open_run[0] == "record":
        return rec("skipped-live", reason=f"open-run-close-job:{open_run[1]}")

    here = _norm(entry)
    for row in ctx.live_rows():
        cwd = _norm(row.cwd) if row.cwd else ""
        if cwd and (cwd == here or cwd.startswith(here + os.sep)):
            return rec("skipped-live", reason=f"live-session-cwd:{row.session_id}")

    no_record = open_run is not None and open_run[0] == "none"
    hours = max(quiescence_hours, OPEN_RUN_NO_RECORD_QUIESCENCE_HOURS) if no_record else quiescence_hours
    cutoff = now - hours * 3600.0
    limit = deadline
    if entry_allowance is not None:
        own = time.monotonic() + entry_allowance
        limit = own if deadline is None else min(deadline, own)

    def cut() -> dict[str, Any]:
        shared_spent = deadline is not None and time.monotonic() >= deadline
        return rec("skipped-budget", reason="budget-exhausted" if shared_spent else ENTRY_OVER_ALLOWANCE)

    w = _walk(entry, cutoff, limit)
    if w.out_of_time:
        return cut()
    if w.young and no_record:
        return rec("skipped-live", w, reason=f"open-run-no-close-job:{open_run[1]}")
    if w.young:
        return rec("skipped-young", w, reason=f"written-within-{quiescence_hours:g}h")

    try:
        rel = Path(here).relative_to(_norm(repo_root)).as_posix()
    except ValueError:
        rel = ""
    if rel:
        citer = ctx.plan_cites(repo_root).get(rel.lower())
        if citer:
            return rec("skipped-live", w, reason=f"cited-by-open-plan:{citer}")

    if verify_copy and _norm(verify_copy.get("source", "")) == here:
        src = _tree_totals(entry)
        dst = _tree_totals(verify_copy.get("dest", ""))
        if src != dst:
            return rec("skipped-unverified", w, reason=f"copy-mismatch:files={src[0]}/{dst[0]},bytes={src[1]}/{dst[1]}")

    held = _has_open_handle(entry, w, ctx, limit)
    if held is None:
        return cut()
    if held:
        return rec("skipped-live", w, reason="open-handle")

    return rec("would-delete", w)


def _entries(root: Path) -> list[tuple[str, bool]]:
    """``(path, is_dir)`` for each child of ``root``; the type comes free with the listing."""
    try:
        with os.scandir(root) as it:
            return [(e.path, _is_real_dir(e)) for e in it]
    except OSError:
        return []


def _is_real_dir(e: os.DirEntry) -> bool:
    try:
        return e.is_dir(follow_symlinks=False) and not _entry_is_junction(e)
    except OSError:
        return False


def _candidate_roots(repo_root: Path) -> tuple[tuple[Path, Optional[str]], ...]:
    return (
        (repo_root / "scratch", _WARP_DIRNAME),
        (repo_root / "state" / "scratch", None),
        (coordinator_temp_root(repo_root), _PYTEST_DIRNAME),
    )


def _order_key(roots: list[str], path: str, is_dir: bool) -> tuple[int, int, str]:
    """Directories sort before files: a file entry is one stat, a directory is the walk that can hold the bytes."""
    norm = _norm(path)
    for i, root in enumerate(roots):
        if norm.startswith(root + os.sep):
            return (not is_dir, i, norm)
    return (not is_dir, len(roots), norm)


def candidate_scan(repo_root: str | os.PathLike[str]) -> list[tuple[str, bool]]:
    """``(path, is_dir)`` for every top-level entry in ``<repo>/scratch``, ``<repo>/state/scratch`` and
    ``coordinator_temp_root(repo)``, directories first so the shared budget reaches them before the files.
    ``pytest/`` under the Temp root and ``warp/`` under scratch are not entries: their
    per-run children are. The op's own cursor file (``CURSOR_NAME`` and its temp files) is never one."""
    repo_root = Path(repo_root)
    roots = [_norm(root) for root, _per_run in _candidate_roots(repo_root)]
    found: list[tuple[str, bool]] = []
    for root, per_run in _candidate_roots(repo_root):
        for p, is_dir in _entries(root):
            base = os.path.basename(p)
            if base.startswith(CURSOR_NAME):
                continue
            if base == per_run and is_dir:
                found.extend(_entries(Path(p)))
            else:
                found.append((p, is_dir))
    found.sort(key=lambda pd: _order_key(roots, pd[0], pd[1]))
    return found


def purge_candidates(repo_root: str | os.PathLike[str]) -> list[str]:
    """The paths of ``candidate_scan``, in its order."""
    return [p for p, _is_dir in candidate_scan(repo_root)]


def ordered_candidate_scan(
    repo_root: str | os.PathLike[str], cursor: Optional[str] = None, cursor_is_dir: Optional[bool] = None
) -> list[tuple[str, bool]]:
    """``candidate_scan`` rotated to start at the first entry ordered after ``cursor`` (a path),
    wrapping at the end. The cursor names an entry, not a position, so deletions since it was
    written do not shift the resume point. No cursor, or one ordered after every entry, starts at the top.
    ``cursor_is_dir`` is the cursor's recorded type; unrecorded, it is read from disk, and a cursor
    gone from disk counts as a directory."""
    repo_root = Path(repo_root)
    scan = candidate_scan(repo_root)
    if not cursor:
        return scan
    if cursor_is_dir is None:
        try:
            cursor_is_dir = stat.S_ISDIR(os.lstat(cursor).st_mode)
        except OSError:
            cursor_is_dir = True
    roots = [_norm(root) for root, _per_run in _candidate_roots(repo_root)]
    key = _order_key(roots, cursor, cursor_is_dir)
    start = next((i for i, (p, d) in enumerate(scan) if _order_key(roots, p, d) > key), 0)
    return scan[start:] + scan[:start]


def plan_purge(
    repo_root: str | os.PathLike[str],
    *,
    quiescence_hours: float = 24,
    verify_copy: Optional[Mapping[str, str]] = None,
    registry_dir: Optional[Path] = None,
    now: Optional[float] = None,
    deadline: Optional[float] = None,
    candidates: Optional[list[str]] = None,
    first_unbounded: bool = False,
    entry_allowance: Optional[float] = None,
) -> list[dict[str, Any]]:
    """Dry-run purge records for every candidate entry (``candidates`` when given, else
    ``purge_candidates``). Deletes nothing. A missing root yields no records.
    ``first_unbounded`` exempts the first entry from the shared ``deadline`` (``entry_allowance``
    still caps it), so a call always gates one."""
    repo_root = Path(repo_root)
    ctx = _Context(registry_dir=registry_dir)
    now = time.time() if now is None else now
    return [
        classify_entry(
            p,
            repo_root=repo_root,
            quiescence_hours=quiescence_hours,
            verify_copy=verify_copy,
            now=now,
            deadline=None if first_unbounded and i == 0 else deadline,
            entry_allowance=entry_allowance,
            _ctx=ctx,
        )
        for i, p in enumerate(purge_candidates(repo_root) if candidates is None else candidates)
    ]
