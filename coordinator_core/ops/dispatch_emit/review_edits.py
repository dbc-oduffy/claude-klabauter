"""Review-stage edits outside a run's declared writes: land them as review residue or halt.

``partition`` answers which reviewer-written dirty files the run's one commit may land and which
must halt it. ``review_writes`` is the lazy delegate to the touch-record reader.
``live_sibling_declared`` names the files another live run declared, which that run lands.
Stdlib only at module top; no git spawn.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from coordinator_core.session.record_homes import home_dir

HALT_PEER_CONTESTED = "peer-contested"
HALT_DELETED = "deleted"
HALT_OUT_OF_WORKTREE = "out-of-worktree"


@dataclass(frozen=True)
class ReviewEditVerdict:
    """``land`` and ``halt`` keys are repo-relative forward-slash paths; ``land`` is sorted.
    ``unanswerable`` is None, or why the review-write source could not answer."""

    land: tuple[str, ...]
    halt: dict[str, str]
    unanswerable: Optional[str]


def norm(path: str) -> str:
    """Forward-slash form of ``path`` with leading ``./`` removed."""
    text = str(path).replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def out_of_worktree(path: str) -> bool:
    """True for a normalised path that is absolute, drive-qualified or climbs out via ``..``."""
    return (
        path.startswith("/")
        or path.startswith("../")
        or path == ".."
        or (len(path) > 1 and path[1] == ":")
    )


def review_writes(
    worktree_root: Path, session_id: Optional[str], since: Optional[float]
) -> Optional[dict[str, str]]:
    """path -> agent id of every review-stage write since ``since``; None when unanswerable."""
    from coordinator_core.ops.dispatch_emit import review_edit_source

    return review_edit_source.session_review_writes(worktree_root, session_id, since)


def live_sibling_declared(
    worktree_root: Path, own_run_id: Optional[str], own_prefix: Optional[str] = None
) -> dict[str, str]:
    """path -> run id for every other live run's manifest ``review_declared_paths``.
    ``own_prefix`` (a slice prefix) additionally marks any run whose id it contains as own.
    An unreadable manifest contributes nothing."""
    from coordinator_core.ops.dispatch_emit import abandoned_runs

    out: dict[str, str] = {}
    try:
        records = abandoned_runs._token_records(worktree_root)
    except Exception:
        return out
    for record in records:
        run_id = record.get("run_id")
        if not isinstance(run_id, str) or run_id == own_run_id or (own_prefix and run_id in own_prefix):
            continue
        manifest = Path(home_dir(str(worktree_root), "warp")) / run_id / "manifest.json"
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        paths = data.get("review_declared_paths") if isinstance(data, dict) else None
        if not isinstance(paths, list):
            continue
        for p in paths:
            if isinstance(p, str) and p:
                out.setdefault(norm(p), run_id)
    return out


def partition(
    writes: Optional[dict[str, str]],
    *,
    changed: set,
    ignored: Sequence[str],
    committed: set,
    sibling_declared: dict[str, str],
    contested: set,
    present: Callable[[str], bool],
) -> ReviewEditVerdict:
    """Split review-written paths into ``land`` and ``halt``. A committed, clean, ignored or
    sibling-declared write is neither."""
    if writes is None:
        return ReviewEditVerdict(land=(), halt={}, unanswerable="review-write source unanswerable")
    changed_n = {norm(p) for p in changed}
    ignored_n = {norm(p) for p in ignored}
    committed_n = {norm(p) for p in committed}
    sibling_n = {norm(p) for p in sibling_declared}
    contested_n = {norm(p) for p in contested}
    land: list[str] = []
    halt: dict[str, str] = {}
    for raw in writes:
        path = norm(raw)
        if out_of_worktree(path):
            halt[path] = HALT_OUT_OF_WORKTREE
            continue
        if path in committed_n or path in ignored_n or path in sibling_n:
            continue
        if path not in changed_n:
            continue
        if path in contested_n:
            halt[path] = HALT_PEER_CONTESTED
        elif not present(path):
            halt[path] = HALT_DELETED
        else:
            land.append(path)
    return ReviewEditVerdict(land=tuple(sorted(set(land))), halt=dict(sorted(halt.items())), unanswerable=None)
