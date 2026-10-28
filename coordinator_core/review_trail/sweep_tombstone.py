"""Sweep tombstones: a record that a sidecar sweeper deleted a session's file.

The review receipt gate reads `swept_sidecars` to report a swept review as
swept rather than unrun. Tombstones live at `machinery_paths.tombstone_path`,
outside both share roots, so a whole-session-dir prune does not delete them.

Both sweepers record only after the delete succeeded (`prune_claude_temp` only
once the whole session dir is gone). Retention is bounded two ways: by age
(`MAX_AGE_SECONDS`, applied on read and by `expire_tombstones`) and by session
reuse (`since=` drops records from an earlier incarnation of a reused session id).

Negative-spec: stdlib only; no git spawn, no directory walk; `record_swept`
never raises (a sweep must not fail on its own record).
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any

from coordinator_core.session.machinery_paths import (
    safe_session_id,
    tombstone_path,
    tombstones_dir,
)

DEFAULT_REASON = "swept"
MAX_AGE_SECONDS = 30 * 86400


def _rel_posix(repo_root: str, path: str) -> str:
    try:
        if os.path.isabs(path):
            path = os.path.relpath(path, repo_root)
    except ValueError:
        pass
    return path.replace("\\", "/")


def record_swept(
    repo_root: str,
    session_id: str,
    paths: list[str],
    sweeper: str,
    now: float,
    reason: str = DEFAULT_REASON,
) -> None:
    """Append one JSON line per swept path to the session's tombstone file.

    Each line is a single `os.write` on an O_APPEND fd, so concurrent sweepers
    never interleave. Never raises.
    """
    try:
        if not paths or not safe_session_id(session_id):
            return
        target = tombstone_path(repo_root, session_id)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        swept_at = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            for p in paths:
                line = json.dumps(
                    {
                        "path": _rel_posix(repo_root, p),
                        "sweeper": sweeper,
                        "swept_at": swept_at,
                        "reason": reason,
                    },
                    sort_keys=True,
                ) + "\n"
                os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except (OSError, ValueError, TypeError):
        return


def _parse_ts(value: Any) -> float | None:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError):
        return None


def expire_tombstones(repo_root: str, now: float) -> int:
    """Delete tombstone files untouched for `MAX_AGE_SECONDS`; returns the count. Never raises."""
    removed = 0
    try:
        directory = tombstones_dir(repo_root)
        names = os.listdir(directory)
    except OSError:
        return 0
    for name in names:
        full = os.path.join(directory, name)
        try:
            if now - os.stat(full).st_mtime > MAX_AGE_SECONDS:
                os.unlink(full)
                removed += 1
        except OSError:
            continue
    return removed


def swept_sidecars(
    repo_root: str,
    session_id: str,
    now: float | None = None,
    since: float | None = None,
) -> tuple[dict[str, Any], ...]:
    """The session's live tombstone records; missing or unreadable -> `()`.

    Records older than `MAX_AGE_SECONDS` before `now` (default: the clock) are
    dropped; with `since` (the current session incarnation's start), so are
    records swept before it.
    """
    now = time.time() if now is None else now
    try:
        if not safe_session_id(session_id):
            return ()
        with open(tombstone_path(repo_root, session_id), "r", encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError):
        return ()
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        ts = _parse_ts(rec.get("swept_at"))
        if ts is not None:
            if now - ts > MAX_AGE_SECONDS or (since is not None and ts < since):
                continue
        out.append(rec)
    return tuple(out)
