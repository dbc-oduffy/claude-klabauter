"""
coordinator_core.ops.cascade_redrive — in-process redrive of terminal sources whose cascade never fired.

Purpose: a plan hand-written `status: implemented`, or a handoff archived by a path other than the
cascade's own trigger, leaves its joined baton open. This module finds those divergences with the
backstop sweep's own collectors and the cascade's own per-target predicate, then advances each one
through `cascade_terminal_sync`, citing the terminal source as the decision event (DR-263 §4).

Not an op: no JSON-RPC registration, no authz entry. `deliverable.cascade_backstop_sweep` stays
read-only; every write here is `cascade_terminal_sync`'s.

`redrive_terminal_divergences` never raises; one deliverable's failure lands in `errors` and the rest
proceed. The no-divergence path reads frontmatter in-process and spawns no git process.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Optional

from coordinator_core.dag import _read_meta
from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT
from coordinator_core.ops.cascade_backstop_sweep import (
    _landed_parked_plans,
    _read_fm,
    _terminal_handoff_deliverable_ids,
    _terminal_plan_deliverable_ids,
)
from coordinator_core.ops.deliverable_cascade import (
    _HANDOFF_KIND,
    _collect_live_candidates_for_kind,
    _predicate_refusal_sync,
    cascade_terminal_sync,
)
from coordinator_core.ops.fleet._common import main_worktree_root


def landed_parked(worktree_root: Path, *, days: int = 14, today: Optional[date] = None) -> list[dict]:
    """Plans at `status: landed` older than `days`: [{"plan", "deliverable_id", "age_days", "age_basis"}]."""
    return _landed_parked_plans(worktree_root, days, today)


def _handoff_side(handoff_metas: Optional[dict[str, dict]], worktree_root: Path) -> dict[str, list[str]]:
    if handoff_metas is None:
        return _terminal_handoff_deliverable_ids(worktree_root)
    out: dict[str, list[str]] = {}
    for rel, meta in sorted(handoff_metas.items()):
        if not isinstance(meta, dict) or meta.get("deployment_state") not in HANDOFF_TERMINAL_DEPLOYMENT:
            continue
        did = meta.get("deliverable_id")
        if isinstance(did, str) and did.strip():
            out.setdefault(did.strip(), []).append(rel)
    return out


def _ship_sha(rel: str, is_plan: bool, handoff_metas: Optional[dict[str, dict]], worktree_root: Path) -> str:
    if is_plan:
        fm = _read_fm(worktree_root / rel)
    elif handoff_metas is not None and rel in handoff_metas:
        fm = handoff_metas[rel]
    else:
        try:
            fm = _read_meta(str(worktree_root / rel)) or {}
        except Exception:  # noqa: BLE001 — unreadable source: no sha, the cascade refuses by name
            fm = {}
    value = fm.get("shipped_in")
    return value.strip() if isinstance(value, str) else ""


def _clears_predicate(worktree_root: Path, common_dir: Path, did: str) -> tuple[bool, bool]:
    """(some live candidate clears the per-target predicate, scan incomplete)."""
    metas: dict[str, dict] = {}
    candidates, incomplete, _ = _collect_live_candidates_for_kind(
        worktree_root, did, kind=_HANDOFF_KIND, metas_out=metas
    )
    for candidate in candidates:
        refusal = _predicate_refusal_sync(
            candidate["path"], candidate["fm"], common_dir, kind=_HANDOFF_KIND, corpus_metas=metas
        )
        if refusal is None:
            return True, incomplete
    return False, incomplete


def redrive_terminal_divergences(
    common_dir: Path,
    *,
    deliverable_ids: Optional[set[str]] = None,
    handoff_metas: Optional[dict[str, dict]] = None,
) -> dict:
    """Advance the live handoffs a terminal source left open; one `cascade_terminal_sync` per deliverable.

    `common_dir` is the git common dir. `deliverable_ids` restricts the redrive to those join keys.
    `handoff_metas` ({handoff relpath at its current location: frontmatter carrying
    deployment_state, deliverable_id, shipped_in}) replaces the archive-wide handoff scan.
    Returns {"advanced", "refused", "commit_shas", "scan_incomplete", "errors"}.
    """
    result: dict = {"advanced": [], "refused": [], "commit_shas": [], "scan_incomplete": False, "errors": []}
    try:
        worktree_root = main_worktree_root(common_dir)
        plan_ids = _terminal_plan_deliverable_ids(worktree_root)
        handoff_ids = _handoff_side(handoff_metas, worktree_root)
    except Exception as exc:  # noqa: BLE001 — the redrive never raises into its caller
        result["errors"].append({"deliverable_id": None, "error": f"scan failed: {exc}"})
        result["scan_incomplete"] = True
        return result

    sources: dict[str, list[str]] = {}
    for ids in (plan_ids, handoff_ids):
        for did, rels in ids.items():
            sources.setdefault(did, []).extend(rels)
    if deliverable_ids is not None:
        sources = {did: rels for did, rels in sources.items() if did in deliverable_ids}

    for did in sorted(sources):
        try:
            clears, incomplete = _clears_predicate(worktree_root, common_dir, did)
            if incomplete:
                result["scan_incomplete"] = True
            if not clears:
                continue
            rel = sorted(sources[did])[0]
            is_plan = rel in plan_ids.get(did, [])
            ship_sha = _ship_sha(rel, is_plan, handoff_metas, worktree_root)
            # source_kind "handoff" makes the cascade stamp the source's last-touching commit,
            # which for an archived handoff is the archive commit; a recorded shipped_in must win,
            # and only the "plan" kind skips that resolution.
            source_kind = "plan" if is_plan or ship_sha else "handoff"
            cascade = cascade_terminal_sync(
                {
                    "deliverable_id": did,
                    "source_kind": source_kind,
                    "source_path": rel,
                    "ship_sha": ship_sha,
                },
                repo_root=common_dir,
            )
        except Exception as exc:  # noqa: BLE001 — one deliverable's failure must not stop the rest
            result["errors"].append({"deliverable_id": did, "error": str(exc)})
            continue
        for entry in cascade.get("advanced", []):
            result["advanced"].append({"deliverable_id": did, "source": rel, **entry})
        for entry in cascade.get("refused", []):
            result["refused"].append({"deliverable_id": did, "source": rel, **entry})
        if cascade.get("scan_incomplete"):
            result["scan_incomplete"] = True
        if cascade.get("commit_sha"):
            result["commit_shas"].append(cascade["commit_sha"])
        if cascade.get("commit_error"):
            result["errors"].append({"deliverable_id": did, "error": cascade["commit_error"]})
    return result
