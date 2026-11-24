"""Stamp a run's ``test-leg.json`` with when and on which commit it was measured, and judge a read of it stale.

Unknown is stale: a leg with no stamp, an unreadable stamp, or a stamp the tree has outrun is re-measured, never cited.
No git spawn: HEAD is read in-process by ``git_state.head_sha``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional

from coordinator_core.git import git_state
from coordinator_core.ops.dispatch_emit import landing_integrity
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

#: Manifest keys naming the run's own-diff paths, first present wins (the manifest carries `review_declared_paths` today).
OWN_DIFF_KEYS = ("own_diff_paths", "review_declared_paths")


def stamp(result: dict, *, measured_at: str, head_sha: Optional[str]) -> dict:
    """`result` plus `measured_at` (UTC ISO-8601, measurement start) and `head_sha` (the commit measured, or None).

    The caller passes both from `started_stamp`, taken before the run: never HEAD re-read at write time.
    """
    return {**result, "measured_at": measured_at, "head_sha": head_sha}


def started_stamp(repo_root, head_rev: Optional[str]) -> tuple[str, Optional[str]]:
    """`(measured_at, head_sha)` to take BEFORE the leg runs.

    `head_rev` None or "HEAD" measures the working tree's HEAD (read in-process); any other value is the
    resolved commit the leg measures and is stamped as given.
    """
    measured_at = datetime.now(timezone.utc).isoformat()
    if head_rev is None or head_rev == "HEAD":
        return measured_at, git_state.head_sha(repo_root)
    return measured_at, head_rev


def leg_stamp(record: Optional[Mapping]) -> dict:
    """`{measured_at, head_sha}` of a leg record; each None when absent or not a non-empty string."""
    def field(name: str) -> Optional[str]:
        value = record.get(name) if isinstance(record, Mapping) else None
        return value if isinstance(value, str) and value else None

    return {"measured_at": field("measured_at"), "head_sha": field("head_sha")}


def _epoch(measured_at: str) -> Optional[float]:
    try:
        moment = datetime.fromisoformat(measured_at)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _own_diff_paths(manifest: Optional[Mapping]) -> list[str]:
    if not isinstance(manifest, Mapping):
        return []
    for key in OWN_DIFF_KEYS:
        paths = manifest.get(key)
        if isinstance(paths, list):
            return [p for p in paths if isinstance(p, str) and p]
    return []


def _stale_reason(record: Optional[dict], worktree_root: Path, manifest: Optional[Mapping]) -> Optional[str]:
    if record is None:
        return "no test-leg.json for the run"
    stamped = leg_stamp(record)
    if stamped["measured_at"] is None or stamped["head_sha"] is None:
        return "test leg carries no measured_at/head_sha stamp"
    current = git_state.head_sha(worktree_root)
    if current is None:
        return "current HEAD is unreadable"
    if stamped["head_sha"] != current:
        return f"test leg measured {stamped['head_sha'][:12]}, HEAD is {current[:12]}"
    measured = _epoch(stamped["measured_at"])
    if measured is None:
        return "test leg measured_at is not an ISO-8601 time"
    for rel in _own_diff_paths(manifest):
        relative = Path(rel)
        if relative.is_absolute() or ".." in relative.parts:
            continue
        try:
            mtime = (worktree_root / relative).stat().st_mtime
        except OSError:
            continue
        if mtime > measured:
            return f"{rel} was written after the test leg was measured"
    return None


def is_stale(worktree_root, run_id: Optional[str], manifest: Optional[Mapping]) -> Optional[str]:
    """A reason string when the run's test leg was not measured on the work being judged, else None.

    Stale: no leg, no stamp, `head_sha` unequal to the tree's HEAD, or `measured_at` older than the newest
    mtime of an own-diff path (`manifest` `own_diff_paths`, else `review_declared_paths`).
    """
    root = Path(worktree_root)
    return _stale_reason(landing_integrity.test_leg_record(root, run_id), root, manifest)


def halt_stamp(record_dir, manifest: Optional[Mapping] = None) -> dict:
    """`{measured_at, head_sha, current_head, stale}` for the leg in `record_dir` (`<root>/<RECORD_DIR_ROOT>/<run_id>`).

    One leg read; a missing leg gives null stamps and `stale: True`. A record dir outside that layout reads as no leg.
    """
    record_dir = Path(record_dir)
    depth = len(Path(RECORD_DIR_ROOT).parts)
    root = record_dir.parents[depth] if len(record_dir.parents) > depth else None
    record = landing_integrity.test_leg_record(root, record_dir.name) if root is not None else None
    stamped = leg_stamp(record)
    current = git_state.head_sha(root) if root is not None else None
    reason = _stale_reason(record, root, manifest) if root is not None else "no test-leg.json for the run"
    return {**stamped, "current_head": current, "stale": reason is not None}
