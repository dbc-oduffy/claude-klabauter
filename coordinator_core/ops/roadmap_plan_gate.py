"""
coordinator_core.ops.roadmap_plan_gate — JSON-RPC "roadmap.plan_gate" operation.

Purpose: thin RPC wrapper over ``coordinator_core.roadmap.gate_v2.compute_gate_report``.
This module's only original code is param validation, worktree-root derivation, the
one-baton ``verdict`` selection, and turning a refused read into an error reply. Every
disposition, gate and wave is computed by ``gate_v2``.

Wire params (all optional):
    subject     (str)  — narrow the report to one baton, named by id, stub_id or path.
    roadmap_id  (str)  — restrict the candidate set to one roadmap.
    targets     (list) — restrict the candidate set to these baton ids.
    gate        (str)  — "planning" | "execution" | "both" (default); selects what
                         ``verdict`` carries under ``subject``.

Reply fields: the requirement-9 top-level set of ``gate_v2.REPORT_KEYS``. ``verdict`` is
populated only under ``subject``.

Negative-spec:
  - Does NOT write, stamp or mutate anything. A closed gate is REPORTED; refusing on it is
    the caller's act.
  - Does NOT spawn.
  - Does NOT return a report when the read cannot answer. ``GateReadRefused`` becomes an
    error reply carrying its text; an empty or open report would read as "no blockers".
  - Does NOT accept a caller-supplied root. The root is the per-request ``repo_root``
    promoted to the main worktree via ``main_worktree_root``.
  - Does NOT fall back to the process cwd when ``repo_root`` is absent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from coordinator_core.ipc import CallerFacingValidationError, register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.roadmap import gate_v2

GENERATES: list = []

_GATE_CHOICES = ("planning", "execution", "both")


def _select_verdict(report: Dict[str, Any], subject: Optional[str], gate: str) -> Optional[Dict[str, Any]]:
    if subject is None:
        return None
    # gate_v2 narrows the report to the single resolved baton under ``subject`` (any
    # id/stem/path spelling), so a lone row IS the subject; re-matching spellings misses it.
    matches = report["batons"] if len(report["batons"]) == 1 else [
        b for b in report["batons"] if subject in (b["id"], b["path"], b["stub_id"])
    ]
    if not matches:
        return {
            "subject": subject,
            "resolved": False,
            "open": False,
            "reason": "no baton on disk carries this id, stub_id, or path",
        }
    if len(matches) > 1:
        return {
            "subject": subject,
            "resolved": False,
            "open": False,
            "reason": f"subject matches {len(matches)} batons; name one by id",
        }
    baton = matches[0]
    if gate == "both":
        return {
            "subject": subject,
            "resolved": True,
            "planning_gate": baton["planning_gate"],
            "execution_gate": baton["execution_gate"],
            "open": baton["planning_gate"]["open"] and baton["execution_gate"]["open"],
        }
    key = "planning_gate" if gate == "planning" else "execution_gate"
    return {
        "subject": subject,
        "resolved": True,
        "gate": gate,
        "open": baton[key]["open"],
        "blocking": baton[key]["blocking"],
    }


@register_op("roadmap.plan_gate")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "roadmap.plan_gate" handler. See module docstring."""
    if repo_root is None:
        raise CallerFacingValidationError("roadmap.plan_gate requires a resolved repo_root")

    subject = params.get("subject")
    if subject is not None and not isinstance(subject, str):
        raise CallerFacingValidationError("subject must be a string when supplied")
    roadmap_id = params.get("roadmap_id")
    if roadmap_id is not None and not isinstance(roadmap_id, str):
        raise CallerFacingValidationError("roadmap_id must be a string when supplied")
    gate = params.get("gate", "both")
    if gate not in _GATE_CHOICES:
        raise CallerFacingValidationError(f"gate must be one of {_GATE_CHOICES}, got {gate!r}")
    targets = params.get("targets")
    if targets is not None:
        if not isinstance(targets, list) or not all(isinstance(t, str) and t for t in targets):
            raise CallerFacingValidationError("targets must be a list of non-empty strings when supplied")
        if not targets:
            raise CallerFacingValidationError("targets was supplied but empty — omit it to sweep every baton")

    worktree_root = main_worktree_root(repo_root)
    try:
        report = gate_v2.compute_gate_report(
            Path(worktree_root),
            roadmap_id=roadmap_id or None,
            targets=targets,
            subject=subject or None,
        )
    except gate_v2.GateReadRefused as exc:
        raise CallerFacingValidationError(f"roadmap.plan_gate refused: {exc}") from exc
    report = dict(report)
    report["verdict"] = _select_verdict(report, subject or None, gate)
    return report
