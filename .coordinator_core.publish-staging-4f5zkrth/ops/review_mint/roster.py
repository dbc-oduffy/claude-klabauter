"""Pure, stage-aware parser for DoE-claude's review-roster fragment.

Spec: ``docs/plans/2026-08-19-review-mints-its-own-gated-workflow.md`` task C1.

``parse_stages`` is the ONE reader both ``review.mint_workflow`` (C3) and
``dispatch.emit`` (C4) call — no forked copy, no second flat-list reader
drifting from this one. It is PURE: no file I/O, no cross-repo pointer
resolution. Those live at the op boundary (``review_mint/op.py``'s
``load_fragment()``, task C3) and in ``dispatch.emit``'s existing injected-
dict seam (task C4) respectively, so this module stays importable and
unit-testable with no sibling clone present.

FRAGMENT SHAPE (DoE-owned; this module consumes it and never authors the
mapping)::

    {
      "schema": "review-roster-fragment",
      "schema_version": 3,
      "blocking_verdicts": {
        "coordinator:prior-art-checker": "BLOCKED-SURFACE-TO-PM",
        "coordinator:docs-checker": null,
        ...
      },
      "tiers": {
        "standard": {
          "stages": [
            {"gate": true, "agents": ["coordinator:prior-art-checker"]},
            {"agents": ["coordinator:code-reviewer", "coordinator:staff-eng"]},
            {"agents": ["coordinator:review-integrator"]}
          ]
        }
      }
    }

``blocking_verdicts`` is top-level, keyed by ``agentType``, valued in that
agent's OWN charter vocabulary (or ``null`` for an agent that contributes no
abort) — it is the ONLY source this module consults for "which agents can
block a gate stage". Agent names and verdict tokens are never hardcoded here.

A pre-v3 ``tiers[tier]`` value may still be a flat list of ``agentType``
strings (schema_version 1). That reads as a single non-gated stage so
nothing that works today breaks; see § v1 compatibility below.

Do NOT read a ``parallel`` key from a stage — it does not exist in this
schema and must not be reintroduced (DR-327: it could encode
``{parallel: false, agents: [a, b]}``, a state the ruling forbids). Arity
alone decides serial-vs-parallel composition, and that decision belongs to
the composer (C2), not this parser.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List


class RosterFragmentError(ValueError):
    """Raised when a review-roster fragment (or the requested tier within
    it) lacks the expected shape.

    Always raised loudly, naming what is missing, in preference to a silent
    empty stage list — an empty stage would compose a review phase that
    dispatches nobody while the run still reads as reviewed.
    """


@dataclass(frozen=True)
class Stage:
    """One ordered step of a review phase.

    ``agents`` is a non-empty, ordered list of ``agentType`` strings that
    run in parallel within this stage (arity 2+) or serially (arity 1) —
    the composer (C2) decides which, never a flag read off the fragment.
    ``gate`` is True only for a stage that can abort the run; a gated stage
    is guaranteed (by ``parse_stages``) to contain at least one agent whose
    ``blocking_verdicts`` entry is non-null.
    """

    agents: List[str]
    gate: bool


def parse_stages(fragment: dict, tier: str) -> List[Stage]:
    """Parse ``fragment``'s ``tier`` entry into an ordered list of ``Stage``.

    ``fragment`` is the already-parsed, top-level review-roster-fragment
    dict (``schema``, optionally ``blocking_verdicts``, and ``tiers``) — the
    same dict shape ``dispatch_emit._reviewers_for_tier`` took, generalised
    to the staged (schema_version >= 2) shape and the top-level
    ``blocking_verdicts`` map v3 adds. No file I/O, no cross-repo pointer
    resolution: see module docstring.

    Each tier's value in ``fragment["tiers"]`` is either:

    - a flat list of ``agentType`` strings (schema_version 1) — read as a
      single ``Stage(agents=..., gate=False)``, preserving today's flat
      behaviour exactly;
    - a ``{"stages": [...]}`` mapping (schema_version >= 2) — each entry is
      a dict with a non-empty ``agents`` list and an optional ``gate``
      (defaults False), read in order into one ``Stage`` apiece.

    Raises ``RosterFragmentError``, naming what is missing, on:

    - ``fragment`` not a dict, or carrying no ``tiers`` mapping;
    - ``tier`` absent from ``tiers``;
    - a staged tier whose ``stages`` key is missing or not a list;
    - any stage (flat-list tier included) with an empty or non-list
      ``agents``;
    - a stage entry that is not a dict (staged shape only);
    - a ``gate: true`` stage containing no agent that can block — i.e. no
      agent in its ``agents`` list has a non-null entry in top-level
      ``blocking_verdicts``. An absent ``blocking_verdicts`` map (a
      pre-v3 fragment) means no agent can ever block, so any ``gate: true``
      stage on such a fragment refuses here.

    Never returns a silent empty stage list.
    """
    if not isinstance(fragment, dict):
        raise RosterFragmentError(
            "review roster fragment is not a mapping"
        )

    tiers = fragment.get("tiers")
    if not isinstance(tiers, dict):
        raise RosterFragmentError(
            "review roster fragment carries no 'tiers' mapping"
        )

    if tier not in tiers:
        raise RosterFragmentError(
            f"review roster fragment declares no tier {tier!r} "
            f"(known tiers: {sorted(tiers)})"
        )
    tier_value = tiers[tier]

    blocking_verdicts = fragment.get("blocking_verdicts")
    if not isinstance(blocking_verdicts, dict):
        blocking_verdicts = {}

    # v1 compatibility: a flat list of agentType strings is one non-gated
    # stage. Nothing that reads this shape today should observe a change.
    if isinstance(tier_value, list):
        agents = list(tier_value)
        if not agents:
            raise RosterFragmentError(
                f"review roster fragment tier {tier!r} declares no reviewers"
            )
        return [Stage(agents=agents, gate=False)]

    if not isinstance(tier_value, dict) or not isinstance(
        tier_value.get("stages"), list
    ):
        raise RosterFragmentError(
            f"review roster fragment tier {tier!r} is neither a flat "
            "reviewer list (schema_version 1) nor a {'stages': [...]} "
            f"mapping (schema_version {fragment.get('schema_version', 1)!r})"
        )

    raw_stages = tier_value["stages"]
    if not raw_stages:
        raise RosterFragmentError(
            f"review roster fragment tier {tier!r} declares no stages"
        )

    stages: List[Stage] = []
    for index, raw_stage in enumerate(raw_stages):
        if not isinstance(raw_stage, dict):
            raise RosterFragmentError(
                f"review roster fragment tier {tier!r} stage {index} is "
                "not a mapping"
            )
        agents = raw_stage.get("agents")
        if not isinstance(agents, list) or not agents:
            raise RosterFragmentError(
                f"review roster fragment tier {tier!r} stage {index} "
                "declares no agents"
            )
        gate = bool(raw_stage.get("gate", False))
        if gate:
            can_block = any(
                blocking_verdicts.get(agent) is not None for agent in agents
            )
            if not can_block:
                raise RosterFragmentError(
                    f"review roster fragment tier {tier!r} stage {index} "
                    "is gate: true but contains no agent that can block "
                    "(check 'blocking_verdicts')"
                )
        stages.append(Stage(agents=list(agents), gate=gate))

    return stages
