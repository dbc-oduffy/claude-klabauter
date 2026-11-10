"""coordinator_core.ops.dispatch_emit.stage_dirt_snapshot -- when the stage happened, on the file clock.

``write_snapshot`` records beside the run's manifest one number: ``marker_ns``, the filesystem's own
mtime clock at stage. It reads neither the index nor the tree (O(1); enumerating a 74k-entry index
costs ~100ms, so stage-time enumeration cannot meet the 50ms bar). The review-time scan parses the
index (~95ms) and lists each product directory once with ``scandir`` (~30ms for ~420 directories,
stat data riding the listing on Windows) instead of one ``stat`` per file (~230ms): ~140ms process
time on this repo when measured, down from ~310ms (rerun benchmarks/review_undeclared_edits_scan.py).
``undeclared_edits`` is the later question review prep asks: which tracked paths are dirty now, were
modified at or after the marker, and whose bytes are not the indexed blob, that no declared row
covers and no live peer holds. A path last modified before the marker is pre-stage dirt and stays
out; bytes are read only for a path modified at or after the marker, and the byte compare against the
indexed blob is the final word (a touch of a clean file is never an edit).

Invariants: paths are repo-relative POSIX; the snapshot is write-once per run record (a refire keeps
the first stage's marker); every failure reads as "no snapshot" and adds nothing to review. Traps:
``undeclared_edits`` lists no untracked file, while ``run_edits`` finds untracked-new files only in
index-ancestor directories modified since the marker (a file overwritten in place under an unmodified
directory, or an untracked file older than the marker, is not seen); a deletion has no mtime, so it is
dated by its nearest existing ancestor directory's mtime, and a pre-stage deletion in a directory
touched since reads as after-stage; dirt written within one filesystem clock tick before stage reads
as after-stage (the peer-hold filter and the byte compare bound the false positives).
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Sequence

SNAPSHOT_FILE = "stage-dirt.json"
_SCHEMA = 3
_NON_PRODUCT_PREFIXES = ("scratch/", "state/", "archive/", "tasks/", "cross-repo/", ".coordinator-local/", ".git/")


def _blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _stat_dirty(root: Path):
    """``({path: os.stat_result | None if deleted}, entries)`` for every index entry whose worktree
    stat differs from the index; ``None`` when the index cannot be parsed. No byte is read."""
    from coordinator_core.git import git_index

    try:
        entries = git_index.parse_index_identity(root)
    except (git_index.IndexParseError, OSError):
        return None
    root = Path(root)
    by_dir: Dict[str, list] = {}
    for rel in entries:
        if not rel.startswith(_NON_PRODUCT_PREFIXES):
            parent, _, name = rel.rpartition("/")
            by_dir.setdefault(parent, []).append((rel, name))
    out: Dict[str, Optional[os.stat_result]] = {}
    for parent, members in by_dir.items():
        listed = _listing(root, parent)
        for rel, name in members:
            hit = listed.get(name)
            try:
                st = hit.stat() if hit is not None else os.stat(root / rel)
            except OSError:
                out[rel] = None
                continue
            entry = entries[rel]
            if entry.size == (st.st_size & 0xFFFFFFFF) and entry.mtime == int(st.st_mtime):
                if not entry.mtime_nsec or entry.mtime_nsec == st.st_mtime_ns % 1_000_000_000:
                    continue
            out[rel] = st
    return out, entries


def _listing(root: Path, parent: str) -> Dict[str, os.DirEntry]:
    """One ``scandir`` of ``root/parent`` as ``{name: DirEntry}``; empty when it cannot be listed.
    On Windows a DirEntry's stat comes with the listing, so a directory costs one syscall, not one
    per file. A name the listing lacks is re-stat'ed by the caller (case-insensitive filesystems)."""
    try:
        with os.scandir(root / parent if parent else root) as it:
            return {e.name: e for e in it}
    except OSError:
        return {}


def _matches_index(path: Path, sha: str) -> bool:
    """True when the file's bytes are the indexed blob (LF-normalised CRLF counts)."""
    try:
        data = path.read_bytes()
    except OSError:
        return False
    return _blob_sha(data) == sha or (b"\r\n" in data and _blob_sha(data.replace(b"\r\n", b"\n")) == sha)


def _body(marker_ns: Optional[int]) -> bytes:
    return json.dumps(
        {
            "schema": _SCHEMA,
            "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "marker_ns": marker_ns,
        },
        indent=1,
        sort_keys=True,
    ).encode("utf-8")


def write_snapshot(root: Path, record: Path) -> Optional[Path]:
    """Write ``<record>/stage-dirt.json`` unless one exists; the path written, or ``None`` when
    present or unwritable. Never raises. ``root`` is unused: the marker is the record file's own
    mtime, read back after a first write so it is on the clock the tree's files carry."""
    target = Path(record) / SNAPSHOT_FILE
    if target.exists():
        return None
    try:
        from coordinator_core.atomic_replace import atomic_write_bytes
        from coordinator_core.session.declared_writes import declare_write

        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(target, _body(None))
        marker = target.stat().st_mtime_ns
        atomic_write_bytes(target, _body(marker))
        declare_write(target)
    except OSError:
        return None
    return target


