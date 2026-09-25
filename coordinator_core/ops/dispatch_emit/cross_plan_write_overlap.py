"""Refuse to emit a plan whose declared ``writes:`` overlap another LIVE
plan's in this shared tree.

Checked at emit, not at ``claim-plan``: a claimed plan's spine can change
before it runs, and emit is where dispatchable work is produced. Refuses
rather than warns, because a warning does not stop one plan's wave landing
over another's.

No git spawn: the common dir is resolved from path reads, and each live peer
claim costs one ``read_spine``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.claim_state import _sessions_dir
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.ops.dispatch_emit.spine_read import UNDECLARED, read_spine
from coordinator_core.ops.dispatch_emit.wave_map import _normalize_path
from coordinator_core.session.claimed_plan import _resolve_plan_slug_path
from coordinator_core.session.liveness import claim_holder_live

#: Mirrors `ops.fleet._common._CLAIM_SUBDIRS[2]`; importing `_common` pulls
#: `coordinator_core.ops`'s eager import sweep.
_PLAN_CLAIMS_SUBDIR = "plan-claims"


class CrossPlanWriteOverlap(ValueError):
    """Raised when this plan's declared ``writes:`` overlap a LIVE peer
    plan's -- both would-be dispatchable at once in the same shared tree."""


def _write_paths_from_rows(rows) -> set:
    """Union of every row's normalized ``writes:``. An UNDECLARED row
    contributes nothing -- absent ``writes:`` never means "collides with
    everything"."""
    paths: set = set()
    for row in rows:
        writes = getattr(row, "writes", UNDECLARED)
        if writes is UNDECLARED or not isinstance(writes, list):
            continue
        paths.update(_normalize_path(p) for p in writes if isinstance(p, str) and p)
    return paths


def _declared_write_paths(plan_path: Path) -> set:
    """A peer plan's writes, read fresh."""
    try:
        rows = read_spine(plan_path)
    except Exception:
        # An unparseable peer (mid-edit, moved) is not evidence of a
        # collision: fail open on that peer only.
        return set()
    return _write_paths_from_rows(rows)


def _live_peer_plan_claims(common_dir: Path, exclude_slug: str, cwd: str) -> list:
    """Live plan-claim dirs other than ``exclude_slug``. Liveness is the
    reaper's own key, so a dead holder's stale claim never blocks."""
    base = _sessions_dir(common_dir) / _PLAN_CLAIMS_SUBDIR
    if not base.is_dir():
        return []
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return []
    live: list = []
    for entry in entries:
        if not entry.is_dir() or entry.name == exclude_slug:
            continue
        try:
            if claim_holder_live(str(entry), cwd):
                live.append(entry)
        except (OSError, ValueError):
            continue
    return live


def check_cross_plan_write_overlap(
    plan_path, rows, repo_root: Optional[Path]
) -> None:
    """Raise ``CrossPlanWriteOverlap`` if ``rows``' writes overlap a live
    peer plan's. ``rows`` is the caller's parsed spine; ``plan_path`` is
    never re-read."""
    if repo_root is None:
        return
    this_writes = _write_paths_from_rows(rows)
    if not this_writes:
        return

    repo_root = Path(repo_root)
    common_dir = resolve_git_common_dir(repo_root)
    this_slug = Path(plan_path).stem
    cwd = str(repo_root)

    for claim_dir in _live_peer_plan_claims(common_dir, this_slug, cwd):
        other_rel = _resolve_plan_slug_path(str(repo_root), claim_dir.name)
        other_writes = _declared_write_paths(repo_root / other_rel)
        overlap = sorted(str(p) for p in (this_writes & other_writes))
        if overlap:
            raise CrossPlanWriteOverlap(
                f"writes: overlap with live plan '{other_rel}': "
                f"{', '.join(overlap)}. Narrow one plan's writes:, or "
                "emit after it finishes."
            )
