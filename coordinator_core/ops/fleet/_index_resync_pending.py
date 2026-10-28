"""Pending-resync records: archival moves whose main-index resync was exhausted.

Each record lives as one JSON file under ``<git_dir>/coordinator-index-resync-pending/``.
The records describe this clone's index, are never committed, and writing them cannot
dirty the tree a drain is cleaning. Leaf module: stdlib plus ``atomic_replace`` only;
git-dir resolution reads the ``.git`` entry directly and spawns nothing.

Every public function swallows OSError/ValueError (logged): a record-store fault must
never fail the archival op that called it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
from pathlib import Path
from typing import Iterable, List

from coordinator_core.atomic_replace import atomic_write_bytes

_LOG = logging.getLogger(__name__)
_LOG.addHandler(logging.NullHandler())

PENDING_DIRNAME = "coordinator-index-resync-pending"


@dataclasses.dataclass(frozen=True)
class PendingResync:
    """One archival move (``src`` -> ``dst``) whose index resync still owes a restore."""

    src: str
    dst: str
    candidate_id: str
    committed_blob: str
    op_label: str
    recorded_at: str
    last_reported_class: str = ""


def _git_dir(worktree_root: Path) -> Path:
    """``.git`` itself when a directory, else the ``gitdir:`` target of a ``.git`` file."""
    dot_git = Path(worktree_root) / ".git"
    if dot_git.is_file():
        for line in dot_git.read_text(encoding="utf-8").splitlines():
            if line.startswith("gitdir:"):
                target = Path(line[len("gitdir:"):].strip().replace("\\", "/"))
                return target if target.is_absolute() else Path(worktree_root) / target
    return dot_git


def pending_dir(worktree_root: Path) -> Path:
    """The record directory under the clone's git dir; not created here."""
    return _git_dir(worktree_root) / PENDING_DIRNAME


def _record_name(rec: PendingResync) -> str:
    key = rec.src + "\0" + rec.dst
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16] + ".json"


def record_pending(worktree_root: Path, records: Iterable[PendingResync]) -> None:
    """Write one file per record; re-recording a src/dst pair overwrites it."""
    try:
        directory = pending_dir(worktree_root)
        for rec in records:
            directory.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(dataclasses.asdict(rec), sort_keys=True).encode("utf-8")
            atomic_write_bytes(directory / _record_name(rec), payload)
    except (OSError, ValueError) as exc:
        _LOG.error("index-resync pending record write failed: %s", exc)


def list_pending(worktree_root: Path) -> List[PendingResync]:
    """All parseable records; an absent directory yields ``[]``, a bad file is skipped."""
    try:
        files = sorted(pending_dir(worktree_root).glob("*.json"))
    except (OSError, ValueError):
        return []
    out: List[PendingResync] = []
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            out.append(PendingResync(**data))
        except (OSError, ValueError, TypeError) as exc:
            _LOG.error("index-resync pending record %s unreadable: %s", path.name, exc)
    return out


ADVISORY_DIRNAME = "coordinator-index-resync-advisory"


def index_lock_present(worktree_root: Path) -> bool:
    """True iff the clone's ``index.lock`` exists now; a peer is then plausibly mid-write."""
    try:
        return (_git_dir(worktree_root) / "index.lock").exists()
    except (OSError, ValueError):
        return False


def record_advisory(
    worktree_root: Path,
    *,
    candidate_id: str,
    op_label: str,
    reason: str,
    recorded_at: str,
) -> bool:
    """Write one advisory file for a lock-contended resync; returns the lock-holder-live observation.

    Advisory state lives beside the pending records under the git dir, is never committed,
    and is keyed by (op_label, candidate_id) so a repeat overwrites. Not read by the drain;
    the only reader is an operator inspecting the git dir, and nothing prunes it on a later
    successful resync.
    """
    holder_live = index_lock_present(worktree_root)
    try:
        directory = _git_dir(worktree_root) / ADVISORY_DIRNAME
        directory.mkdir(parents=True, exist_ok=True)
        name = hashlib.sha1(f"{op_label}\0{candidate_id}".encode("utf-8")).hexdigest()[:16] + ".json"
        payload = json.dumps(
            {
                "candidate_id": candidate_id,
                "op_label": op_label,
                "reason": reason,
                "recorded_at": recorded_at,
                "index_lock_present_at_record": holder_live,
            },
            sort_keys=True,
        ).encode("utf-8")
        atomic_write_bytes(directory / name, payload)
    except (OSError, ValueError) as exc:
        _LOG.error("index-resync advisory record write failed: %s", exc)
    return holder_live


def discharge(worktree_root: Path, rec: PendingResync) -> None:
    """Remove the record's file; an already-missing file is fine."""
    try:
        (pending_dir(worktree_root) / _record_name(rec)).unlink(missing_ok=True)
    except (OSError, ValueError) as exc:
        _LOG.error("index-resync pending discharge failed: %s", exc)
