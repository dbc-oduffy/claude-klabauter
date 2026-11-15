"""Peer-window withhold for a blanket safe-commit: which staged non-own paths a live peer may own.

A staged path is attributable to a live peer when it is not own and was written at or after the
earliest `started_at` of any other live session. Pure over stat and one file read per peer; no git.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _parse_started_at(text: str) -> datetime:
    """Parse `now_iso()` output (`%Y-%m-%dT%H:%M:%SZ`); anything else is the epoch."""
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return _EPOCH


def read_peer_starts(
    sessions_dir: str | os.PathLike, live_ids: Iterable[str], self_id: str
) -> dict[str, datetime]:
    """Map each live non-self session id to its `started_at`. An unreadable or malformed file
    counts as the epoch, widening the window to every dated path (fails toward withholding)."""
    starts: dict[str, datetime] = {}
    for sid in live_ids:
        if sid == self_id:
            continue
        try:
            text = (Path(sessions_dir) / sid / "started_at").read_text(encoding="utf-8")
        except OSError:
            starts[sid] = _EPOCH
            continue
        starts[sid] = _parse_started_at(text)
    return starts


def withheld_paths(
    worktree_root: str | os.PathLike,
    staged: Iterable[str],
    own: Iterable[str],
    peer_starts: Mapping[str, datetime],
) -> list[str]:
    """Staged paths not in `own` whose mtime is at or after the earliest peer start.

    `started_at` has second resolution, so the mtime is compared at its floored second. A path
    with no stat (deleted) cannot be dated and is withheld whenever any peer is live. No live
    peer withholds nothing. Order of `staged` is kept.
    """
    if not peer_starts:
        return []
    floor = int(min(peer_starts.values()).timestamp())
    own_set = {p.replace("\\", "/") for p in own}
    root = Path(worktree_root)
    out: list[str] = []
    for path in staged:
        norm = path.replace("\\", "/")
        if norm in own_set:
            continue
        try:
            mtime = os.stat(root / norm).st_mtime
        except OSError:
            out.append(path)
            continue
        if int(mtime) >= floor:
            out.append(path)
    return out
