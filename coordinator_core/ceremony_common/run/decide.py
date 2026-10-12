"""In-run resolver: every judgment point ends as a decision, an observation or an agent entry.

The module is ceremony-neutral: the rule table, the routing and the point-claiming rules arrive
as arguments, so another ceremony imports it unchanged. Nothing here hands a decision to a
person: handback and never-offered dispositions are removed from every entry before an agent
is offered it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from coordinator_core.contract.decision_object.judgment import partition_reportable

from .contract import DECISIONS_FILE, HANDBACK_DISPOSITIONS, NEVER_OFFERED_DISPOSITIONS, Decision

DEFAULT_AGENT = "coordinator:apm"
RECOMMENDATION = "recommendation"

PointRule = Callable[..., "Decision | None"]


@dataclass(frozen=True)
class RuleTable:
    """The deciders a ceremony hands to `resolve`.

    `point_rules` is tried in order; the first to return a Decision claims the point. `routing`
    maps a point id prefix to the subagent type that answers an unclaimed point; a point no
    prefix matches goes to `default_agent`. `exhausted_rule` is the rule id stamped on the
    `no-change` decision of a point left with no non-handback disposition.
    """

    point_rules: tuple[tuple[str, PointRule], ...]
    exhausted_rule: str
    routing: Mapping[str, str] = field(default_factory=dict)
    default_agent: str = DEFAULT_AGENT


@dataclass(frozen=True)
class Resolution:
    decisions: list[Decision]
    observations: list[dict[str, Any]]
    judgment_entries: list[dict[str, Any]]


def recommended_disposition(point: Mapping[str, Any]) -> tuple[str, str] | None:
    """`(value, rationale)` when the point recommends one of its own non-handback dispositions.

    The only place a `recommendation` is read.
    """
    recommendation = point.get("recommendation")
    if not isinstance(recommendation, Mapping):
        return None
    value = recommendation.get("disposition")
    if value in HANDBACK_DISPOSITIONS:
        return None
    declared = {d.get("value") for d in (point.get("dispositions") or [])}
    if value not in declared:
        return None
    return value, str(recommendation.get("rationale") or "")


def recommendation(point: Mapping[str, Any], *, context: Mapping[str, Any]) -> Decision | None:
    """DR-312: a point recommending one of its own non-handback dispositions takes it."""
    picked = recommended_disposition(point)
    if picked is None:
        return None
    value, rationale = picked
    return Decision(
        point_id=str(point.get("id", "")),
        question=str(point.get("question", "")),
        decision=value,
        decided_by="rule",
        rule=RECOMMENDATION,
        rationale=rationale,
        evidence=str(point.get("evidence", "")),
    )


def _agent_for(point_id: str, rules: RuleTable) -> str:
    for prefix, agent in rules.routing.items():
        if point_id.startswith(prefix):
            return agent
    return rules.default_agent


def _no_live_decision(point: Mapping[str, Any], rules: RuleTable) -> Decision:
    return Decision(
        point_id=str(point.get("id", "")),
        question=str(point.get("question", "")),
        decision="no-change",
        decided_by="rule",
        rule=rules.exhausted_rule,
        rationale="Every disposition the producer offered names a person as the actor; none can be taken in a run.",
        evidence=f"producer evidence: {point.get('evidence', '')}",
    )


def resolve(
    judgment_points: Sequence[Mapping[str, Any]],
    directives: Sequence[Mapping[str, Any]],
    *,
    rules: RuleTable,
    context: Mapping[str, Any],
) -> Resolution:
    """Place every point in exactly one of decisions, observations, judgment entries.

    Order per point: a reportable gate-nothing point is an observation; a rule that claims it
    decides it; otherwise it becomes an entry with handback dispositions removed, or a
    `no-change` decision when none remain.
    """
    asked, reported = partition_reportable(judgment_points, directives)
    decisions: list[Decision] = []
    entries: list[dict[str, Any]] = []
    for point in asked:
        decided = None
        for _rule_id, rule in rules.point_rules:
            decided = rule(point, context=context)
            if decided is not None:
                break
        if decided is not None:
            decisions.append(decided)
            continue
        live = [
            d
            for d in (point.get("dispositions") or [])
            if d.get("value") not in HANDBACK_DISPOSITIONS | NEVER_OFFERED_DISPOSITIONS
        ]
        if not live:
            decisions.append(_no_live_decision(point, rules))
            continue
        entry = dict(point)
        entry["dispositions"] = live
        entry["agent"] = _agent_for(str(point.get("id", "")), rules)
        entries.append(entry)
    return Resolution(decisions=decisions, observations=reported, judgment_entries=entries)


def record(run_dir: Path, decisions: Sequence[Decision]) -> None:
    """Append one JSON object per decision to the run's decisions file."""
    if not decisions:
        return
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / DECISIONS_FILE).open("a", encoding="utf-8", newline="\n") as fh:
        for decision in decisions:
            fh.write(json.dumps(decision.to_row(), sort_keys=True) + "\n")


def _result_for(point_id: str, agent_results: Mapping[str, Any]) -> Mapping[str, Any] | None:
    groups = agent_results.get("groups")
    if not isinstance(groups, Mapping):
        return None
    for results in groups.values():
        for result in results or []:
            if isinstance(result, Mapping) and result.get("key") == point_id:
                return result
    return None


def record_agent_decisions(
    run_dir: Path,
    judgment_entries: Sequence[Mapping[str, Any]],
    agent_results: Mapping[str, Any],
) -> tuple[list[Decision], list[dict[str, str]]]:
    """Record one decision per entry whose agent result carries a disposition the entry declares.

    An entry with no result, no decision, or a disposition it never declared comes back in
    `undecided` as `{"id", "reason"}`. An answer is never coerced or defaulted.
    """
    decisions: list[Decision] = []
    undecided: list[dict[str, str]] = []
    for entry in judgment_entries:
        point_id = str(entry.get("id", ""))
        result = _result_for(point_id, agent_results)
        if result is None:
            undecided.append({"id": point_id, "reason": "no-agent-result"})
            continue
        answer = result.get("decision")
        if not isinstance(answer, Mapping) or not answer.get("disposition"):
            undecided.append({"id": point_id, "reason": "result-carries-no-decision"})
            continue
        value = answer["disposition"]
        payload = result.get("payload")
        declared = {d.get("value") for d in (entry.get("dispositions") or [])}
        if value not in declared:
            undecided.append({"id": point_id, "reason": f"undeclared-disposition:{value}"})
            continue
        decisions.append(
            Decision(
                point_id=point_id,
                question=str(entry.get("question", "")),
                decision=value,
                decided_by=f"agent:{entry.get('agent', DEFAULT_AGENT)}",
                rule="",
                rationale=str(answer.get("rationale", "")),
                evidence=str(entry.get("evidence", "")),
                payload=payload if isinstance(payload, Mapping) else None,
            )
        )
    record(run_dir, decisions)
    return decisions, undecided


def decisions_map(decisions: Sequence[Decision]) -> dict[str, dict[str, str]]:
    """`{point_id: {"disposition": value}}`, the shape `apply_base.directive_gate_open` reads."""
    return {d.point_id: {"disposition": d.decision} for d in decisions}
