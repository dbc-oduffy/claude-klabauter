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
#: matches the DoE schema-of-record's object-shaped `recommendation` (AC-13
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
    and the DoE `decision-object.schema.json` schema-of-record).
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


def _validate_reportable(reportable: Any) -> None:
    """Raise ``ValueError`` unless *reportable* is ``None``, ``True``, or
    ``False``.

    Three-way, not two-way, by design (docs/plans/2026-08-15-judgment-
    points-that-gate-nothing-stop-being-questions.md, C1b correction): a
    judgment point's `resolves` naming no live directive id does NOT by
    itself mean "answering it changes nothing" -- some points are executed
    by the EM directly, with no directive and no gate (channel 3 in the
    plan's premise-finding sidecar). `reportable` is the explicit,
    author-supplied classification that makes "gate-nothing" and "safe to
    demote into narration" two separate facts instead of one inferred from
    the other:

    - `True` -- acknowledgement-class. The EM merely notes this; demoting
      it into `narration` loses nothing.
    - `False` -- action-class, explicitly decided. The EM must still DO
      something on the answer even though no directive gates it; this
      keeps the point in `judgment_points[]` while recording that the
      omission from `True` was a deliberate call, not an oversight.
    - `None` (default) -- unclassified. Never treated as "safe to demote"
      -- absence must never mean safety, only "nobody has decided yet".
      A census test (`test_reportable_partition.py`) fails the build on an
      unclassified gate-nothing recommendation-carrying point so this
      cannot rot back into a silent inference.
    """
    if reportable is not None and not isinstance(reportable, bool):
        raise ValueError(
            "build_judgment_point: reportable must be None, True, or False, "
            f"got {type(reportable).__name__}"
        )


