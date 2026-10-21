"""Pinned evidence contract and pure divergence/residue decision for baton close.

Evidence readers emit plain dicts of the shape
``{"kind", "reads": "terminal"|"live", "strength": "strong"|"weak",
"proposes": "shipped"|"continued"|"abandoned"|None, "source", "detail"}``
and import nothing from here. ``decide`` does no I/O and spawns nothing; the
caller computes ``predicate_refusal``.
"""

from __future__ import annotations

SCHEMA_VERSION: int = 1

SIZING_TERMINAL = "SIZING_TERMINAL"
SIZING_DECLINED = "SIZING_DECLINED"
SIZING_MISSING = "SIZING_MISSING"
RUN_COMMITTED = "RUN_COMMITTED"
RUN_HALTED = "RUN_HALTED"
PLAN_IMPLEMENTED = "PLAN_IMPLEMENTED"
PLAN_SPINE_DONE = "PLAN_SPINE_DONE"
SUCCESSOR_CLAIMED = "SUCCESSOR_CLAIMED"
# The never-started shape that handoff.archive_transition mode=supersede refuses:
# a terminal successor says nothing about a baton nobody claimed, so it is weak
# and proposes "abandoned", which is a human call (DR-242).
SUCCESSOR_UNCLAIMED = "SUCCESSOR_UNCLAIMED"
SUCCESSOR_LIVE = "SUCCESSOR_LIVE"
MERGED_BRANCH = "MERGED_BRANCH"

# kind -> (reads, strength, proposes)
KIND_SHAPES: dict[str, tuple[str, str, str | None]] = {
    SIZING_TERMINAL: ("terminal", "strong", "shipped"),
    SIZING_DECLINED: ("terminal", "weak", "abandoned"),
    SIZING_MISSING: ("live", "weak", None),
    RUN_COMMITTED: ("terminal", "strong", "shipped"),
    RUN_HALTED: ("live", "strong", None),
    PLAN_IMPLEMENTED: ("terminal", "strong", "shipped"),
    PLAN_SPINE_DONE: ("terminal", "weak", "shipped"),
    SUCCESSOR_CLAIMED: ("terminal", "strong", "continued"),
    SUCCESSOR_UNCLAIMED: ("terminal", "weak", "abandoned"),
    SUCCESSOR_LIVE: ("live", "weak", None),
    MERGED_BRANCH: ("terminal", "weak", "shipped"),
}


def _kinds(items: list[dict]) -> str:
    return ", ".join(sorted({str(e.get("kind")) for e in items}))


def _residue(reason: str) -> dict:
    return {"verdict": "residue", "proposes": None, "reason": reason}


def decide(
    baton_rel: str,
    fm: dict,
    evidence: list[dict],
    predicate_refusal: str | None,
) -> dict:
    """Return ``{"verdict": "divergence"|"residue"|"no_evidence", "proposes", "reason"}``.

    ``divergence`` only when strong terminal evidence exists, none reads live,
    all strong evidence proposes one terminal other than ``abandoned``, and
    ``predicate_refusal`` is None. Any other baton carrying evidence is residue
    with a non-empty reason.
    """
    if not evidence:
        return {"verdict": "no_evidence", "proposes": None, "reason": None}

    terminal = [e for e in evidence if e.get("reads") == "terminal"]
    live = [e for e in evidence if e.get("reads") == "live"]

    if not terminal:
        return _residue(f"still live: {_kinds(live or evidence)}")
    if live:
        return _residue(f"conflicting evidence: {_kinds(evidence)}")

    strong = [e for e in terminal if e.get("strength") == "strong"]
    if not strong:
        return _residue(f"weak evidence only: {_kinds(terminal)}")

    proposed = {e.get("proposes") for e in strong}
    if len(proposed) != 1 or None in proposed:
        return _residue(f"conflicting evidence: {_kinds(strong)}")
    proposes = next(iter(proposed))

    if proposes == "abandoned":
        return _residue("abandoned is a human call (DR-242)")
    if predicate_refusal is not None:
        return _residue(f"predicate refused: {predicate_refusal}")
    return {"verdict": "divergence", "proposes": proposes, "reason": None}
