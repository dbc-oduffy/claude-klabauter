"""Batched terminal write for `baton.close_commit`: flips each requested baton through the
cascade's own `_flip_handoff` and names exactly the files it rewrote.

Never commits and spawns nothing; the caller lands `pathspec` with `commit_subject` as one
scoped commit. Each flip carries the `shipped_in` its evidence justified; HEAD is never read.
"""

from pathlib import Path
from typing import NamedTuple, Optional

from coordinator_core.ops.baton_close_commit_contract import (
    AlreadyTerminalItem,
    AppliedItem,
    FlipRequest,
    RefusalReason,
    RefusedItem,
)
from coordinator_core.ops.cascade_terminal_op import _flip_handoff
from coordinator_core.ops.deliverable_cascade import _ALREADY_ADVANCED_MARKER, _iso_now

_SHIPPED_IN_KIND = "successor"


class FlipOutcome(NamedTuple):
    """`already_terminal` trails the four contracted fields so a caller can read it by name."""

    applied: list[AppliedItem]
    refused: list[RefusedItem]
    pathspec: list[str]
    commit_subject: Optional[str]
    already_terminal: list[AlreadyTerminalItem]


def _refusal(path: str, detail: str) -> RefusedItem:
    return {
        "handoff_path": path,
        "reason": RefusalReason.WRITE_FAILED.value,
        "detail": detail,
        "evidence": [],
    }


def _relative_posix(path: Path, worktree: Path) -> str:
    try:
        return path.resolve().relative_to(worktree.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def flip_batch(flips: list[FlipRequest], common_dir: Path, worktree: Path) -> FlipOutcome:
    """Make every requested baton terminal (`shipped`, kind `successor`, `advanced_by` its own
    deliverable_id). `pathspec` holds only files `_flip_handoff` reported advanced; a peer-won
    race is `already_terminal`, any other failure a `write_failed` refusal. A flip without a
    `shipped_in` is refused and writes nothing."""
    advanced_at = _iso_now()

    applied: list[AppliedItem] = []
    refused: list[RefusedItem] = []
    already_terminal: list[AlreadyTerminalItem] = []
    changed: set[str] = set()
    for flip in flips:
        shipped_in = flip.get("shipped_in")
        if not shipped_in:
            refused.append(
                {
                    "handoff_path": flip["path"],
                    "reason": RefusalReason.NO_EVIDENCE_SHA.value,
                    "detail": "flip carries no shipped_in",
                    "evidence": [],
                }
            )
            continue
        path = Path(flip["path"])
        if not path.is_absolute():
            path = worktree / path
        rel = _relative_posix(path, worktree)
        did_advance, reason = _flip_handoff(
            path,
            flip["deliverable_id"],
            advanced_at,
            shipped_in,
            common_dir,
            shipped_in_kind=_SHIPPED_IN_KIND,
        )
        if did_advance:
            changed.add(rel)
            applied.append(
                {
                    "handoff_path": flip["path"],
                    "advanced_by": flip["deliverable_id"],
                    "advanced_at": advanced_at,
                    "shipped_in": shipped_in,
                }
            )
        elif reason == _ALREADY_ADVANCED_MARKER:
            already_terminal.append({"handoff_path": flip["path"]})
        else:
            refused.append(_refusal(flip["path"], reason or "write failed"))

    pathspec = sorted(changed)
    subject = f"baton.close_commit: {len(pathspec)} baton(s) terminal" if pathspec else None
    return FlipOutcome(applied, refused, pathspec, subject, already_terminal)
