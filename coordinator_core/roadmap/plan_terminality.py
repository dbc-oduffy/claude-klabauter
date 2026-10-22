"""
coordinator_core.roadmap.plan_terminality — is every plan a baton names finished?

Purpose: the verdict behind the `plan-terminality=` line `archive-stamp-cli
ship-handoff` prints. It unions the plans `plan_gate.link_plans` resolves for a
baton with the plans whose `predecessor_handoff:` names the baton, and reports
whether every one is in `PLAN_TERMINAL_STATUS`.

Domain vocabulary: baton, plan link basis, terminal plan.

Negative-spec:
  - Does NOT re-implement the baton-to-plan link order; `link_plans` owns it.
  - Does NOT raise: an unreadable corpus is `no-plan-linked`.
  - Does NOT spawn or write; one in-process index build when no index is passed.
  - Leaf module: imports `plan_gate` and `lifecycle_constants` only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, NamedTuple, Optional, Tuple

from coordinator_core.lifecycle_constants import PLAN_TERMINAL_STATUS
from coordinator_core.roadmap.plan_gate import (
    PlanIndex,
    build_plan_index,
    link_plans,
    plans_naming_predecessor,
)

PLAN_TERMINALITY_NO_PLAN = "no-plan-linked"
PLAN_TERMINALITY_TERMINAL = "terminal"
PLAN_TERMINALITY_NON_TERMINAL = "non-terminal"

_REVERSE_EDGE_BASIS = "predecessor_handoff"


class PlanTerminality(NamedTuple):
    outcome: str
    plans: Tuple[Tuple[str, str], ...]  # (repo-relative plan path, status), every linked plan
    basis: Optional[str]  # link_plans basis, else "predecessor_handoff", else None


def plan_terminality(
    baton_fm: Mapping[str, Any],
    baton_basename: str,
    worktree_root: Path,
    plans: Optional[PlanIndex] = None,
) -> PlanTerminality:
    """The plan verdict for one baton. Any non-terminal linked plan makes it non-terminal.

    `plans` is built from `worktree_root` only when None. Never raises.
    """
    try:
        if plans is None:
            plans = build_plan_index(worktree_root)
        hits, basis = link_plans(dict(baton_fm), plans)
        reverse = plans_naming_predecessor(plans, baton_basename)
    except Exception:  # an unreadable corpus reads as no-plan-linked; callers cannot tell the two apart
        return PlanTerminality(PLAN_TERMINALITY_NO_PLAN, (), None)

    linked = {}
    for record in list(hits) + list(reverse):
        linked.setdefault(record["path"], str(record.get("status") or ""))
    if not linked:
        return PlanTerminality(PLAN_TERMINALITY_NO_PLAN, (), None)

    pairs = tuple(sorted(linked.items()))
    all_terminal = all(status.lower() in PLAN_TERMINAL_STATUS for _, status in pairs)
    outcome = PLAN_TERMINALITY_TERMINAL if all_terminal else PLAN_TERMINALITY_NON_TERMINAL
    return PlanTerminality(outcome, pairs, basis or _REVERSE_EDGE_BASIS)


def format_terminality_line(v: PlanTerminality) -> str:
    """The single `plan-terminality=` line; non-terminal names only the non-terminal plans."""
    if v.outcome == PLAN_TERMINALITY_NO_PLAN:
        return f"plan-terminality={PLAN_TERMINALITY_NO_PLAN}"
    if v.outcome == PLAN_TERMINALITY_TERMINAL:
        return f"plan-terminality={PLAN_TERMINALITY_TERMINAL} plans={','.join(p for p, _ in v.plans)}"
    parts = [
        f"plan={path} status={status or '<none>'}"
        for path, status in v.plans
        if status.lower() not in PLAN_TERMINAL_STATUS
    ]
    return f"plan-terminality={PLAN_TERMINALITY_NON_TERMINAL} " + "; ".join(parts)
