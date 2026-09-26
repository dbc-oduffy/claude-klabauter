"""Judgment-point constructors: offer-never-verdict enforced by construction.

A judgment point is the decision-object's mechanism for surfacing an
open question to a human or downstream trust boundary rather than silently
resolving it. Two constructors exist because the candor principle applies
asymmetrically:

- `build_judgment_point` may carry a `recommendation` -- a trusted caller
  (e.g. the EM) is allowed to say what it would do.
- `build_untrusted_gate_judgment_point` must NOT carry one -- an untrusted
  gate (a boundary this engine does not fully trust to judge its own
  situation) gets no `recommendation` parameter at all, so it is
  structurally impossible to attach a verdict to that judgment point. This
  is enforced by the function signature, not by a runtime check.

`build_disposition` may additionally carry `guidance` -- per-option text
describing what a disposition means and how to carry it out. Guidance rides
on the disposition, never on `recommendation`: it is descriptive, not a
verdict, and stays evenhanded across every option in a set rather than
front-loading toward one.

Extracted from the R1 partition-table candidate-general rows (constructor
contract + compute/apply split shape) -- deliberately excludes any
pickup-specific gate field.

Spec backlink: docs/plans/2026-07-21-canonical-resolution-engine.md (Wave 1,
chunk W1-A2). [DEAD-CITATION: plan file never committed to this repo]
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

#: `_RECOMMENDATION_FIELDS` for the pre-existing sibling shape this mirrors).
_RECOMMENDATION_FIELDS = frozenset({"disposition", "rationale"})


def _validate_recommendation(recommendation: Any) -> None:
    if recommendation is None:
        return
    if not isinstance(recommendation, Mapping):
        raise ValueError(
            "build_judgment_point: recommendation must be None or a mapping "
            f"{{'disposition', 'rationale'}}, got {type(recommendation).__name__}"
        )
    keys = set(recommendation)
    if keys != _RECOMMENDATION_FIELDS:
        raise ValueError(
            "build_judgment_point: recommendation must have exactly the keys "
            f"{sorted(_RECOMMENDATION_FIELDS)!r}, got {sorted(keys)!r}"
        )
    for key in _RECOMMENDATION_FIELDS:
        if not isinstance(recommendation[key], str):
            raise ValueError(
                f"build_judgment_point: recommendation[{key!r}] must be a str, "
                f"got {type(recommendation[key]).__name__}"
            )


def build_disposition(
    value: str, resolves: Sequence[str] = (), *, guidance: str | None = None
) -> dict[str, Any]:
    if guidance is not None and not isinstance(guidance, str):
        raise ValueError(
            f"build_disposition: guidance must be None or a str, got {type(guidance).__name__}"
        )
    if guidance is not None and not guidance.strip():
        raise ValueError(
            "build_disposition: guidance must not be empty or whitespace-only"
        )
    disposition = {"value": value, "resolves": list(resolves)}
    if guidance is not None:
        disposition["guidance"] = guidance
    return disposition


def _validate_reportable(reportable: Any) -> None:
    if reportable is not None and not isinstance(reportable, bool):
        raise ValueError(
            "build_judgment_point: reportable must be None, True, or False, "
            f"got {type(reportable).__name__}"
        )


def _validate_resolves_computed(resolves_computed: Any) -> None:
    if not isinstance(resolves_computed, bool):
        raise ValueError(
            "build_judgment_point: resolves_computed must be a bool, "
            f"got {type(resolves_computed).__name__}"
        )


def _build_judgment_point_base(
    *,
    id: str,
    question: str,
    dispositions: Sequence[Mapping[str, Any]],
    evidence: str,
    reason: str,
    recommendation: Mapping[str, str] | None,
    revalidate_at_dispatch: bool,
    round_trip: str,
    reportable: bool | None,
    resolves_computed: bool = False,
) -> dict[str, Any]:
    point = {
        "id": id,
        "question": question,
        "dispositions": [dict(d) for d in dispositions],
        "evidence": evidence,
        "reason": reason,
        "recommendation": recommendation,
        "revalidate_at_dispatch": revalidate_at_dispatch,
        "round_trip": round_trip,
    }
    if reportable is not None:
        point["reportable"] = reportable
    if resolves_computed:
        point["resolves_computed"] = True
    return point


def build_judgment_point(
    recommendation: Mapping[str, str] | None,
    *,
    id: str,
    question: str,
    dispositions: Sequence[Mapping[str, Any]],
    evidence: str,
    reason: str,
    revalidate_at_dispatch: bool = True,
    round_trip: str = "terminal",
    reportable: bool | None = None,
    resolves_computed: bool = False,
) -> dict[str, Any]:
    _validate_recommendation(recommendation)
    _validate_reportable(reportable)
    _validate_resolves_computed(resolves_computed)
    return _build_judgment_point_base(
        id=id,
        question=question,
        dispositions=dispositions,
        evidence=evidence,
        reason=reason,
        recommendation=recommendation,
        revalidate_at_dispatch=revalidate_at_dispatch,
        round_trip=round_trip,
        reportable=reportable,
        resolves_computed=resolves_computed,
    )


def build_untrusted_gate_judgment_point(
    *,
    id: str,
    question: str,
    dispositions: Sequence[Mapping[str, Any]],
    evidence: str,
    reason: str,
    revalidate_at_dispatch: bool = True,
    round_trip: str = "terminal",
    reportable: bool | None = None,
) -> dict[str, Any]:
    _validate_reportable(reportable)
    return _build_judgment_point_base(
        id=id,
        question=question,
        dispositions=dispositions,
        evidence=evidence,
        reason=reason,
        recommendation=None,
        revalidate_at_dispatch=revalidate_at_dispatch,
        round_trip=round_trip,
        reportable=reportable,
    )


def partition_reportable(
    judgment_points: Sequence[Mapping[str, Any]],
    directives: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    from coordinator_core.contract.apply_base import normalize_depends_on

    directive_ids, depended_on_ids = _directive_axis(directives, normalize_depends_on)

    asked: list[dict[str, Any]] = []
    reported: list[dict[str, Any]] = []
    for point in judgment_points:
        # PRECONDITION (a): gate-nothing alone is insufficient -- demotion
        demote = (
            _gates_nothing(point, directive_ids, depended_on_ids)
            and point.get("reportable") is True
        )
        (reported if demote else asked).append(dict(point))
    return asked, reported


def _directive_axis(
    directives: Sequence[Mapping[str, Any]], normalize_depends_on: Any
) -> tuple[set[Any], set[Any]]:
    directive_ids = {d.get("id") for d in directives}
    depended_on_ids: set[Any] = set()
    for directive in directives:
        depended_on_ids.update(normalize_depends_on(directive.get("depends_on")))
    return directive_ids, depended_on_ids


def _gates_nothing(
    point: Mapping[str, Any], directive_ids: set[Any], depended_on_ids: set[Any]
) -> bool:
    if point.get("id") in depended_on_ids:
        return False
    return not any(
        resolves_id in directive_ids
        for disposition in (point.get("dispositions") or [])
        for resolves_id in (disposition.get("resolves") or [])
    )


def find_unclassified_gate_nothing_points(
    judgment_points: Sequence[Mapping[str, Any]],
    directives: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    from coordinator_core.contract.apply_base import normalize_depends_on

    _, depended_on_ids = _directive_axis(directives, normalize_depends_on)
    return [
        dict(point)
        for point in judgment_points
        if point.get("recommendation") is not None
        and not point.get("resolves_computed")
        and point.get("reportable") is None
        and point.get("id") not in depended_on_ids
        and not any(
            disposition.get("resolves")
            for disposition in (point.get("dispositions") or [])
        )
    ]
