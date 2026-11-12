"""
coordinator_core.ops.repo_scratch_sweep -- age-based, batched sweep of ``<repo>/scratch/``.

Purpose: "move it to scratch" is the delete only if something reaps scratch.
``sweep_repo_scratch`` removes top-level ``scratch/`` entries whose newest
non-directory mtime is older than ``DEFAULT_MAX_AGE_DAYS``, at most
``DEFAULT_BATCH`` removals per call, and is called once per housekeeping cycle.

Constants, each the answer to one question:
    DEFAULT_MAX_AGE_DAYS (7) -- is this entry abandoned.
    DEFAULT_BATCH (200)      -- how much delete I/O may one cycle spend; a 9k-entry
                                rm on E: coincided with engine I/O wedges.
    run-name skip set        -- does a live run still need this: ``scratch/warp``
                                is a container, and ``scratch/warp/<run>`` is skipped
                                unless ``state/warp/<run>/run-stage.json`` reads
                                stage ``landed`` (the one terminal stage; ``halted``
                                can resume). Absent or unreadable state is live.

Negative spec:
    - Never descend a symlink or junction; a link is unlinked as a link, only when
      its own lstat mtime is aged. A young link is counted under ``skipped.link``.
    - Never spawn a process; one scandir walk per entry, stopping at the first
      mtime inside the threshold. Directory mtimes are ignored when the tree holds
      any non-directory entry: deleting children bumps the parent's mtime, which
      would make a half-swept tree look young to the next cycle.
    - Windows liveness probe: rename the aged entry to a sibling tombstone and back;
      PermissionError means held. POSIX has no probe; the mtime gate is the test.
      A tombstone whose rename-back failed is left in place and reported under
      ``tombstones``.
    - Every unlink/rmdir counts against the batch; the call stops at the cap.
"""

from __future__ import annotations

import json
import os
import stat
import time
from typing import Dict, List, Optional, Tuple

from coordinator_core.session import record_homes

DEFAULT_MAX_AGE_DAYS = 7
DEFAULT_BATCH = 200

_SECONDS_PER_DAY = 86400.0
_WARP_DIRNAME = "warp"
_TERMINAL_RUN_STAGES = frozenset({"landed"})
_IS_WINDOWS = os.name == "nt"
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

_YOUNG = "young"
_HELD = "held"


def _is_link(entry_stat: os.stat_result) -> bool:
    if _IS_WINDOWS:
        return bool(getattr(entry_stat, "st_file_attributes", 0) & _REPARSE)
    return stat.S_ISLNK(entry_stat.st_mode)


def _walk_aged(root: str, root_stat: os.stat_result, cutoff: float):
    """Return ``(files, dirs)`` of an entry wholly older than *cutoff* (``files`` holds
    every non-directory leaf, links included; ``dirs`` is pre-order, root first), or
    ``_YOUNG`` at the first mtime inside the threshold, or ``_HELD`` when a directory
    cannot be read. One scandir per directory, no second walk."""
    if not stat.S_ISDIR(root_stat.st_mode) or _is_link(root_stat):
        return ([root], []) if root_stat.st_mtime <= cutoff else _YOUNG
    files: List[str] = []
    dirs: List[str] = [root]
    dir_newest = root_stat.st_mtime
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                entries = list(it)
        except OSError:
            return _HELD
        for e in entries:
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                return _HELD
            if not _is_link(st) and stat.S_ISDIR(st.st_mode):
                dirs.append(e.path)
                stack.append(e.path)
                if st.st_mtime > dir_newest:
                    dir_newest = st.st_mtime
                continue
            if st.st_mtime > cutoff:
                return _YOUNG
            files.append(e.path)
    if not files and dir_newest > cutoff:
        return _YOUNG
    return files, dirs


def _held_probe(path: str) -> Tuple[bool, Optional[str]]:
    """Windows only: ``(held, tombstone_left)``. Held means rename away failed or the
    rename back failed (the tombstone stays, named in the second slot)."""
    if not _IS_WINDOWS:
        return False, None
    tomb = f"{path}.sweep-tomb-{os.getpid()}"
    try:
        os.rename(path, tomb)
    except OSError:
        return True, None
    try:
        os.rename(tomb, path)
    except OSError:
        return True, tomb
    return False, None