def _validate_resolves_computed(resolves_computed: Any) -> None:
    """Raise ``ValueError`` unless *resolves_computed* is a bool.

    Records a different fact from `reportable`, and conflating the two would
    be wrong. `reportable` answers "does the EM have to act on the answer?" --
    a static property of the question. `resolves_computed` answers "is this
    point's `resolves` populated from a `decisions` slice?", so an empty one
    means *not yet populated* rather than *gates nothing*. Such a point
    genuinely does gate directives; the emptiness is incidental to the data
    state at build time, and each resolver's own docstring calls that emission
    deliberate ("an honest 'this dispatches nothing', not a phantom id").

    Legal ONLY on a point whose `resolves` is genuinely resolver-populated.
    It is not a general escape hatch from classification -- a census test pins
    exactly which points may carry it, so it cannot be reached for to silence
    a real unclassified point.
    """
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
    # Omitted entirely when unclassified, same as `build_disposition` does with
    # `guidance` and for the same reason: every existing caller's output stays
    # byte-identical, so the key-set pins across the fleet (baton_assemble's in
    # particular) keep catching real shape drift instead of tripping on this.
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
    """Construct a judgment point that may carry a recommendation.

    `recommendation` is a required positional argument -- a trusted caller
    must explicitly decide (even if the decision is `None` for "no opinion").
    When non-null it must be an object shaped exactly `{disposition: str,
    rationale: str}` -- the canonical schema-of-record shape (a bare string
    is rejected with `ValueError`, not silently accepted).

    `reportable` (default `None`, keyword-only) is the explicit
    demotion-eligibility marker `partition_reportable` reads -- see
    `_validate_reportable`'s docstring for the three-way semantics. Left
    `None` by default so an existing caller that never opts in stays
    exactly where it was (asked), never silently reclassified.
    """
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
    """Construct a judgment point for an untrusted gate.

    Negative-spec: there is deliberately NO `recommendation` parameter on
    this constructor -- not a default of `None`, an absent parameter. A
    caller cannot pass `recommendation=...` here at all (it would raise
    `TypeError: unexpected keyword argument`); `recommendation` is always
    emitted as `None` in the resulting item. This is what "offer, never
    verdict" means structurally rather than by convention.

    `reportable` carries the same three-way semantics as
    `build_judgment_point`'s -- see `_validate_reportable`.
    """
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
    """Returns (asked, reported). A point is `reported` ONLY when BOTH:
    (1) no disposition names a directive id present in `directives`
    (gate-nothing on the directive axis), AND (2) the point is explicitly
    marked `reportable=True` (see `build_judgment_point`'s `reportable`
    kwarg / `_validate_reportable`).

    Condition (2) is not redundant with (1) (docs/plans/2026-08-15-
    judgment-points-that-gate-nothing-stop-being-questions.md, C1b
    correction, premise-finding sidecar): "gates nothing" was originally
    read as "answering it changes nothing", but a judgment point can be
    gate-nothing on the directive axis and still be executed by the EM
    directly -- no directive, no gate, channel 3 in that sidecar's
    taxonomy. Demoting a channel-3 point into narration silences a real
    decision. `reportable` makes classification a deliberate authoring act
    instead of an inference from directive-membership alone: `True` opts a
    point into demotion, `False` explicitly keeps it asking (a decided
    action-class point, not an overlooked one), and `None` (unclassified)
    is NEVER treated as safe to demote -- absence must never mean safety.

    A point whose id is named in ANY directive's `depends_on` is NEVER
    classified `reported`, even when no disposition names a live directive
    id and even when marked `reportable=True` -- `contract/apply_base.py`
    fails open on a `depends_on` naming an absent judgment point, so
    demoting such a point would silently unblock its dependents.
    General-case guard, not a fix for a known instance.

    A phantom `resolves` id (naming a directive absent from the envelope
    entirely) is out of this predicate's vocabulary -- that is
    `ceremony_common/phantom_resolves_sweep.py`'s jurisdiction, and this
    function must not classify it as `reported`.

    This function does not attempt to distinguish a phantom `resolves` id
    from a deliberate absent-directive one (e.g. `jp-coverage-verdict`'s
    back-compat `d-write-trail` reference) -- both are, structurally,
    "names no directive id present in `directives`" from this predicate's
    point of view. `phantom_resolves_sweep.py` is what keeps an accidental
    one from ever reaching this function in a shipped envelope; duplicating
    that check here would be a second, competing mechanism for the same
    fact, which is exactly what this function must not become.
    """
    # Function-local: keeps `apply_base` off every brief-path import of this
    # shape module. `depends_on` is legally a bare string, so the canonical
    # normalizer is the only safe read -- iterating the raw value walks it
    # character by character and the precondition guard below fails open.
    from coordinator_core.contract.apply_base import normalize_depends_on

    directive_ids, depended_on_ids = _directive_axis(directives, normalize_depends_on)

    asked: list[dict[str, Any]] = []
    reported: list[dict[str, Any]] = []
    for point in judgment_points:
        # PRECONDITION (a): gate-nothing alone is insufficient -- demotion
        # additionally requires an explicit `reportable=True` opt-in. A
        # `False` (explicitly action-class) or `None` (unclassified) marker
        # both keep the point asked -- only their `is True` truth value
        # differs in intent, not in outcome here.
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
    """The directive axis, computed once and used by `partition_reportable`
    (via this helper) -- `find_unclassified_gate_nothing_points` deliberately
    does NOT call this; it applies a looser any-directive-at-all test, and
    that asymmetry is intentional and documented on its own docstring.

    A point named in any directive's `depends_on` is never gate-nothing:
    `contract/apply_base.py` fails open on a `depends_on` naming an absent
    judgment point, so treating such a point as gating nothing would silently
    unblock its dependents.
    """
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
    """Returns the recommendation-carrying points that gate nothing on the
    directive axis and carry no `reportable` classification at all.

    This is the defect `_emit` raises on. Both marked states are legal: a
    `True` point was demoted into `narration`, a `False` point is asked on
    purpose. Only `None` means nobody decided.

    A `resolves_computed=True` point is skipped: its `resolves` is populated
    from a `decisions` slice, so an empty one means "not yet populated", not
    "gates nothing". Demanding a static classification there would force a
    wrong answer rather than surface a missing one.

    Deliberately stricter than `partition_reportable`'s gate-nothing test, and
    the asymmetry is the point. `partition_reportable` asks "does this name a
    directive present on THIS envelope", which is the right question for
    demotion. `_emit` sees one envelope and must not over-fire, so it asks
    whether the point names any directive id AT ALL: a point naming a
    conditionally-emitted directive (`commit-significance-filter` names
    `d-complete-entry`) is unremarkable on the envelopes where that directive
    was not emitted, and raising there would fail the ceremony over a data
    state rather than an authoring omission. A `resolves` id naming a
    directive that exists nowhere is a phantom, and stays
    `ceremony_common/phantom_resolves_sweep.py`'s jurisdiction. The census
    test is where full-variant coverage lives; this is the runtime backstop.
    """
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
