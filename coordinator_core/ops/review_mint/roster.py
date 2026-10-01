"""Pure parser for the roster-v5 ``execute_review`` block of coordinator-content-repo's review-roster fragment (``parse_execute_review``).

No file I/O and no cross-repo pointer resolution; ``op.load_fragment`` reads the fragment. Agent names are never hardcoded here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


_VALID_EFFORTS = frozenset({"low", "medium", "high"})
_VALID_PER_VALUES = frozenset({"slice", "whole-diff"})


class RosterFragmentError(ValueError):
    pass


@dataclass(frozen=True)
class ReviewAgent:
    """One resolved roster-v5 ``execute_review`` agent entry.

    ``agent_type`` is the concrete ``agentType`` this entry resolved to --
    for an ``accepts_signals`` entry that is one of the caller's
    ``signals`` selections, never the placeholder itself. ``model``/
    ``effort`` are ``None`` for a signal-resolved persona (it inherits its
    own frontmatter model/effort; Design D6), always set otherwise.
    """

    agent_type: str
    model: Optional[str]
    effort: Optional[str]
    per: Optional[str]
    applies: Optional[str]
    schema: str
    accepts_signals: Optional[str] = None


@dataclass(frozen=True)
class ExecuteReview:
    """Roster-v5 ``execute_review`` block, resolved: prep (one agent),
    review-wave (one or more agents) and integration (AT MOST one agent).

    ``integration`` is ``None`` on the zero-integration-stage path (the
    2026-09-28 no-integration-pass sequence, step b' —
    ``docs/plans/2026-09-28-*`` PM order): each review-wave reviewer applies
    its own findings in place instead of a dedicated integration pass. A
    fragment declaring exactly one ``integration`` stage still resolves it
    here, unchanged from before."""

    prep: ReviewAgent
    review_wave: List[ReviewAgent]
    integration: Optional[ReviewAgent]
    #: The terminal criterion judge (`kind: "judge"`, at most one agent): who
    #: adjudicates the plan's exit criterion is DoE's to name, never this
    #: engine's. `None` keeps the plan's own falsifier on the test runner.
    judge: Optional[ReviewAgent] = None


def parse_execute_review(
    fragment: dict,
    signals: Optional[Dict[str, List[str]]] = None,
) -> ExecuteReview:
    """Parse a schema_version 5 ``execute_review`` block (DoE D1 § Contract,
    ``review-roster-fragment.json``) into an :class:`ExecuteReview`.

    ``fragment["execute_review"]["stages"]`` holds, in any order, exactly
    one ``kind: "prep"`` stage resolving to exactly one agent, one or more
    ``kind: "review-wave"`` stages resolving to at least one agent between
    them, and AT MOST one ``kind: "integration"`` stage resolving to
    exactly one agent when present -- DoE's ask (D1 § Contract): "At most
    one ``integration`` stage may exist and it holds exactly one agent."
    Zero ``integration`` stages is the 2026-09-28 no-integration-pass path
    (step b'): ``ExecuteReview.integration`` is ``None`` in that case.

    Each stage entry either carries ``agentType`` (plus required ``model``
    and ``effort``) or ``accepts_signals`` (carrying neither -- it inherits
    its resolved persona's own frontmatter model/effort). An
    ``accepts_signals`` entry expands, in order, into one
    :class:`ReviewAgent` per agent type ``signals`` resolves for that
    label; an unresolved label contributes nothing. This module does no
    signal-name lookup of its own -- the caller resolves signals.

    Raises ``RosterFragmentError`` on any structural violation, naming
    what is wrong.
    """
    if not isinstance(fragment, dict):
        raise RosterFragmentError("review roster fragment is not a mapping")

    execute_review = fragment.get("execute_review")
    if not isinstance(execute_review, dict):
        raise RosterFragmentError(
            "review roster fragment carries no 'execute_review' mapping"
        )

    raw_stages = execute_review.get("stages")
    if not isinstance(raw_stages, list) or not raw_stages:
        raise RosterFragmentError(
            "review roster fragment 'execute_review' declares no stages"
        )

    prep_agents: List[ReviewAgent] = []
    wave_agents: List[ReviewAgent] = []
    integration_stage_count = 0
    integration_agents: List[ReviewAgent] = []
    judge_stage_count = 0
    judge_agents: List[ReviewAgent] = []

    for index, raw_stage in enumerate(raw_stages):
        if not isinstance(raw_stage, dict):
            raise RosterFragmentError(
                f"execute_review stage {index} is not a mapping"
            )
        kind = raw_stage.get("kind")
        raw_agents = raw_stage.get("agents")
        if not isinstance(raw_agents, list) or not raw_agents:
            raise RosterFragmentError(
                f"execute_review stage {index} ({kind!r}) declares no agents"
            )
        resolved = [
            agent
            for raw_agent in raw_agents
            for agent in _resolve_review_agent(raw_agent, index, signals)
        ]

        if kind == "prep":
            prep_agents.extend(resolved)
        elif kind == "review-wave":
            wave_agents.extend(resolved)
        elif kind == "integration":
            integration_stage_count += 1
            integration_agents.extend(resolved)
        elif kind == "judge":
            judge_stage_count += 1
            judge_agents.extend(resolved)
        else:
            raise RosterFragmentError(
                f"execute_review stage {index} carries unknown kind {kind!r}"
            )

    if len(prep_agents) != 1:
        raise RosterFragmentError(
            "execute_review 'prep' stage must resolve to exactly one agent, "
            f"got {len(prep_agents)}"
        )
    if not wave_agents:
        raise RosterFragmentError(
            "execute_review 'review-wave' stage(s) resolve to no agents"
        )
    if integration_stage_count not in (0, 1):
        raise RosterFragmentError(
            "execute_review fragment must declare at most one 'integration' "
            f"stage, got {integration_stage_count}"
        )
    if integration_stage_count == 1 and len(integration_agents) != 1:
        raise RosterFragmentError(
            "execute_review 'integration' stage must hold exactly one "
            f"agent, got {len(integration_agents)}"
        )

    if judge_stage_count > 1 or (judge_stage_count == 1 and len(judge_agents) != 1):
        raise RosterFragmentError(
            "execute_review fragment may declare at most one 'judge' stage holding "
            f"exactly one agent, got {judge_stage_count} stage(s), {len(judge_agents)} agent(s)"
        )

    return ExecuteReview(
        prep=prep_agents[0],
        review_wave=wave_agents,
        integration=integration_agents[0] if integration_stage_count == 1 else None,
        judge=judge_agents[0] if judge_stage_count == 1 else None,
    )


def _resolve_review_agent(
    raw_agent: object,
    stage_index: int,
    signals: Optional[Dict[str, List[str]]],
) -> List[ReviewAgent]:
    if not isinstance(raw_agent, dict):
        raise RosterFragmentError(
            f"execute_review stage {stage_index} carries a non-mapping agent entry"
        )

    schema = raw_agent.get("schema")
    if not isinstance(schema, str) or not schema:
        raise RosterFragmentError(
            f"execute_review stage {stage_index} agent entry carries no 'schema' name"
        )

    per = raw_agent.get("per")
    if per is not None and per not in _VALID_PER_VALUES:
        raise RosterFragmentError(
            f"execute_review stage {stage_index} agent entry 'per' {per!r} "
            f"is not one of {sorted(_VALID_PER_VALUES)!r}"
        )
    applies = raw_agent.get("applies")

    agent_type = raw_agent.get("agentType")
    accepts_signals = raw_agent.get("accepts_signals")

    if agent_type is not None and accepts_signals is not None:
        raise RosterFragmentError(
            f"execute_review stage {stage_index} agent entry carries both "
            "'agentType' and 'accepts_signals'"
        )
    if agent_type is None and accepts_signals is None:
        raise RosterFragmentError(
            f"execute_review stage {stage_index} agent entry carries "
            "neither 'agentType' nor 'accepts_signals'"
        )

    if agent_type is not None:
        model = raw_agent.get("model")
        effort = raw_agent.get("effort")
        if not isinstance(model, str) or not model:
            raise RosterFragmentError(
                f"execute_review stage {stage_index} agent {agent_type!r} "
                "carries no 'model'"
            )
        if effort not in _VALID_EFFORTS:
            raise RosterFragmentError(
                f"execute_review stage {stage_index} agent {agent_type!r} "
                f"carries invalid 'effort' {effort!r}"
            )
        return [
            ReviewAgent(
                agent_type=agent_type,
                model=model,
                effort=effort,
                per=per,
                applies=applies,
                schema=schema,
                accepts_signals=None,
            )
        ]

    if raw_agent.get("model") is not None or raw_agent.get("effort") is not None:
        raise RosterFragmentError(
            f"execute_review stage {stage_index} 'accepts_signals' entry "
            f"{accepts_signals!r} must carry neither 'model' nor 'effort'"
        )

    selected = (signals or {}).get(accepts_signals) or []
    return [
        ReviewAgent(
            agent_type=resolved_type,
            model=None,
            effort=None,
            per=per,
            applies=applies,
            schema=schema,
            accepts_signals=accepts_signals,
        )
        for resolved_type in selected
    ]
