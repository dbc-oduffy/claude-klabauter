"""JSON-RPC "baton.close_report": read-only report of which open batons can close.

One header pass over state/handoffs/, cited-target status reads, a pure per-baton decision.
Reports closable batons with evidence and the residue it could not decide; writes nothing,
spawns nothing, parses no YAML. Closing stays the closer's act under DR-263.

Self-registration: importing this module registers the op; ops/__init__.py imports it eagerly.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Optional

from coordinator_core import dag
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import git_common_dir, main_worktree_root
from coordinator_core.ops.baton_close_report_contract import (
    OP_KEY,
    SCHEMA_VERSION,
    Evidence,
    EvidenceClass,
    NoEvidenceReason,
    ResidueReason,
    Verdict,
)
from coordinator_core.ops.baton_close_plan_delivery import PlanDeliveryCache
from coordinator_core.ops.baton_close_report_decide import (
    STATUS_MISSING,
    STATUS_UNREADABLE,
    decide,
    evidence_ship_ref,
)
from coordinator_core.ops.baton_close_report_readers import read_baton_headers, successor_index
from coordinator_core.ops.baton_close_report_targets import target_evidence
from coordinator_core.ops.deliverable_cascade import _LIVE_DEPLOYMENT_STATES, handoff_state_refusal
from coordinator_core.session import record_homes


_ROUTE = "clear the gate, then handoff.transition to ready_to_fire, then re-run close_report"
_NO_SHA_DETAIL = "no delivery commit in the evidence that justifies this close"


def _norm(rel: str) -> str:
    rel = rel.strip().replace("\\", "/")
    return rel[2:] if rel.startswith("./") else rel


def _coded_plan(plans: PlanDeliveryCache, items: list[Evidence]) -> tuple[str, str] | None:
    """(plan path, last commit) of the first cited plan whose dispatchable rows are all coded, else None."""
    for item in items:
        if item["class"] != EvidenceClass.PLAN.value or item["target_status"] in (None, STATUS_MISSING, STATUS_UNREADABLE):
            continue
        delivery = plans.get(item["target_path"])
        if delivery is not None and not delivery.open_rows and delivery.ship_ref:
            return item["target_path"], delivery.ship_ref
    return None


def _state_refusal(worktree_root: str, rel: str, header: dict) -> str | None:
    """Leg (c) text for a baton whose `deployment_state` is not live, else None.

    Only such batons are read and parsed; live ones skip the file read.
    """
    if header.get("deployment_state") in _LIVE_DEPLOYMENT_STATES:
        return None
    try:
        with open(os.path.join(worktree_root, *rel.split("/")), "rb") as fh:
            text = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    return handoff_state_refusal(dag._parse_frontmatter(text))


@register_op("baton.close_report")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Report closable batons, residue and the no-evidence batons for one repo.

    Optional param `handoff` (repo-relative path) narrows the report to that one baton.
    Returns {exit_code, schema_version, op, batons_scanned, no_evidence, no_evidence_batons,
    closable, residue, scan_incomplete}; `no_evidence_batons` names each no-evidence baton with
    a reason (nothing_cited, targets_not_done or acs_all_ticked) and `no_evidence` is its length;
    `scan_incomplete` is true when the handoffs directory is missing.
    """
    return await asyncio.to_thread(_run, params, repo_root)


def _run(params: dict, repo_root: Optional[Path]) -> dict:
    if repo_root is None:
        return {"exit_code": 1, "error": f"{OP_KEY}: repo_root is required (no founding root available)"}

    worktree_root = str(main_worktree_root(git_common_dir(repo_root)))
    only = str(params.get("handoff") or "").strip()
    only = _norm(only) if only else None

    scan_incomplete = not os.path.isdir(record_homes.home_dir(worktree_root, "handoffs"))
    headers, unreadable = read_baton_headers(worktree_root)
    live_successor = successor_index(headers)
    evidence = target_evidence(worktree_root, headers)

    closable: list[dict] = []
    residue: list[dict] = []
    no_evidence_batons: list[dict] = []
    plans: PlanDeliveryCache | None = None
    scanned = 0

    for rel in unreadable:
        if only is not None and rel != only:
            continue
        scanned += 1
        residue.append(
            {"handoff_path": rel, "reason": ResidueReason.UNREADABLE_FRONTMATTER.value, "evidence": [],
             "conflict": None, "detail": None}
        )

    for rel, header in headers.items():
        if only is not None and rel != only:
            continue
        scanned += 1
        state_refusal = _state_refusal(worktree_root, rel, header)
        verdict = decide(header, evidence.get(rel, []), rel in live_successor, state_refusal)
        if verdict[0] == Verdict.CLOSABLE.value:
            ship_ref = evidence_ship_ref(verdict[1])
            if ship_ref is None:
                residue.append({
                    "handoff_path": rel,
                    "reason": ResidueReason.NO_EVIDENCE_SHA.value,
                    "evidence": verdict[1],
                    "conflict": verdict[2],
                    "detail": _NO_SHA_DETAIL,
                })
            else:
                closable.append(
                    {"handoff_path": rel, "evidence": verdict[1], "conflict": verdict[2], "ship_ref": ship_ref}
                )
        elif verdict[0] == Verdict.RESIDUE.value:
            detail = None
            if verdict[1] == ResidueReason.NOT_ADVANCEABLE.value:
                detail = f"{state_refusal}; {_ROUTE}"
            residue.append(
                {"handoff_path": rel, "reason": verdict[1], "evidence": verdict[2],
                 "conflict": verdict[3], "detail": detail}
            )
        else:
            reason, detail = verdict[1], None
            if reason == NoEvidenceReason.TARGETS_NOT_DONE.value:
                if plans is None:
                    plans = PlanDeliveryCache(worktree_root)
                coded = _coded_plan(plans, evidence.get(rel, []))
                if coded is not None:
                    reason = NoEvidenceReason.ACS_ALL_TICKED.value
                    detail = (
                        f"every dispatchable row of {coded[0]} is coded (last commit {coded[1]}) "
                        f"but its status is not terminal; closing needs the plan implemented"
                    )
            no_evidence_batons.append(
                {"handoff_path": rel, "reason": reason, "evidence": evidence.get(rel, []),
                 "conflict": None, "detail": detail}
            )

    residue.sort(key=lambda item: item["handoff_path"])
    no_evidence_batons.sort(key=lambda item: item["handoff_path"])
    return {
        "exit_code": 0,
        "schema_version": SCHEMA_VERSION,
        "op": OP_KEY,
        "batons_scanned": scanned,
        "no_evidence": len(no_evidence_batons),
        "no_evidence_batons": no_evidence_batons,
        "closable": closable,
        "residue": residue,
        "scan_incomplete": scan_incomplete,
    }
