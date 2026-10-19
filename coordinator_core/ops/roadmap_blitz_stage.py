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
    interaction_mode (str, optional) — the run's mode, recorded on each sizing minted.
    commit       (bool, optional) — commit the scaffolded stubs, then the frozen gate report,
                                    through ``ceremony.commit_v2``'s handler with explicit
                                    paths (default true). False leaves both uncommitted and
                                    defers the gate report until the stubs are tracked.
    approval     (object, optional) — {verdict: "approved"|"declined", approver: non-empty str,
                                    exactly one of utterance (str) or ruling (str),
                                    ruling_ref (str, optional)}.
    trail_dir    (str, required with approval) — a repo-relative forward-slash dir under
                                    scratch/; refused when absolute, containing "..", or
                                    outside scratch/. Refused without an approval.

Reply fields:
    {"gate_report_path", "gate_report_state", "stubs": [...], "numbering": {...},
     "cluster_numbering": {...}, "folds": {merged, split, flagged, unsized, in_band, routes,
     rule, ambiguities}, "audit": {...}, "commits": [{sha, paths}], "commit_error",
     "roadmap_id", "edge_source",
     "approval_record": {path, recorded_at} | null, "overview_stamped": bool,
     "declined": bool (true means nothing was staged),
     "held": [{stub_id, path, reason}] — stubs an APM hold (plan_blitz_hold_reason without a
       clearance) keeps out of every wave; each also carries held/hold_reason in "stubs",
     "waves": [{index, batons: [{id, path, title, sized, planPath, executionOpen, tshirt,
       route, sizingObject}]}] — copied from the frozen plan_gate report in order, never
       regrouped; empty when the gate report was deferred}
    The approval fields appear only when an approval was supplied. An approved call stamps
    OVERVIEW.md (status, approver, approval_utterance) and commits the roadmap directory's
    changed files with the stubs.

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


def _nonempty(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _approval(raw: object) -> Optional[dict]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("approval must be an object when supplied")
    if raw.get("verdict") not in ("approved", "declined"):
        raise ValueError("approval.verdict must be 'approved' or 'declined'")
    _nonempty(raw.get("approver"), "approval.approver")
    present = [k for k in ("utterance", "ruling") if raw.get(k) is not None]
    if len(present) != 1:
        raise ValueError("approval needs exactly one of utterance or ruling")
    _nonempty(raw[present[0]], f"approval.{present[0]}")
    if raw.get("ruling_ref") is not None:
        _nonempty(raw["ruling_ref"], "approval.ruling_ref")
    return raw


def _trail_dir(raw: object) -> str:
    if raw is None:
        raise ValueError("trail_dir is required with approval")
    value = _nonempty(raw, "trail_dir")
    parts = value.split("/")
    if "\\" in value or value.startswith("/") or (len(value) > 1 and value[1] == ":"):
        raise ValueError("trail_dir must be a repo-relative forward-slash path")
    if ".." in parts:
        raise ValueError("trail_dir must not contain '..'")
    if parts[0] != "scratch" or len(parts) < 2 or not parts[1]:
        raise ValueError("trail_dir must be under scratch/")
    return value


@register_op("roadmap.blitz_stage")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "roadmap.blitz_stage" handler. See module docstring."""
    if repo_root is None:
        raise ValueError("roadmap.blitz_stage requires a resolved repo_root")
    roadmap = _optional_str(params, "roadmap")
    if roadmap is None:
        raise ValueError("roadmap is required")
    kwargs: dict = {
        "edges_path": _optional_str(params, "edges_path"),
        "branch": _optional_str(params, "branch") or "main",
        "commit": _commit_flag(params),
        "interaction_mode": _optional_str(params, "interaction_mode"),
    }
    approval = _approval(params.get("approval"))
    if approval is not None:
        kwargs["approval"] = approval
        kwargs["trail_dir"] = _trail_dir(params.get("trail_dir"))
    elif params.get("trail_dir") is not None:
        raise ValueError("trail_dir is only accepted with approval")
    try:
        return stage_roadmap(Path(main_worktree_root(repo_root)), roadmap, **kwargs)
    except BlitzStageRefused as exc:
        raise ValueError(str(exc)) from exc
