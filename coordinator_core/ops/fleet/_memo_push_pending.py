"""coordinator_core.ops.fleet._memo_push_pending -- durable push-pending records.

One JSON file per delivery commit that still owes a push, under
`<sender outbox>/push-pending/<commit_sha>.json`. Pure stdlib, zero git spawns.

Negative-spec:
    - A record is removed only by `remove_pending` (a landed push, or an
      upstream tree already holding the path); a failed or rejected push goes
      through `bump_attempt`, which never deletes; `is_exhausted` records stop
      being retried but stay on disk.
    - `bump_attempt` never recreates a record that was removed meanwhile.
    - Unreadable or malformed files are skipped by `list_pending`, never raised.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

from coordinator_core.ops.fleet._outbox_enum import outbox_dir

PENDING_LEAF = "push-pending"

#: A record at this many failed attempts is exhausted: the warm sweep stops
#: pushing it and leaves the file in place for a human or doctor read.
MAX_ATTEMPTS = 20

#: Keys every record carries; `attempts` and `last_error` default on write.
REQUIRED_KEYS = (
    "receiver_root",
    "remote",
    "branch",
    "path",
    "blob_sha",
    "message",
    "commit_sha",
    "parent_sha",
)

#: Set by `list_pending` on each returned row so `bump_attempt` can find its
#: file; never persisted.
RECORD_PATH_KEY = "record_path"

_SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")


def pending_dir(worktree: object) -> Path:
    """The `push-pending/` directory of the sender outbox rooted at `worktree`."""
    return outbox_dir(Path(str(worktree))) / PENDING_LEAF


def _record_file(worktree: object, commit_sha: str) -> Path:
    if not _SHA_RE.match(commit_sha or ""):
        raise ValueError(f"commit_sha {commit_sha!r} is not a lowercase hex object id")
    return pending_dir(worktree) / f"{commit_sha}.json"


def _write_atomic(target: Path, row: dict) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".{target.stem}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()


def write_pending(worktree: object, record: dict) -> Path:
    """Write `record` under its `commit_sha` and return the file path.

    Raises ValueError when a `REQUIRED_KEYS` key is missing or `commit_sha`
    is not a hex object id. Rewriting the same sha replaces the file.
    """
    missing = [k for k in REQUIRED_KEYS if k not in record]
    if missing:
        raise ValueError(f"push-pending record lacks {', '.join(missing)}")
    row = {k: v for k, v in record.items() if k != RECORD_PATH_KEY}
    row.setdefault("attempts", 0)
    row.setdefault("last_error", "")
    target = _record_file(worktree, row["commit_sha"])
    _write_atomic(target, row)
    return target


def list_pending(worktree: object) -> list[dict]:
    """Every readable record, oldest sha-name first, each with `RECORD_PATH_KEY` set."""
    root = pending_dir(worktree)
    if not root.is_dir():
        return []
    rows: list[dict] = []
    for path in sorted(root.glob("*.json"), key=lambda p: p.name):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(row, dict) or any(k not in row for k in REQUIRED_KEYS):
            continue
        row[RECORD_PATH_KEY] = str(path)
        rows.append(row)
    return rows


def is_exhausted(record: dict) -> bool:
    """True when `record` has used `MAX_ATTEMPTS` failed pushes."""
    try:
        return int(record.get("attempts", 0)) >= MAX_ATTEMPTS
    except (TypeError, ValueError):
        return False


def remove_pending(worktree: object, commit_sha: str) -> bool:
    """Delete the record for `commit_sha`; `False` when none existed."""
    try:
        _record_file(worktree, commit_sha).unlink()
    except FileNotFoundError:
        return False
    return True


def bump_attempt(record: dict, error: str) -> Optional[dict]:
    """Rewrite `record`'s file with `attempts` + 1 and `last_error` set to `error`.

    `record` must come from `list_pending`. Returns the updated row, or `None`
    when the file is gone (the record is not resurrected).
    """
    location = record.get(RECORD_PATH_KEY)
    if not location:
        raise ValueError("record has no record_path; take it from list_pending")
    target = Path(location)
    if not target.exists():
        return None
    row = {k: v for k, v in record.items() if k != RECORD_PATH_KEY}
    row["attempts"] = int(row.get("attempts", 0)) + 1
    row["last_error"] = str(error)
    _write_atomic(target, row)
    row[RECORD_PATH_KEY] = str(target)
    return row


def retarget_pending(worktree: object, old_commit_sha: str, new_record: dict) -> Path:
    """Write `new_record`, then remove the record keyed by `old_commit_sha`.

    A rebuild changes the commit sha, so it is a new file; the new one lands
    first so a crash between the two leaves a record, never none.
    """
    written = write_pending(worktree, new_record)
    if old_commit_sha != new_record["commit_sha"]:
        remove_pending(worktree, old_commit_sha)
    return written
