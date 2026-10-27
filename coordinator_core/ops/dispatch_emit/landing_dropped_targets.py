"""Dropped-target classification for the landing gate: which retest targets the run's own diff
deleted or renamed. Imports only ``landing_retest``."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Mapping, Optional

from coordinator_core.ops.dispatch_emit.landing_retest import _retest_record, run_base_for


def run_deleted_dropped_targets(
    worktree_root: Path, params: Mapping, run_id: Optional[str], record: Mapping
) -> list[str]:
    """The record's ``dropped`` targets whose file the run's own diff deleted (present at the run
    base, gone from the worktree, its base blob nowhere else); empty when no base is known or the
    base tree is unreadable. A renamed target is not deleted: see ``renamed_dropped_targets``."""
    return _classify_dropped(worktree_root, params, run_id, record)[0]


def renamed_dropped_targets(
    worktree_root: Path, params: Mapping, run_id: Optional[str], record: Mapping
) -> dict[str, str]:
    """``{target: new_path}`` for dropped targets whose old name is gone but whose base blob sits at
    another path (index entry or a file beside the old one; a case-only rename included). Trap: a
    deleted file byte-identical to another tracked file also reads as renamed, which refuses (fail closed)."""
    return _classify_dropped(worktree_root, params, run_id, record)[1]


def _classify_dropped(
    worktree_root: Path, params: Mapping, run_id: Optional[str], record: Mapping
) -> tuple[list[str], dict[str, str]]:
    dropped = [d for d in (record.get("dropped") or []) if isinstance(d, str)]
    base = run_base_for(worktree_root, run_id, params) if dropped else None
    if not base:
        return [], {}
    from coordinator_core.git.git_state import read_tree_spine
    from coordinator_core.git.ref_cas_retry import _root_tree

    files = {d: d.split("::")[0].replace("\\", "/") for d in dropped}
    tree = _root_tree(worktree_root, base)
    spine = read_tree_spine(worktree_root, list(files.values()), root_tree_sha=tree) if tree else None
    if spine is None:
        return [], {}
    deleted: list[str] = []
    renamed: dict[str, str] = {}
    index: Optional[Mapping] = None
    for target, rel in files.items():
        parent, _, name = rel.rpartition("/")
        entry = spine.get(parent, {}).get(name)
        if entry is None:
            continue
        beside = _dir_files(Path(worktree_root) / parent) if parent else _dir_files(Path(worktree_root))
        if name in beside:
            continue
        if index is None:
            index = _index_or_empty(worktree_root)
        moved = _blob_elsewhere(worktree_root, entry[1], rel, parent, beside, index)
        if moved:
            renamed[target] = moved
        else:
            deleted.append(target)
    return deleted, renamed


def _dir_files(directory: Path) -> dict[str, Path]:
    """``{on-disk name: path}`` of the regular files directly in ``directory`` (true case on Windows)."""
    try:
        with os.scandir(directory) as it:
            return {e.name: Path(e.path) for e in it if e.is_file()}
    except OSError:
        return {}


def _index_or_empty(worktree_root: Path) -> Mapping:
    from coordinator_core.git.git_state import read_index

    try:
        return read_index(worktree_root)
    except Exception:
        return {}


def _blob_elsewhere(
    worktree_root: Path, blob_sha: str, rel: str, parent: str, beside: Mapping[str, Path], index: Mapping
) -> str:
    """The first other path holding blob ``blob_sha``: an index entry, else an untracked ``.py`` file
    beside ``rel`` (only those are hashed); ``""`` when none."""
    for path, entry in index.items():
        if path != rel and entry.sha == blob_sha:
            return path
    for name, full in beside.items():
        if not name.endswith(".py") or (f"{parent}/{name}" if parent else name) in index:
            continue
        try:
            data = full.read_bytes()
        except OSError:
            continue
        # core.autocrlf stores a CRLF worktree file as its LF blob, so hash the LF form too.
        for form in (data, data.replace(b"\r\n", b"\n")):
            if hashlib.sha1(b"blob %d\0" % len(form) + form).hexdigest() == blob_sha:
                return f"{parent}/{name}" if parent else name
    return ""


def deleted_targets_receipt_note(
    worktree_root: Path, params: Mapping, run_id: Optional[str]
) -> str:
    """Receipt text naming the dropped targets the run's own diff deleted when that is why the gate
    read benign (an all-dropped, no-tests record); ``""`` otherwise. Never a refusal."""
    record = _retest_record(worktree_root, run_id)
    dropped = record.get("dropped") if record else None
    if not dropped or record.get("tests"):
        return ""
    names = _classify_dropped(worktree_root, params, run_id, record)[0]
    if set(names) != set(dropped):
        return ""
    return "not-run: run deleted " + ", ".join(names)


def _only_run_deleted_targets(
    worktree_root: Path, params: Mapping, run_id: Optional[str], record: Mapping
) -> bool:
    """True for an all-dropped record (no tests) whose every dropped target the run's own diff deleted."""
    dropped = record.get("dropped")
    if not dropped or record.get("tests"):
        return False
    return set(run_deleted_dropped_targets(worktree_root, params, run_id, record)) == set(dropped)