def _remove(path: str, is_dir: bool) -> None:
    if is_dir:
        os.rmdir(path)
        return
    try:
        os.unlink(path)
    except PermissionError:
        os.chmod(path, stat.S_IWRITE)
        os.unlink(path)
    except IsADirectoryError:
        os.rmdir(path)


def _unlink_link(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        os.rmdir(path)


def _run_is_terminal(repo_root: str, run_id: str) -> bool:
    try:
        with open(
            os.path.join(record_homes.home_dir(repo_root, "warp"), run_id, "run-stage.json"),
            encoding="utf-8",
        ) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("stage") in _TERMINAL_RUN_STAGES


def _list_items(scratch: str) -> List[Tuple[str, Optional[str]]]:
    """Top-level entries as ``(path, run_id_or_None)``, ``scratch/warp`` expanded to
    its children (each carrying its run id)."""
    try:
        with os.scandir(scratch) as it:
            top = sorted(it, key=lambda e: e.name)
    except OSError:
        return []
    items: List[Tuple[str, Optional[str]]] = []
    for e in top:
        if e.name == _WARP_DIRNAME:
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if not _is_link(st) and stat.S_ISDIR(st.st_mode):
                try:
                    with os.scandir(e.path) as it:
                        kids = sorted(it, key=lambda k: k.name)
                except OSError:
                    continue
                items.extend((k.path, k.name) for k in kids)
                continue
        items.append((e.path, None))
    return items


def sweep_repo_scratch(
    repo_root,
    *,
    max_age_days: float = DEFAULT_MAX_AGE_DAYS,
    batch: int = DEFAULT_BATCH,
    dry_run: bool = False,
) -> dict:
    """Sweep ``<repo_root>/scratch`` once. Returns::

        {"deleted": int, "skipped": {"young", "link", "held", "run_named"},
         "remaining": int, "batch_exhausted": bool, "dry_run": bool,
         "tombstones": [path, ...], "errors": [str, ...]}

    ``deleted`` counts unlink/rmdir operations (planned ones under ``dry_run``);
    ``remaining`` counts entries not yet evaluated plus a partly deleted one. A
    missing scratch root returns the empty result."""
    repo_root = str(repo_root)
    skipped: Dict[str, int] = {"young": 0, "link": 0, "held": 0, "run_named": 0}
    result = {
        "deleted": 0,
        "skipped": skipped,
        "remaining": 0,
        "batch_exhausted": False,
        "dry_run": dry_run,
        "tombstones": [],
        "errors": [],
    }
    scratch = os.path.join(repo_root, "scratch")
    items = _list_items(scratch)
    if not items:
        return result

    cutoff = time.time() - max_age_days * _SECONDS_PER_DAY
    budget = batch
    for idx, (path, run_id) in enumerate(items):
        if budget <= 0:
            result["batch_exhausted"] = True
            result["remaining"] = len(items) - idx
            return result
        if run_id is not None and not _run_is_terminal(repo_root, run_id):
            skipped["run_named"] += 1
            continue
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if _is_link(st):
            if st.st_mtime > cutoff:
                skipped["link"] += 1
                continue
            if not dry_run:
                try:
                    _unlink_link(path)
                except OSError as exc:
                    result["errors"].append(f"{path}: {exc}")
                    continue
            result["deleted"] += 1
            budget -= 1
            continue
        walked = _walk_aged(path, st, cutoff)
        if walked is _YOUNG:
            skipped["young"] += 1
            continue
        if walked is _HELD:
            skipped["held"] += 1
            continue
        files, dirs = walked
        if not dry_run:
            held, tomb = _held_probe(path)
            if tomb:
                result["tombstones"].append(tomb)
            if held:
                skipped["held"] += 1
                continue
        ops = [(p, False) for p in files] + [(p, True) for p in reversed(dirs)]
        done = 0
        for p, is_dir in ops:
            if budget <= 0:
                break
            if not dry_run:
                try:
                    _remove(p, is_dir)
                except FileNotFoundError:
                    pass
                except PermissionError:
                    skipped["held"] += 1
                    break
                except OSError as exc:
                    result["errors"].append(f"{p}: {exc}")
                    break
            result["deleted"] += 1
            budget -= 1
            done += 1
        if done < len(ops) and budget <= 0:
            result["batch_exhausted"] = True
            result["remaining"] = len(items) - idx
            return result
    return result
