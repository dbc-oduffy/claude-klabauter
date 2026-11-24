"""Ledger test-freshness check: a `met` item needs a test run stamped after its writes' last edit.

Stat-only, no spawn: the verify leg runs the tests and stamps `tests_ran_at`; this module
compares that stamp with the newest st_mtime among the row's writes.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional, Sequence, Tuple

TESTS_RAN_AT_KEY = "tests_ran_at"
STALE_TESTS_REASON = "met without a test run after the last edit of its writes"


def _newest_under(root: Path) -> Optional[Tuple[float, str]]:
    """Newest (st_mtime, path) of the files beneath `root`; the directories' own mtimes are skipped."""
    newest: Optional[Tuple[float, str]] = None
    stack = [str(root)]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                            continue
                        mtime = entry.stat().st_mtime
                    except OSError:
                        continue
                    if newest is None or mtime > newest[0]:
                        newest = (mtime, entry.path)
        except OSError:
            continue
    return newest


def _newest_write(repo_root: Path, paths: Sequence[str]) -> Optional[Tuple[float, str]]:
    newest: Optional[Tuple[float, str]] = None
    for rel in paths:
        target = Path(repo_root) / rel
        try:
            if target.is_dir():
                found = _newest_under(target)
            else:
                found = (target.stat().st_mtime, str(rel))
        except OSError:
            continue
        if found is not None and (newest is None or found[0] > newest[0]):
            newest = found
    return newest


def last_edit_at(repo_root: Path, paths: Sequence[str]) -> Optional[datetime]:
    """Newest st_mtime (UTC) over the existing `paths`, a directory counting by its files; None if none exist."""
    newest = _newest_write(repo_root, paths)
    if newest is None:
        return None
    return datetime.fromtimestamp(newest[0], tz=timezone.utc)


def _parse_aware(stamp: object) -> Optional[datetime]:
    if not isinstance(stamp, str) or not stamp.strip():
        return None
    try:
        moment = datetime.fromisoformat(stamp.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def stale_tests_refusal(repo_root: Path, writes: Sequence[str], item: Mapping) -> Optional[str]:
    """Refusal reason when `item` lacks a valid `tests_ran_at` at or after its writes' last edit; None otherwise.

    None also when no write exists. A naive stamp is refused, never assumed UTC.
    """
    newest = _newest_write(repo_root, writes)
    if newest is None:
        return None
    edited = datetime.fromtimestamp(newest[0], tz=timezone.utc)
    raw = item.get(TESTS_RAN_AT_KEY)
    ran = _parse_aware(raw)
    if ran is None:
        shown = "absent" if raw in (None, "") else f"unusable ({raw!r})"
        return (
            f"{STALE_TESTS_REASON}: {TESTS_RAN_AT_KEY} {shown}; "
            f"newest write {newest[1]} edited {edited.isoformat()}"
        )
    if ran < edited:
        return (
            f"{STALE_TESTS_REASON}: newest write {newest[1]} edited {edited.isoformat()}, "
            f"{TESTS_RAN_AT_KEY} {ran.isoformat()}"
        )
    return None
