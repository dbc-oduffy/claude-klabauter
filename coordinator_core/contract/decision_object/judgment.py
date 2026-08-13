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

#: `recommendation` carries exactly these two string fields when non-null —
#: matches the coordinator-claude schema-of-record's object-shaped `recommendation` (AC-13
#: cross-slice correction; see `coordinator_core.pickup_assemble`'s
#: `_RECOMMENDATION_FIELDS` for the pre-existing sibling shape this mirrors).
_RECOMMENDATION_FIELDS = frozenset({"disposition", "rationale"})


def _validate_recommendation(recommendation: Any) -> None:
    """Raise ``ValueError`` unless *recommendation* is ``None`` or an object
    shaped exactly ``{disposition: str, rationale: str}``.

    The canonical `recommendation` shape is an object (`{disposition,
    rationale}`) or `None` — never a bare string. A plain string used to be
    accepted here; that was wrong (Review: code-reviewer — cross-slice
    correction from S3, verified against `pickup_assemble.build_judgment_point`
    and the coordinator-claude `decision-object.schema.json` schema-of-record).
    """
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
    """Construct one `dispositions` entry: `{value, resolves}`, plus an
    optional `guidance` string describing what this option means and how to
    carry it out.

    `guidance` is descriptive, never a verdict -- it rides on the
    disposition, never on `recommendation` (which stays `None` on an
    untrusted-gate judgment point regardless of whether its options carry
    guidance). Omitted from the returned dict entirely when `None`, so every
    existing no-guidance caller's output stays byte-identical. A non-`str`,
    non-`None` value raises `ValueError`, matching `_validate_recommendation`'s
    fail-loud register. An empty or whitespace-only string is likewise
    rejected -- `None` still means "omit the key"; a supplied-but-vacuous
    string would leave a present key that says nothing, which is worse than
    an absent one (Review: code-reviewer -- overridden from `deferred`: this
    is a shared contract constructor whose purpose is to carry meaning to a
    reader).
    """
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
) -> dict[str, Any]:
    return {
        "id": id,
        "question": question,
        "dispositions": [dict(d) for d in dispositions],
        "evidence": evidence,
        "reason": reason,
        "recommendation": recommendation,
        "revalidate_at_dispatch": revalidate_at_dispatch,
        "round_trip": round_trip,
    }


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
) -> dict[str, Any]:
    """Construct a judgment point that may carry a recommendation.

    `recommendation` is a required positional argument -- a trusted caller
    must explicitly decide (even if the decision is `None` for "no opinion").
    When non-null it must be an object shaped exactly `{disposition: str,
    rationale: str}` -- the canonical schema-of-record shape (a bare string
    is rejected with `ValueError`, not silently accepted).
    """
    _validate_recommendation(recommendation)
    return _build_judgment_point_base(
        id=id,
        question=question,
        dispositions=dispositions,
        evidence=evidence,
        reason=reason,
        recommendation=recommendation,
        revalidate_at_dispatch=revalidate_at_dispatch,
        round_trip=round_trip,
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
) -> dict[str, Any]:
    """Construct a judgment point for an untrusted gate.

    Negative-spec: there is deliberately NO `recommendation` parameter on
    this constructor -- not a default of `None`, an absent parameter. A
    caller cannot pass `recommendation=...` here at all (it would raise
    `TypeError: unexpected keyword argument`); `recommendation` is always
    emitted as `None` in the resulting item. This is what "offer, never
    verdict" means structurally rather than by convention.
    """
    return _build_judgment_point_base(
        id=id,
        question=question,
        dispositions=dispositions,
        evidence=evidence,
        reason=reason,
        recommendation=None,
        revalidate_at_dispatch=revalidate_at_dispatch,
        round_trip=round_trip,
    )
