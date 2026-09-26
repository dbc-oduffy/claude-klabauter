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
      "schema_version": 4,
      "blocking_verdicts": {
        "coordinator:prior-art-checker": "BLOCKED-SURFACE-TO-PM",
        "coordinator:docs-checker": null,
        ...
      },
      "tiers": {
        "standard": {
          "stages": [
            {"gate": true, "agents": ["coordinator:prior-art-checker"],
             "accepts_signals": "preflight"},
            {"agents": ["coordinator:code-reviewer", "coordinator:staff-eng"],
             "accepts_signals": "named"},
            {"agents": ["coordinator:review-integrator"]}
          ]
        }
      }
    }

``accepts_signals`` (schema_version >= 4) marks the stage that absorbs
signal-selected agents sharing its label (``"preflight"`` or ``"named"``) —
see ``parse_stages``'s ``signals`` parameter and § tier cap below.

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
from typing import Dict, List, Optional


_PERSONA_AGENT_TYPES = frozenset(
    {
        "coordinator:staff-eng",
        "coordinator:staff-data-sci",
        "coordinator:senior-front-end",
        "coordinator:staff-ux",
        "coordinator:eng-director",
        "coordinator:vp-product",
        "coordinator:apm",
    }
)

_PERSONA_CAP_BY_TIER = {"full": 3}
_DEFAULT_PERSONA_CAP = 2


class RosterFragmentError(ValueError):
    pass


@dataclass(frozen=True)
class Stage:

    agents: List[str]
    gate: bool
    accepts_signals: Optional[str] = None


def parse_stages(
    fragment: dict,
    tier: str,
    signals: Optional[Dict[str, List[str]]] = None,
) -> List[Stage]:
    """Parse ``fragment``'s ``tier`` entry into an ordered list of ``Stage``.

    ``fragment`` is the already-parsed, top-level review-roster-fragment
    dict (``schema``, optionally ``blocking_verdicts``, and ``tiers``) — the
    same dict shape ``dispatch_emit._reviewers_for_tier`` took, generalised
    to the staged (schema_version >= 2) shape and the top-level
    ``blocking_verdicts`` map v3 adds and ``accepts_signals`` v4 adds. No
    file I/O, no cross-repo pointer resolution: see module docstring.

    Each tier's value in ``fragment["tiers"]`` is either:

    - a flat list of ``agentType`` strings (schema_version 1) — read as a
      single ``Stage(agents=..., gate=False)``, preserving today's flat
      behaviour exactly;
    - a ``{"stages": [...]}`` mapping (schema_version >= 2) — each entry is
      a dict with a non-empty ``agents`` list, an optional ``gate``
      (defaults False), and an optional ``accepts_signals`` (schema_version
      >= 4; one of ``"preflight"``/``"named"``), read in order into one
      ``Stage`` apiece.

    ``signals`` (schema_version >= 4 callers only; ``None``/omitted is a
    no-op, preserving pre-v4 behaviour exactly) maps a stage label
    (``"preflight"``/``"named"``) to the ``agentType`` strings signal
    selection resolved for that label (DoE's ``review-signals.json``
    ``selects``/``stage`` pair, resolved by the caller — this module does no
    signal-name lookup). Each stage whose own ``accepts_signals`` matches a
    key present in ``signals`` gets that key's agents appended (skipping any
    already present), in order.

    **Tier cap.** Signal selection does not bypass the PM's size ruling
    (state/cross-repo/inbox/2026-09-01-example-game-repo-em-review-roster-signal-
    selection-uncapped-by-size.md): "three Opus reviewers shouldn't happen
    on a plan unless it is XL or XXL in size." Persona-band (Opus) agents —
    see ``_PERSONA_AGENT_TYPES`` — merging in from ``signals`` are dropped,
    in order, once the tier's persona total (rostered + already-merged)
    would reach its cap: 3 for ``full`` (XL/XXL), 2 for every other tier.
    Already-rostered personas are never dropped — only signal-selected
    additions route around the cap, never through it.

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
        accepts_signals = raw_stage.get("accepts_signals")
        if not isinstance(accepts_signals, str):
            accepts_signals = None
        stages.append(
            Stage(agents=list(agents), gate=gate, accepts_signals=accepts_signals)
        )

    _merge_signals(stages, tier, signals)

    return stages


def _merge_signals(
    stages: List[Stage],
    tier: str,
    signals: Optional[Dict[str, List[str]]],
) -> None:
    if not signals:
        return

    persona_count = sum(
        1
        for stage in stages
        for agent in stage.agents
        if agent in _PERSONA_AGENT_TYPES
    )
    cap = _PERSONA_CAP_BY_TIER.get(tier, _DEFAULT_PERSONA_CAP)

    for stage in stages:
        if stage.accepts_signals is None:
            continue
        selected = signals.get(stage.accepts_signals)
        if not selected:
            continue
        for agent in selected:
            if agent in stage.agents:
                continue
            if agent in _PERSONA_AGENT_TYPES:
                if persona_count >= cap:
                    continue
                persona_count += 1
            stage.agents.append(agent)
