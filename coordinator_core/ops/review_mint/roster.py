"""Pure parser for the roster-v5 ``execute_review`` block of coordinator-content-repo's review-roster fragment (``parse_execute_review``).

No file I/O and no cross-repo pointer resolution; ``op.load_fragment`` reads the fragment. Agent names are never hardcoded here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Dict, List, Optional, Tuple

import yaml

from coordinator_core.frontmatter.primitives import split_frontmatter


_VALID_EFFORTS = frozenset({"low", "medium", "high"})
#: The tiers ``dispatch_emit.emit.derive_review_tier`` returns.
VALID_TIERS = frozenset({"lightweight", "standard", "full"})
_VALID_PER_VALUES = frozenset({"slice", "whole-diff"})
#: Mirror of ``sizing_assemble.TSHIRT_ORDER`` (importing it costs ~16ms); a test pins equality.
_TSHIRT_ORDER = ("XS", "S", "M", "L", "XL", "XXL")


EMIT_ROUTE_PLAN = "plan"
EMIT_ROUTE_INVENTORY = "inventory"
EMIT_ROUTE_QUEUE = "queue"
EMIT_ROUTE_WAVE_FIRE = "wave-fire-dispatch"


class RosterFragmentError(ValueError):
    pass


def require_emit_route(fragment: dict, route: str) -> None:
    """Raise ``RosterFragmentError`` naming ``route`` unless
    ``fragment["execute_review"]["required_for_emit"]`` is a list containing it."""
    block = fragment.get("execute_review") if isinstance(fragment, dict) else None
    required = block.get("required_for_emit") if isinstance(block, dict) else None
    if not isinstance(required, list) or route not in required:
        raise RosterFragmentError(
            f"review roster 'execute_review.required_for_emit' does not list route {route!r}"
        )


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
    #: Review tiers this agent runs at; ``None`` runs at every tier.
    tiers: Optional[Tuple[str, ...]] = None
    #: ``(min, max)`` executor rows one reviewer covers; ``None`` keeps one reviewer per slice.
    executor_rows_per_reviewer: Optional[Tuple[int, int]] = None
    #: Smallest plan t-shirt this agent is admitted at; ``None`` admits every size.
    min_tshirt: Optional[str] = None

    def runs_at(self, tier: Optional[str]) -> bool:
        """True when this agent runs at ``tier``; an untiered agent or an unknown tier always runs."""
        return tier is None or self.tiers is None or tier in self.tiers

    def reviewer_count(self, rows: int, slices: int) -> int:
        """Reviewers to fan out over ``slices`` slices of a ``rows``-row run: ``slices`` when no
        ratio is set, else ``ceil(rows / max_rows)`` held to at least ``min_rows`` rows each, within ``[1, slices]``."""
        if self.executor_rows_per_reviewer is None:
            return slices
        min_rows, max_rows = self.executor_rows_per_reviewer
        return max(1, min(slices, -(-rows // max_rows), rows // min_rows))

    def admits_tshirt(self, tshirt: Optional[str]) -> bool:
        """True when this agent is admitted at plan ``tshirt``; under a floor an unknown size is
        excluded, since a grind row carries no size and must not admit an L-gated reviewer."""
        if self.min_tshirt is None:
            return True
        if tshirt not in _TSHIRT_ORDER:
            return False
        return _TSHIRT_ORDER.index(tshirt) >= _TSHIRT_ORDER.index(self.min_tshirt)


DELIVERY_NOT_ROSTERED = "roster names no coordinator:delivery-verifier"


def agent_not_run_at_tier(review: "ExecuteReview", agent_type: str, tier: Optional[str]) -> bool:
    """True when the review wave names no ``agent_type``, or its roster ``tiers`` exclude ``tier``."""
    agents = [a for a in review.review_wave if a.agent_type == agent_type]
    return not any(a.runs_at(tier) for a in agents)


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


#: The roster label of the signal-resolved reviewer stage that required reviewers join.
NAMED_SIGNAL_LABEL = "named"
_AGENT_TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*:[a-z0-9][a-z0-9_-]*$")


def validate_required_reviewers(raw: object) -> List[str]:
    """``raw`` as a de-duplicated list of ``plugin:agent`` types; ``None`` is the empty list.
    Raises ``RosterFragmentError`` on a non-list or a malformed entry."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise RosterFragmentError(f"required_reviewers {raw!r} is not a list of agent types")
    out: List[str] = []
    for item in raw:
        if not isinstance(item, str) or not _AGENT_TYPE_RE.match(item):
            raise RosterFragmentError(
                f"required_reviewers entry {item!r} is not a 'plugin:agent' agent type"
            )
        if item not in out:
            out.append(item)
    return out


def plan_required_reviewers(plan_text: Optional[str]) -> List[str]:
    """The plan frontmatter's ``required_reviewers`` (plan.schema.json), validated; empty when the
    plan is absent, has no frontmatter, or declares none."""
    return validate_required_reviewers(_plan_frontmatter(plan_text).get("required_reviewers"))


