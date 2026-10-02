"""
coordinator_core.ops.roadmap_blitz_stage — JSON-RPC "roadmap.blitz_stage" operation.

Purpose: thin RPC wrapper over ``coordinator_core.roadmap.blitz_stage``, Phase 2
of a roadmap blitz: scaffold and number the roadmap's stubs, report their size
against the M/L band, run audit-roadmap, and freeze the gate report plan-blitz
consumes.

Wire params:
    roadmap      (str, required) — the roadmap directory under ``state/roadmap/`` or
                                    any file in it (OVERVIEW.md, clusters.md).
    edges_path   (str, optional) — an edges file (``A <- B`` lines) that overrides
                                    the edges derived from ``clusters.md``.
    branch       (str, optional) — branch recorded on each stub (default "main").
    commit       (bool, optional) — commit the scaffolded stubs, then the frozen gate report,
                                    through ``ceremony.commit_v2``'s handler with explicit
                                    paths (default true). False leaves both uncommitted and
                                    defers the gate report until the stubs are tracked.

Reply fields:
    {"gate_report_path", "gate_report_state", "stubs": [...], "numbering": {...},
     "cluster_numbering": {...}, "folds": {merged, split, flagged, unsized, in_band, routes,
     rule, ambiguities}, "audit": {...}, "commits": [{sha, paths}], "commit_error",
     "roadmap_id", "edge_source"}

Negative-spec:
  - Commits only the paths it wrote, never a sweep. With ``commit: false`` the gate report is
    deferred (``gate_report_path: null``) until the staged stubs are tracked; commit, then re-run.
  - Does NOT spawn a process.
  - Does NOT re-scaffold a staged stub or rewrite a frozen gate report.

Spec backlink: coordinator-content-repo state/memos/2026-10-02-example-stats-repo-roadmap-blitz-requirements.md items 6-7.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.roadmap.blitz_stage import BlitzStageRefused, stage_roadmap

GENERATES: list = []


def _optional_str(params: dict, key: str) -> Optional[str]:
    value = params.get(key)
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ValueError(f"{key} must be a non-empty string when supplied")
    return value


def _commit_flag(params: dict) -> bool:
    value = params.get("commit", True)
    if not isinstance(value, bool):
        raise ValueError("commit must be a boolean when supplied")
    return value


@register_op("roadmap.blitz_stage")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "roadmap.blitz_stage" handler. See module docstring."""
    if repo_root is None:
        raise ValueError("roadmap.blitz_stage requires a resolved repo_root")
    roadmap = _optional_str(params, "roadmap")
    if roadmap is None:
        raise ValueError("roadmap is required")
    try:
        return stage_roadmap(
            Path(main_worktree_root(repo_root)),
            roadmap,
            edges_path=_optional_str(params, "edges_path"),
            branch=_optional_str(params, "branch") or "main",
            commit=_commit_flag(params),
        )
    except BlitzStageRefused as exc:
        raise ValueError(str(exc)) from exc
