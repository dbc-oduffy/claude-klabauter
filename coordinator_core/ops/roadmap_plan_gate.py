"""
coordinator_core.ops.roadmap_plan_gate — JSON-RPC "roadmap.plan_gate" operation.

Purpose: thin RPC wrapper over ``coordinator_core.roadmap.plan_gate``, the
two-gate roadmap resolver. This module's only original code is param validation
and worktree-root derivation; every disposition, gate, and wave is computed by
the library module, which is pure.

The op exists so that the planning-vs-execution distinction is a MEASUREMENT a
caller reads, not a rule a caller remembers. A skill that has to derive "are
this baton's blockers planned yet?" from `blocked_by` by hand will derive it
differently each time, and the difference will be invisible until a wave fires
against a gate that was never open.

Wire params (all optional):
    subject     (str) — narrow the reported `batons` list to one baton, named by
                        any of its ids (`stub_id`, `handoff_id`,
                        `deliverable_id`, filename stem) or by its repo-relative
                        path. The SCAN is always whole-tree — a baton's gates
                        are a function of the corpus — so this filters the
                        report, never the computation.
    roadmap_id  (str) — restrict the candidate set to one roadmap.
    gate        (str) — "planning" | "execution" | "both" (default). Selects
                        which gate's verdict lands in the top-level `verdict`
                        field; both gates are always present per baton.

Reply fields:
    {"batons": [...], "waves": [[id, ...], ...], "cycles": [[id, ...], ...],
     "unresolved_blockers": [...], "counts": {...}, "scanned": {...},
     "verdict": {...} | None}

    `verdict` is populated only under `subject`, and is that baton's selected
    gate plus the reason it is shut — the one-baton admission question
    (`/pickup` asking "may planning start?", `/execute-plan` asking "may
    execution start?") answered without the caller re-deriving it from `batons`.

Negative-spec:
  - Does NOT write, stamp, or mutate anything. A closed gate is REPORTED here;
    refusing on it is the caller's act. Making this op the refusal would put an
    authorization decision behind a derived read, and a derived read that has
    gone stale would then silently block work rather than mis-report it.
  - Does NOT spawn, and does NOT consult git. See the library module's Budget
    note: `deployment_state` and plan `status` are the disk-truth read.
  - Does NOT accept a caller-supplied root. The scan is rooted at the
    per-request `repo_root` resolved by the dispatcher, promoted to the main
    worktree via `main_worktree_root` — `state/handoffs/` and `docs/plans/`
    live in the main worktree, and a linked worktree's own root holds neither.
  - Does NOT fall back to the process cwd when `repo_root` is absent. An op
    keyed "common_dir" is always handed one; deriving a root from cwd instead
    would make the answer depend on where the caller happened to stand.

Spec backlink: DoE-claude coordinator/skills/plan-blitz/SKILL.md § The two gates
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.roadmap.plan_gate import assemble_plan_gate

# Generator-provenance: this op writes nothing.
GENERATES: list = []

_GATE_CHOICES = ("planning", "execution", "both")


def _select_verdict(report: Dict[str, Any], subject: Optional[str], gate: str) -> Optional[Dict[str, Any]]:
    """The one-baton admission verdict, or None when no single subject resolved.

    A `subject` matching no baton returns a verdict of its own — `resolved:
    False` — rather than None. Silence would read as "no gate holds this", which
    is the fail-open direction: a caller asking about a baton that does not
    exist must not be told to proceed.
    """
    if subject is None:
        return None
    matches = [b for b in report["batons"] if subject in (b["id"], b["path"], b["stub_id"])]
    if not matches:
        return {
            "subject": subject,
            "resolved": False,
            "open": False,
            "reason": "no baton on disk carries this id, stub_id, or path",
        }
    baton = matches[0]
    if gate == "both":
        return {
            "subject": subject,
            "resolved": True,
            "planning_gate": baton["planning_gate"],
            "execution_gate": baton["execution_gate"],
            "open": baton["planning_gate"]["open"],
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
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "roadmap.plan_gate" handler. See module docstring."""
    if repo_root is None:
        raise ValueError("roadmap.plan_gate requires a resolved repo_root")

    subject = params.get("subject")
    if subject is not None and not isinstance(subject, str):
        raise ValueError("subject must be a string when supplied")
    roadmap_id = params.get("roadmap_id")
    if roadmap_id is not None and not isinstance(roadmap_id, str):
        raise ValueError("roadmap_id must be a string when supplied")
    gate = params.get("gate", "both")
    if gate not in _GATE_CHOICES:
        raise ValueError(f"gate must be one of {_GATE_CHOICES}, got {gate!r}")
    targets = params.get("targets")
    if targets is not None:
        if not isinstance(targets, list) or not all(isinstance(t, str) and t for t in targets):
            raise ValueError("targets must be a list of non-empty strings when supplied")
        if not targets:
            # An empty list is the caller asking for nothing, which is almost
            # always a mistake upstream (a filter that matched zero). Refusing is
            # louder than returning an empty sweep that reads as "nothing to do".
            raise ValueError("targets was supplied but empty — omit it to sweep every baton")

    worktree_root = main_worktree_root(repo_root)
    report = assemble_plan_gate(
        Path(worktree_root),
        subject=subject or None,
        roadmap_id=roadmap_id or None,
        targets=targets,
    )
    if targets:
        report["unmatched_targets"] = sorted(
            t for t in targets if not any(t in (b["id"], b["path"], b["stub_id"]) for b in report["batons"])
        )
    else:
        report["unmatched_targets"] = []
    report["verdict"] = _select_verdict(report, subject or None, gate)
    return report