def _plan_frontmatter(plan_text: Optional[str]) -> dict:
    """The plan's frontmatter mapping; ``{}`` when absent, unparseable, or not a mapping."""
    if not plan_text:
        return {}
    split = split_frontmatter(plan_text)
    if split is None:
        return {}
    try:
        doc = yaml.safe_load(split.fm_text)
    except yaml.YAMLError:
        return {}
    return doc if isinstance(doc, dict) else {}


#: Plan ``review_signals`` that select a wave reviewer; the default for ``plan_review_signals``'s ``selects``.
DEFAULT_SIGNAL_SELECTS = {"front-end": "coordinator:senior-front-end"}


def plan_review_signals(
    plan_text: Optional[str],
    selects: Optional[Dict[str, str]] = None,
) -> Dict[str, List[str]]:
    """The ``signals`` mapping for ``parse_execute_review``: the agent types the plan's
    ``review_signals`` select (per ``selects``, default ``DEFAULT_SIGNAL_SELECTS``), under the
    ``named`` label. Empty when the plan is absent, malformed, or declares no selecting signal."""
    declared = _plan_frontmatter(plan_text).get("review_signals")
    if not isinstance(declared, list):
        return {}
    table = DEFAULT_SIGNAL_SELECTS if selects is None else selects
    agents = validate_required_reviewers(
        [table[s] for s in declared if isinstance(s, str) and s in table]
    )
    return {NAMED_SIGNAL_LABEL: agents} if agents else {}


def parse_execute_review(
    fragment: dict,
    signals: Optional[Dict[str, List[str]]] = None,
    required_reviewers: Optional[List[str]] = None,
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

    ``required_reviewers`` (``plugin:agent`` types the plan or ask names) join the
    ``named`` signal label's selection and run at every tier, whatever tiers the
    fragment's ``named`` entry carries. A fragment with no ``named`` entry in a
    review-wave stage raises, since the reviewer would otherwise be dropped silently.

    Raises ``RosterFragmentError`` on any structural violation, naming
    what is wrong.
    """
    required = validate_required_reviewers(required_reviewers)
    if required:
        merged = list((signals or {}).get(NAMED_SIGNAL_LABEL) or [])
        merged.extend(t for t in required if t not in merged)
        signals = {**(signals or {}), NAMED_SIGNAL_LABEL: merged}
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

    if required:
        landed = {a.agent_type for a in wave_agents if a.accepts_signals == NAMED_SIGNAL_LABEL}
        missing = [t for t in required if t not in landed]
        if missing:
            raise RosterFragmentError(
                f"required_reviewers {missing!r} cannot join: the roster declares no "
                f"review-wave '{NAMED_SIGNAL_LABEL}' (accepts_signals) entry"
            )
        wave_agents = [
            replace(a, tiers=None) if a.agent_type in required and a.accepts_signals == NAMED_SIGNAL_LABEL else a
            for a in wave_agents
        ]

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

    where = f"execute_review stage {stage_index} agent entry"
    rows_per_reviewer: Optional[Tuple[int, int]] = None
    raw_ratio = raw_agent.get("executor_rows_per_reviewer")
    if raw_ratio is not None:
        lo = raw_ratio.get("min") if isinstance(raw_ratio, dict) else None
        hi = raw_ratio.get("max") if isinstance(raw_ratio, dict) else None
        if (
            not isinstance(raw_ratio, dict)
            or set(raw_ratio) != {"min", "max"}
            or any(type(v) is not int or v < 1 for v in (lo, hi))
            or lo > hi
        ):
            raise RosterFragmentError(
                f"{where} 'executor_rows_per_reviewer' {raw_ratio!r} must be "
                "{min, max} integers >= 1 with min <= max"
            )
        if per != "slice":
            raise RosterFragmentError(
                f"{where} 'executor_rows_per_reviewer' requires 'per': 'slice', got {per!r}"
            )
        rows_per_reviewer = (lo, hi)

    min_tshirt = raw_agent.get("min_tshirt")
    if min_tshirt is not None and min_tshirt not in _TSHIRT_ORDER:
        raise RosterFragmentError(
            f"{where} 'min_tshirt' {min_tshirt!r} is not one of {list(_TSHIRT_ORDER)!r}"
        )

    raw_tiers = raw_agent.get("tiers")
    tiers: Optional[Tuple[str, ...]] = None
    if raw_tiers is not None:
        if (
            not isinstance(raw_tiers, list)
            or not raw_tiers
            or any(t not in VALID_TIERS for t in raw_tiers)
        ):
            raise RosterFragmentError(
                f"execute_review stage {stage_index} agent entry 'tiers' {raw_tiers!r} "
                f"must be a non-empty list drawn from {sorted(VALID_TIERS)!r}"
            )
        tiers = tuple(raw_tiers)

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
                tiers=tiers,
                executor_rows_per_reviewer=rows_per_reviewer,
                min_tshirt=min_tshirt,
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
            tiers=tiers,
            executor_rows_per_reviewer=rows_per_reviewer,
            min_tshirt=min_tshirt,
        )
        for resolved_type in selected
    ]