def read_snapshot(record: Path) -> Optional[int]:
    """The recorded ``marker_ns``, or ``None`` when absent or malformed."""
    try:
        data = json.loads((Path(record) / SNAPSHOT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != _SCHEMA:
        return None
    marker = data.get("marker_ns")
    return marker if isinstance(marker, int) and not isinstance(marker, bool) else None


def _covered(path: str, declared: Sequence[str]) -> bool:
    for d in declared:
        d = d.replace("\\", "/").rstrip("/")
        if d and (path == d or path.startswith(d + "/")):
            return True
    return False


def _deleted_after(root: Path, rel: str, marker: int) -> bool:
    """True when the nearest existing ancestor directory of ``rel`` changed at or after ``marker``."""
    parent = (root / rel).parent
    while True:
        try:
            return os.stat(parent).st_mtime_ns >= marker
        except OSError:
            if parent == root or parent == parent.parent:
                return False
            parent = parent.parent


def _tracked_edits(root: Path, marker: int, declared: Sequence[str]):
    """``(sorted tracked edited paths, index entries)`` or ``None`` when the index cannot be parsed."""
    scanned = _stat_dirty(root)
    if scanned is None:
        return None
    now, entries = scanned
    root = Path(root)
    out = []
    for p, st in now.items():
        if _covered(p, declared):
            continue
        if st is None:
            if _deleted_after(root, p, marker):
                out.append(p)
            continue
        if st.st_mtime_ns < marker or _matches_index(root / p, entries[p].sha):
            continue
        out.append(p)
    return sorted(out), entries


def undeclared_edits(root: Path, record: Path, declared: Sequence[str]) -> list:
    """Sorted paths dirty now, modified at or after the stage marker, whose bytes are not the indexed
    blob, that no ``declared`` entry names or contains. Empty when there is no snapshot or no index.
    Peer holds are the caller's filter."""
    marker = read_snapshot(record)
    if marker is None:
        return []
    scanned = _tracked_edits(root, marker, declared)
    return [] if scanned is None else scanned[0]


_PRUNED_DIRS = frozenset({"__pycache__", ".pytest_cache", ".git"})


def _skipped(rel: str) -> bool:
    """True for a path in a non-product prefix or under a pruned directory."""
    return (rel + "/").startswith(_NON_PRODUCT_PREFIXES) or any(part in _PRUNED_DIRS for part in rel.split("/"))


def _untracked_new(root: Path, marker: int, entries, record_rel: Optional[str]) -> list:
    """Files absent from the index with mtime at or after ``marker``, found only in index-ancestor
    directories whose own mtime is at or after it, plus the full contents of directories new since it."""
    ancestors = {""}
    for rel in entries:
        if rel.startswith(_NON_PRODUCT_PREFIXES):
            continue
        parts = rel.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            ancestors.add("/".join(parts[:i]))
    out = []

    def _new_tree(rel_dir: str) -> None:
        for name, entry in _listing(root, rel_dir).items():
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if _skipped(rel) or (record_rel and (rel + "/").startswith(record_rel + "/")):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.stat(follow_symlinks=False).st_mtime_ns >= marker:
                        _new_tree(rel)
                elif rel not in entries and entry.stat(follow_symlinks=False).st_mtime_ns >= marker:
                    out.append(rel)
            except OSError:
                continue

    for rel_dir in ancestors:
        try:
            if os.stat(root / rel_dir if rel_dir else root).st_mtime_ns < marker:
                continue
        except OSError:
            continue
        for name, entry in _listing(root, rel_dir).items():
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if _skipped(rel) or (record_rel and (rel + "/").startswith(record_rel + "/")):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    # An indexed-ancestor directory is examined in its own right; any other is new or untracked-only.
                    if rel not in ancestors and entry.stat(follow_symlinks=False).st_mtime_ns >= marker:
                        _new_tree(rel)
                elif rel not in entries and entry.stat(follow_symlinks=False).st_mtime_ns >= marker:
                    out.append(rel)
            except OSError:
                continue
    return out


def run_edits(root: Path, record: Path, declared: Sequence[str] = ()) -> list:
    """Sorted repo-relative paths the run edited since the stage marker: tracked paths changed from
    the indexed blob (deletions included) and untracked-new files, with no ``declared`` exclusion
    (``declared`` is accepted for call-site symmetry with ``undeclared_edits`` and ignored). Skips
    non-product prefixes, the record dir and anything under ``__pycache__``/``.pytest_cache``/``.git``.
    Empty when there is no snapshot or no index. No git spawn; ignore filtering and peer holds are
    the caller's."""
    marker = read_snapshot(record)
    if marker is None:
        return []
    scanned = _tracked_edits(root, marker, ())
    if scanned is None:
        return []
    tracked, entries = scanned
    root = Path(root)
    record_rel = None
    try:
        record_rel = Path(record).resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        pass
    found = {p for p in tracked if not _skipped(p)}
    found.update(_untracked_new(root, marker, entries, record_rel))
    return sorted(found)
