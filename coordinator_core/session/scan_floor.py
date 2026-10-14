"""Session-start floor and the floored scandir walk the own-sid scan legs share."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

# Seconds subtracted from the session's started_at so a file written just before it still passes.
FLOOR_SLACK_S: float = 300.0


def _parse_epoch(text: str) -> float | None:
    text = text.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def session_start_floor(repo_root: Path, sid: str) -> float | None:
    """Earlier of the live and newest archived ``started_at``, minus ``FLOOR_SLACK_S``.

    Resume keeps the live ``started_at`` winning over an archived one, so the earlier of the
    two bounds every file the session could have written. ``None`` when neither yields a
    parseable timestamp. A naive timestamp is read as UTC. No spawn.
    """
    from coordinator_core.session.core import archived_session_dir, session_dir

    try:
        live = session_dir(sid, str(repo_root))
        archived = archived_session_dir(sid, str(repo_root))
    except ValueError:
        return None
    epochs = [
        e
        for e in (_read_epoch(live), _read_epoch(archived))
        if e is not None
    ]
    if not epochs:
        return None
    return min(epochs) - FLOOR_SLACK_S


def _read_epoch(sdir: str | None) -> float | None:
    if not sdir:
        return None
    try:
        with open(os.path.join(sdir, "started_at"), encoding="utf-8") as fh:
            return _parse_epoch(fh.read())
    except (OSError, UnicodeDecodeError):
        return None


def iter_files_at_or_above(
    directory: Path,
    floor: float | None,
    always: frozenset[str],
    *,
    recursive: bool,
) -> Iterator[Path]:
    """Yield files (symlinks to files included) under ``directory`` with mtime >= ``floor`` or a name in ``always``.

    ``floor is None`` yields every file. An unreadable directory yields nothing for itself.
    Never opens a file; symlinked directories are not descended.
    """
    try:
        with os.scandir(directory) as it:
            entries = list(it)
    except OSError:
        return
    subdirs: list[Path] = []
    for entry in entries:
        try:
            if entry.is_dir(follow_symlinks=False):
                if recursive:
                    subdirs.append(Path(entry.path))
                continue
            if not entry.is_file():
                continue
            if floor is None or entry.name in always or entry.stat().st_mtime >= floor:
                yield Path(entry.path)
        except OSError:
            continue
    for sub in subdirs:
        yield from iter_files_at_or_above(sub, floor, always, recursive=True)
