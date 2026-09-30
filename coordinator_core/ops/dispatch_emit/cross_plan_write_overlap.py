
"""Detects overlapping declared `writes:` scope between the emitting plan and any live peer plan claim, so two concurrently dispatched plans can't both claim ownership of the same paths."""

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
    pass


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
    try:
        rows = read_spine(plan_path)
    except Exception:
        return set()
    return _write_paths_from_rows(rows)


def _claim_session_id(claim_dir: Path) -> str:
    try:
        return (claim_dir / "session_id").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _live_peer_plan_claims(
    common_dir: Path, exclude_slug: str, cwd: str, own_session_id: Optional[str] = None
) -> list:
    """Live plan claims held by OTHER sessions. A claim this session holds
    (plan-blitz planners claim while authoring, and it lingers after landing)
    is never a peer."""
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
        if own_session_id and _claim_session_id(entry) == own_session_id:
            continue
        try:
            if claim_holder_live(str(entry), cwd):
                live.append(entry)
        except (OSError, ValueError):
            continue
    return live


def check_cross_plan_write_overlap(
    plan_path, rows, repo_root: Optional[Path], session_id: Optional[str] = None
) -> None:
    if repo_root is None:
        return
    this_writes = _write_paths_from_rows(rows)
    if not this_writes:
        return

    repo_root = Path(repo_root)
    common_dir = resolve_git_common_dir(repo_root)
    this_slug = Path(plan_path).stem
    cwd = str(repo_root)

    for claim_dir in _live_peer_plan_claims(common_dir, this_slug, cwd, session_id):
        other_rel = _resolve_plan_slug_path(str(repo_root), claim_dir.name)
        other_writes = _declared_write_paths(repo_root / other_rel)
        overlap = sorted(str(p) for p in (this_writes & other_writes))
        if overlap:
            raise CrossPlanWriteOverlap(
                f"writes: overlap with live plan '{other_rel}': "
                f"{', '.join(overlap)}. Narrow one plan's writes:, or "
                "emit after it finishes."
            )
