"""
coordinator_core.ops.fleet._receiver_materialize — write one memo file into a
receiver repo's worktree and commit it hook-free.

Shared by `memo.heal_inbox` (restore), `memo.send` (delivered means file) and
`memo.sweep_ref_only`. The write is O_EXCL and the commit goes through
`git_native.commit_authored_new_file`, which never runs a receiver hook.

Invariants:
  - Never overwrites: an existing target, or a filename already present in
    either corpus root's inbox or archive (any depth), is `already_present`.
  - A declined commit removes the file this call wrote, so a retry is not
    refused by the no-clobber guard.
  - No remote transport, no push, no branch creation or move, no HEAD switch.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Collection, NamedTuple, Optional

from coordinator_core.ops.ceremony import git_native
from coordinator_core.ops.fleet._memo_anchor import present_filenames
from coordinator_core.temp_layout import managed_tempdir

STATUS_MATERIALIZED = "materialized"
STATUS_ALREADY_PRESENT = "already_present"
STATUS_WRITE_FAILED = "materialize_write_failed"
STATUS_COMMIT_DECLINED = "materialize_commit_declined"

DEST_INBOX = "inbox"
DEST_ARCHIVE = "archive"


class MaterializeOutcome(NamedTuple):
    """`status` is one of the STATUS_* constants; `rel_path` is the receiver-relative
    posix target; `commit_sha` is set only when materialized; `reason` only on failure."""

    status: str
    rel_path: str
    commit_sha: Optional[str]
    reason: Optional[str]


def rollback_unwritten_file(target_file: Path) -> str:
    try:
        target_file.unlink()
    except OSError as unlink_exc:
        return (
            f" WARNING: the file this call wrote could NOT be removed"
            f" ({unlink_exc}); it is left uncommitted and a retry will refuse"
            f" on the no-clobber guard until it is cleared."
        )
    return " the file this call wrote was removed (tree restored)."


def write_msg_file(text: str) -> Path:
    fd, name = tempfile.mkstemp(prefix="memo-heal-msg-", suffix=".txt", dir=managed_tempdir())
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    except BaseException:
        os.unlink(name)
        raise
    return Path(name)


def _target(receiver_root: Path, filename: str, dest: str) -> Path:
    from coordinator_core import memo_corpus
    from coordinator_core.ops.fleet.archive_actioned_memos import memo_archive_dest

    if dest == DEST_INBOX:
        return Path(memo_corpus.memo_corpus_root(str(receiver_root))) / "inbox" / filename
    if dest == DEST_ARCHIVE:
        return memo_archive_dest(receiver_root, Path(filename))
    raise ValueError(f"materialize_memo: dest must be 'inbox' or 'archive', got {dest!r}")


def materialize_memo(
    receiver_root: Path,
    filename: str,
    content: str,
    *,
    dest: str = DEST_INBOX,
    commit_message: str,
    known_present: Optional[Collection[str]] = None,
) -> MaterializeOutcome:
    """Write `content` at the receiver's `dest` path and commit it hook-free.

    `known_present` is a caller-held set of filenames already present in the
    receiver (a sweep that scanned once); when None the receiver is scanned here.
    """
    receiver_root = Path(receiver_root)
    target_file = _target(receiver_root, filename, dest)
    rel_path = target_file.relative_to(receiver_root).as_posix()

    present = known_present if known_present is not None else present_filenames(receiver_root)
    if filename in present:
        return MaterializeOutcome(STATUS_ALREADY_PRESENT, rel_path, None, None)

    try:
        target_file.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return MaterializeOutcome(STATUS_WRITE_FAILED, rel_path, None, f"write failed for {filename!r}: {exc}")
    try:
        fd = os.open(str(target_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
    except FileExistsError:
        return MaterializeOutcome(STATUS_ALREADY_PRESENT, rel_path, None, None)
    except OSError as exc:
        return MaterializeOutcome(STATUS_WRITE_FAILED, rel_path, None, f"write failed for {filename!r}: {exc}")

    msg_file = write_msg_file(commit_message)
    try:
        commit_result = git_native.commit_authored_new_file(
            rel_path, content, msg_file, receiver_root, record_ledger=True,
        )
    finally:
        try:
            msg_file.unlink()
        except OSError:
            pass

    if not commit_result.ok:
        rollback_detail = rollback_unwritten_file(target_file)
        return MaterializeOutcome(
            STATUS_COMMIT_DECLINED, rel_path, None,
            f"commit declined for {filename!r}: {commit_result.stderr};{rollback_detail}",
        )
    return MaterializeOutcome(STATUS_MATERIALIZED, rel_path, commit_result.stdout.strip(), None)
