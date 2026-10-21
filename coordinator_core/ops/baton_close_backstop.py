"""coordinator_core.ops.baton_close_backstop: "deliverable.baton_close_backstop" op.

Purpose: report, per open baton in one repo's ``state/handoffs/``, whether on-disk evidence
(sizing, warp run record, plan, successor, merged branch) says the work finished, and whether
the DR-263 per-target predicate would let a closer act on it. ``divergences`` are what a closer
consumes; ``residue`` is for agent review.

NEGATIVE-SPEC:
  - Never writes: no baton, sizing, plan, run record or archive move, under any outcome. A scan
    conclusion is inference and stays off the write path (DR-263 section 6; reversal only through
    its section 4 revisit trigger).
  - Never re-fires the cascade: no ``_advance_one``, ``stamp_shipped_in`` or ``locked_rmw``.
  - Not wired to any hook, cron, boot sweep, commit path or slash command; DoE invokes it
    through the engine door, one repo per call.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, Optional

from coordinator_core.dag import _read_meta
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT
from coordinator_core.ops.baton_close_evidence_chain import (
    merged_branch_evidence,
    merged_branches,
    successor_evidence,
)
from coordinator_core.ops.baton_close_evidence_plans import plan_evidence
from coordinator_core.ops.baton_close_evidence_records import run_record_evidence, sizing_evidence
from coordinator_core.ops.baton_close_predicate import SCHEMA_VERSION, decide
from coordinator_core.ops.deliverable_cascade import (
    _HANDOFF_KIND,
    _leg_d_refusal,
    _legs_c_a_refusal,
    _PredicateLeg,
)
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.session import record_homes

OP_KEY = "deliverable.baton_close_backstop"

# Leg (d) refuses every `kind: spinoff` because a spinoff's inherited deliverable_id is not its
# parent's. This op joins on evidence, not deliverable_id, so the unchanged descriptor would send
# every spinoff to residue.
_EVIDENCE_HANDOFF_KIND = dataclasses.replace(
    _HANDOFF_KIND,
    predicate_legs={
        **_HANDOFF_KIND.predicate_legs,
        "d": _PredicateLeg(applies=False, reason="evidence join, not a deliverable_id join"),
    },
)


def _predicate_refusal_without_leg_b(path: Path, fm: dict, repo_root: Path) -> str | None:
    """DR-263 legs c, a and d. Leg b (live successor) is answered by SUCCESSOR_LIVE evidence,
    which ``decide`` already routes to residue; running it per candidate rebuilds the reverse
    edge index every time (about 33ms each)."""
    return _legs_c_a_refusal(path, fm, repo_root, _EVIDENCE_HANDOFF_KIND, "") or _leg_d_refusal(
        fm, _EVIDENCE_HANDOFF_KIND
    )


def _live_batons(worktree_root: Path) -> tuple[dict[str, dict], list[str]]:
    """Return ``(batons, unreadable)``: repo-relative posix path to frontmatter for each readable
    non-terminal baton, and the paths whose frontmatter did not read."""
    batons: dict[str, dict] = {}
    unreadable: list[str] = []
    base = Path(record_homes.home_dir(str(worktree_root), "handoffs"))
    if not base.is_dir():
        return batons, unreadable
    for path in sorted(base.rglob("*.md")):
        fm = _read_meta(str(path))
        rel = path.relative_to(worktree_root).as_posix()
        if not fm:
            unreadable.append(rel)
        elif fm.get("deployment_state") not in HANDOFF_TERMINAL_DEPLOYMENT:
            batons[rel] = fm
    return batons, unreadable


def _merge(*maps: dict[str, list[dict]]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for m in maps:
        for rel, items in m.items():
            out.setdefault(rel, []).extend(items)
    return out


@register_op(OP_KEY)
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "deliverable.baton_close_backstop" handler (read-only).

    Optional param ``handoff`` (repo-relative path) restricts the report to one baton; the
    corpora are still read once.

    Returns ``{"exit_code": 0, "schema_version": 1, "op", "batons_scanned", "no_evidence",
    "divergences": [{"handoff_path", "proposes", "evidence"}],
    "residue": [{"handoff_path", "reason", "evidence"}],
    "legs": {"merged_branch": "ran" | "skipped: <why>"}, "scan_incomplete"}``.
    ``exit_code`` is 0 on every completed scan; an empty list is the clean result.
    """
    if repo_root is None:
        return {
            "exit_code": 1,
            "error": "deliverable.baton_close_backstop: repo_root is required (no founding root available)",
        }

    worktree_root = main_worktree_root(repo_root)
    only = (params.get("handoff") or "").replace("\\", "/").strip() or None

    batons, unreadable = _live_batons(worktree_root)
    merged, merged_status = merged_branches(worktree_root)
    evidence = _merge(
        sizing_evidence(worktree_root, batons),
        run_record_evidence(worktree_root, batons),
        plan_evidence(worktree_root, batons),
        successor_evidence(worktree_root, batons),
        merged_branch_evidence(batons, merged),
    )

    divergences: list[dict[str, Any]] = []
    residue: list[dict[str, Any]] = []
    no_evidence = 0

    unread = [rel for rel in unreadable if only is None or rel == only]
    for rel in unread:
        residue.append({"handoff_path": rel, "reason": "unreadable frontmatter", "evidence": []})

    scanned = 0
    for rel in sorted(batons):
        if only is not None and rel != only:
            continue
        scanned += 1
        fm = batons[rel]
        items = evidence.get(rel, [])
        refusal = None
        if any(e["reads"] == "terminal" and e["strength"] == "strong" for e in items):
            refusal = _predicate_refusal_without_leg_b(worktree_root / rel, fm, repo_root)
        verdict = decide(rel, fm, items, refusal)
        if verdict["verdict"] == "no_evidence":
            no_evidence += 1
        elif verdict["verdict"] == "divergence":
            divergences.append(
                {"handoff_path": rel, "proposes": verdict["proposes"], "evidence": items}
            )
        else:
            residue.append({"handoff_path": rel, "reason": verdict["reason"], "evidence": items})

    return {
        "exit_code": 0,
        "schema_version": SCHEMA_VERSION,
        "op": OP_KEY,
        "batons_scanned": scanned + len(unread),
        "no_evidence": no_evidence,
        "divergences": divergences,
        "residue": residue,
        "legs": {"merged_branch": merged_status},
        "scan_incomplete": not Path(record_homes.home_dir(str(worktree_root), "handoffs")).is_dir(),
    }
