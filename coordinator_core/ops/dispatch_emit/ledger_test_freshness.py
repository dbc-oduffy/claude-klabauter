"""Ledger test-freshness check: a `met` item needs a test run stamped after its writes' last edit.

Stat and hash reads only, no spawn: the stamp is the item's `tests_ran_at` or the run's own test-leg
`measured_at`, compared with the newest st_mtime among the row's writes; an mtime past the stamp is an
edit only when the file's bytes differ from the run's `measured-rev.json`.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional, Sequence, Tuple

TESTS_RAN_AT_KEY = "tests_ran_at"
STALE_TESTS_REASON = "met without a test run after the last edit of its writes"
_RED_STATUSES = ("fail", "error")


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


def _files_newer(repo_root: Path, paths: Sequence[str], after: float) -> list[str]:
    """Repo-relative (posix) files among `paths` (a directory by its files) whose st_mtime is past `after`."""
    out: list[str] = []
    root = Path(repo_root)
    for rel in paths:
        target = root / rel
        try:
            if not target.is_dir():
                if target.stat().st_mtime > after:
                    out.append(str(rel).replace("\\", "/"))
                continue
        except OSError:
            continue
        stack = [str(target)]
        while stack:
            try:
                with os.scandir(stack.pop()) as entries:
                    for entry in entries:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(entry.path)
                            elif entry.stat().st_mtime > after:
                                out.append(os.path.relpath(entry.path, root).replace("\\", "/"))
                        except OSError:
                            continue
            except OSError:
                continue
    return out


def _leg_measured_at(repo_root: Path, run_id: Optional[str]) -> Optional[datetime]:
    """The run's own test-leg `measured_at` (UTC), None when the run has no leg record, no usable stamp, or a leg
    that recorded itself red: the leg writer stamps every leg, so a stamp alone never vouches for a failing run."""
    from coordinator_core.ops.dispatch_emit import landing_integrity, test_leg_freshness

    record = landing_integrity.test_leg_record(Path(repo_root), run_id)
    if record and (record.get("failing") or record.get("status_after", record.get("status")) in _RED_STATUSES):
        return None
    return _parse_aware(test_leg_freshness.leg_stamp(record)["measured_at"])


def _changed_since_leg(repo_root: Path, run_id: Optional[str], leg_at: datetime, newer: Sequence[str]) -> Optional[str]:
    """First of `newer` whose content is not what the run's `measured-rev.json` recorded, else None.

    The record counts only when it predates the leg's measurement start (its blobs are then what the
    leg ran against); a path the record does not name, a missing or later record, or an unreadable
    hash is not provably unchanged.
    """
    from coordinator_core.git import commit
    from coordinator_core.ops.dispatch_emit.ask_contract import measured_rev_record_path

    try:
        record = measured_rev_record_path(Path(repo_root), run_id or "")
        if record.stat().st_mtime > leg_at.timestamp():
            return newer[0]
        blobs = json.loads(record.read_text(encoding="utf-8")).get("blobs") or {}
        return next((rel for rel in newer if blobs.get(rel) != commit.checkin_blob_sha(Path(repo_root), rel)), None)
    except (OSError, ValueError, AttributeError):
        return newer[0]


def _reverify_at(repo_root: Path, inline_review: Optional[Mapping]) -> Optional[datetime]:
    """Finish time (UTC) of the run's green review re-verify; None when absent, red, errored or unstamped.

    trap: the stamp is the newest mtime of the record's sidecar, and the re-verify's own coverage of an item's writes is not checked."""
    if not isinstance(inline_review, Mapping):
        return None
    from coordinator_core.ops.dispatch_emit import landing_targeted_tests, terminal_commit_review

    if not landing_targeted_tests.reverify_passed_clean({"inline_review": inline_review}):
        return None
    block = inline_review.get("reverify")
    if isinstance(block, Mapping) and block.get("tests_run") == 0:
        return None
    epoch = terminal_commit_review._reverify_finished_at(Path(repo_root), dict(inline_review))
    return None if epoch is None else datetime.fromtimestamp(epoch, tz=timezone.utc)


def stale_tests_refusal(
    repo_root: Path, writes: Sequence[str], item: Mapping, run_id: Optional[str] = None,
    inline_review: Optional[Mapping] = None,
) -> Optional[str]:
    """Refusal reason when no test run is stamped at or after the content of the row's writes; None otherwise.

    The stamp is the latest of the item's `tests_ran_at`, the `run_id` run's test-leg `measured_at` and the
    finish of a green `inline_review` re-verify (red, errored or unstamped ones count for nothing).
    A write whose mtime is past the stamp still passes while its bytes equal the run's measured-rev
    record (a peer's touch is not an edit); content that differs from it is refused. None also when no
    write exists. A naive stamp is refused, never assumed UTC.
    """
    newest = _newest_write(repo_root, writes)
    if newest is None:
        return None
    edited = datetime.fromtimestamp(newest[0], tz=timezone.utc)
    raw = item.get(TESTS_RAN_AT_KEY)
    ran = _parse_aware(raw)
    leg_at = _leg_measured_at(repo_root, run_id)
    reverify_at = _reverify_at(repo_root, inline_review)
    stamp = max((s for s in (ran, leg_at, reverify_at) if s is not None), default=None)
    if stamp is None:
        shown = "absent" if raw in (None, "") else f"unusable ({raw!r})"
        return (
            f"{STALE_TESTS_REASON}: {TESTS_RAN_AT_KEY} {shown}; "
            f"newest write {newest[1]} edited {edited.isoformat()}"
        )
    if stamp >= edited:
        return None
    if leg_at is None:
        return (
            f"{STALE_TESTS_REASON}: newest write {newest[1]} edited {edited.isoformat()}, "
            f"{TESTS_RAN_AT_KEY} {stamp.isoformat()}"
        )
    newer = _files_newer(repo_root, writes, stamp.timestamp())
    changed = _changed_since_leg(repo_root, run_id, leg_at, newer) if newer else None
    if changed is None:
        return None
    return (
        f"{STALE_TESTS_REASON}: {changed} changed after the test leg measured it; "
        f"newest write {newest[1]} edited {edited.isoformat()}, test leg {leg_at.isoformat()}"
    )
