"""Read-only ``scratch-hold/`` nag for ``fleet.scratch_hygiene``: one hold-nag record per held entry."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from coordinator_core.install.junction import is_junction
from coordinator_core.ops.fleet.scratch_hygiene_records import hold_nag_record

HOLD_DIR = "scratch-hold"
SCRATCH_DIR = "scratch"
HOLD_PENDING_MARKER = "move-to-hold.md"
_README_SUFFIX = ".README"
_DIR_READMES = ("README", "README.md")


def _is_link(path: str | os.PathLike[str]) -> bool:
    return os.path.islink(path) or is_junction(path)


def _size_and_mtime(entry: Path) -> tuple[int, float]:
    """Total bytes and newest mtime under ``entry``; links are counted as themselves, never followed."""
    st = entry.lstat()
    total, newest = st.st_size, st.st_mtime
    if _is_link(entry) or not entry.is_dir():
        return total, newest
    # Not os.walk: before 3.12 its followlinks=False still descends a Windows junction.
    stack = [os.fspath(entry)]
    while stack:
        try:
            it = os.scandir(stack.pop())
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    s = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                total += s.st_size
                newest = max(newest, s.st_mtime)
                if not _is_link(e.path) and e.is_dir(follow_symlinks=False):
                    stack.append(e.path)
    return total, newest


def _first_line(path: Path) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.strip():
                    return line.strip()
    except OSError:
        return None
    return None


def _readme_line(entry: Path) -> str | None:
    if _is_link(entry):
        return None
    if entry.is_dir():
        candidates = [entry / n for n in _DIR_READMES]
    else:
        candidates = [entry.with_name(entry.name + _README_SUFFIX)]
    for cand in candidates:
        if cand.is_file() and not cand.is_symlink():
            return _first_line(cand)
    return None


def hold_pending_marker(entry: str | os.PathLike[str]) -> Path | None:
    """The ``MOVE-TO-HOLD.md`` (any case) file directly inside directory ``entry``, else None."""
    if _is_link(entry) or not os.path.isdir(entry):
        return None
    try:
        names = os.listdir(entry)
    except OSError:
        return None
    for name in names:
        if name.lower() == HOLD_PENDING_MARKER:
            cand = Path(entry) / name
            if cand.is_file() and not _is_link(cand):
                return cand
    return None


def hold_pending_nag(repo_root: Path | str, *, now: float | None = None) -> list[dict[str, Any]]:
    """Hold-nag records for ``scratch/`` entries parked by a ``MOVE-TO-HOLD.md`` marker."""
    root = Path(repo_root)
    scratch = root / SCRATCH_DIR
    if not scratch.is_dir() or _is_link(scratch):
        return []
    stamp = time.time() if now is None else now
    records: list[dict[str, Any]] = []
    for entry in sorted(scratch.iterdir(), key=lambda p: p.name):
        marker = hold_pending_marker(entry)
        if marker is None:
            continue
        size, newest = _size_and_mtime(entry)
        records.append(
            hold_nag_record(
                root.name,
                entry,
                bytes=size,
                age_days=round(max(0.0, stamp - newest) / 86400, 2),
                readme=_first_line(marker),
                finding="hold-pending",
                repo_root=root,
            )
        )
    return records


def hold_nag(repo_root: Path | str, *, now: float | None = None) -> list[dict[str, Any]]:
    """Hold-nag records for every entry directly under ``<repo_root>/scratch-hold/``; touches nothing."""
    root = Path(repo_root)
    hold = root / HOLD_DIR
    if not hold.is_dir() or hold.is_symlink():
        return []
    stamp = time.time() if now is None else now
    records: list[dict[str, Any]] = []
    for entry in sorted(hold.iterdir(), key=lambda p: p.name):
        if entry.name.endswith(_README_SUFFIX) and entry.is_file():
            continue
        size, newest = _size_and_mtime(entry)
        line = _readme_line(entry)
        records.append(
            hold_nag_record(
                root.name,
                entry,
                bytes=size,
                age_days=round(max(0.0, stamp - newest) / 86400, 2),
                readme=line,
                finding="held" if line else "missing-readme",
                repo_root=root,
            )
        )
    return records
