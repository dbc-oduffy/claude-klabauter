"""Authoring constraints: the prep-gate and readiness rules handed to plan-author before it drafts.

One registry read by the gate wiring, the waiver reader and the prompt renderer, so the rule
a plan-author is shown cannot diverge from the kind the gate emits. Pure: no I/O, no spawn.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

#: Plan frontmatter key: ``constraint_waived: [{id, reason}]``.
WAIVER_FIELD = "constraint_waived"

READINESS = "READINESS"


@dataclass(frozen=True)
class Constraint:
    """One authoring rule. ``id`` is the defect kind the gate emits; ``source`` is
    ``<enclosing function> <repo-relative path>``."""

    id: str
    gate: str
    rule: str
    source: str
    satisfy: str
    waivable: bool
    declared_empty_form: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "id": self.id,
            "gate": self.gate,
            "rule": self.rule,
            "source": self.source,
            "satisfy": self.satisfy,
            "waivable": self.waivable,
        }
        if self.declared_empty_form is not None:
            out["declared_empty_form"] = self.declared_empty_form
        return out


CONSTRAINTS: Tuple[Constraint, ...] = (
    Constraint(
        id="register-rows-unclaimed",
        gate="REGISTER",
        rule="register_claims.rows must name every row of the sizing's requirement_register.",
        source="_register coordinator_core/roadmap/prep_gate.py",
        satisfy="List every register row id in register_claims.rows, each with its pin.",
        waivable=False,
    ),
    Constraint(
        id="register-claims-missing",
        gate="REGISTER",
        rule="A plan whose sizing_object carries a requirement_register must declare register_claims.",
        source="_register coordinator_core/roadmap/prep_gate.py",
        satisfy="Add register_claims: {sizing, rows, pins} citing the sizing's register.",
        waivable=False,
    ),
    Constraint(
        id="anchor-premises-undeclared",
        gate=READINESS,
        rule="Declare each load-bearing premise as an anchor premise with a runnable check.",
        source="parse_anchors coordinator_core/ops/_anchor_premise.py",
        satisfy="anchor_premises: [{id, claim, check: {command, expect}}]",
        declared_empty_form="anchor_premises: []",
        waivable=True,
    ),
    Constraint(
        id="capabilities-undeclared",
        gate="CAPABILITIES",
        rule="The plan frontmatter must carry a capabilities key.",
        source="_capabilities coordinator_core/roadmap/prep_gate.py",
        satisfy="capabilities: [<capability the plan needs>]",
        declared_empty_form="capabilities: []",
        waivable=True,
    ),
    Constraint(
        id="census-undeclared",
        gate="CENSUS",
        rule="The plan must carry a census key; a plan resting on no counted premise declares it empty.",
        source="_census coordinator_core/roadmap/prep_gate.py",
        satisfy="census: [{question, command, result, count}]",
        declared_empty_form="census: []",
        waivable=True,
    ),
    Constraint(
        id="prime-exit-absent",
        gate="PRIME_EXIT",
        rule="Every plan carries prime_exit_criterion with a statement and a derived_from link.",
        source="_prime_exit coordinator_core/roadmap/prep_gate.py",
        satisfy="prime_exit_criterion: {statement: <behaviour and tests>, derived_from: <sizing path>}",
        waivable=False,
    ),
)

_BY_ID: Dict[str, Constraint] = {c.id: c for c in CONSTRAINTS}


def constraint_ids() -> Tuple[str, ...]:
    return tuple(c.id for c in CONSTRAINTS)


def get_constraint(constraint_id: str) -> Optional[Constraint]:
    return _BY_ID.get(constraint_id)


def constraints_list() -> List[Dict[str, Any]]:
    """The constraint list as plain dicts, in registry order."""
    return [c.as_dict() for c in CONSTRAINTS]


@dataclass(frozen=True)
class WaiverReading:
    """``waived`` maps an honoured id to its reason; each other entry is a ``problems`` item
    ``{kind, id, detail}`` with kind one of malformed, reasonless, unknown, non-waivable."""

    waived: Dict[str, str]
    problems: Tuple[Dict[str, str], ...]


def read_waivers(fm: Optional[Mapping[str, Any]]) -> WaiverReading:
    """Map a plan's ``constraint_waived`` to ``{id: reason}``. An absent field yields nothing."""
    if not isinstance(fm, Mapping) or WAIVER_FIELD not in fm:
        return WaiverReading({}, ())
    entries = fm.get(WAIVER_FIELD)
    if entries is None:
        return WaiverReading({}, ())
    problems: List[Dict[str, str]] = []
    if not isinstance(entries, list):
        problems.append({"kind": "malformed", "id": "", "detail": f"{WAIVER_FIELD} is not a list"})
        return WaiverReading({}, tuple(problems))
    waived: Dict[str, str] = {}
    for i, entry in enumerate(entries):
        if not isinstance(entry, Mapping) or not isinstance(entry.get("id"), str) or not entry["id"].strip():
            problems.append(
                {"kind": "malformed", "id": "", "detail": f"{WAIVER_FIELD}[{i}] is not {{id, reason}}"}
            )
            continue
        cid = entry["id"].strip()
        reason = entry.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            problems.append({"kind": "reasonless", "id": cid, "detail": f"waiver of {cid} carries no reason"})
            continue
        constraint = _BY_ID.get(cid)
        if constraint is None:
            problems.append({"kind": "unknown", "id": cid, "detail": f"{cid} is not an authoring constraint"})
            continue
        if not constraint.waivable:
            problems.append({"kind": "non-waivable", "id": cid, "detail": f"{cid} cannot be waived"})
            continue
        waived[cid] = reason.strip()
    return WaiverReading(waived, tuple(problems))


def render_prompt() -> str:
    """The constraint block for a plan-author prompt."""
    lines = [
        "Authoring constraints (the gate applies these to your plan; satisfy each before drafting):",
    ]
    for c in CONSTRAINTS:
        lines.append(f"- {c.id} [{c.gate}]: {c.rule}")
        lines.append(f"  satisfy: {c.satisfy}")
        if c.declared_empty_form is not None:
            lines.append(f"  if none: {c.declared_empty_form}")
        lines.append(f"  source: {c.source}")
        if not c.waivable:
            lines.append("  not waivable")
    waivable = ", ".join(c.id for c in CONSTRAINTS if c.waivable)
    lines.append(
        f"Waive a constraint with a reason in frontmatter: {WAIVER_FIELD}: [{{id: <id>, reason: <why>}}]. "
        f"Waivable: {waivable}. An absent {WAIVER_FIELD} means you proceed."
    )
    return "\n".join(lines)
