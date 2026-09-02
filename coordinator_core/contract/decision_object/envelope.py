"""The decision-object envelope: 8-key schema + the `_emit` fail-loud chokepoint.

Conformance target (the DoE schema-of-record): `schemas/decision-object.schema.json`
in DoE-claude is the contract-of-record (DR-047). `ENVELOPE_KEYS` below encodes
that schema's 8 canonical top-level keys as a module constant rather than
coupling this package to a cross-repo file path -- the conformance suite
(`coordinator_core/tests/test_decision_object_envelope.py`) asserts this
package's produced/accepted key set matches that authority by name.

Negative-spec: do not add a 9th key here without first updating the DoE-side
schema-of-record and re-deriving `ENVELOPE_KEYS` from it -- this constant is a
mirror of that schema, not an independent source of truth.

Spec backlink: docs/plans/2026-07-21-canonical-resolution-engine.md (Wave 1,
chunk W1-A2). [DEAD-CITATION: plan file never committed to this repo]
"""

from __future__ import annotations

import enum
from typing import Any, Mapping, Type

# Module-level: `_emit` runs on every envelope build (it IS the brief path),
# unlike `partition_reportable`'s function-local import of `apply_base`,
# which exists specifically to keep `apply_base` off that path. No cycle:
# `judgment.py` has no module-level imports of its own besides `typing`, and
# `apply_base.py` imports neither `envelope` nor `judgment` (Review:
# code-reviewer -- hoisted from a cargo-culted function-local import).
from coordinator_core.contract.decision_object.judgment import (
    find_unclassified_gate_nothing_points,
)

ENVELOPE_KEYS: tuple[str, ...] = (
    "artifact",
    "preflight",
    "gates",
    "directives",
    "judgment_points",
    "decisions",
    "narration",
    "next_move",
)


class DecisionObjectError(ValueError):
    """Raised by `_emit` when a decision-object envelope is malformed."""


def build_envelope(
    *,
    artifact: Any = None,
    preflight: Any = None,
    gates: Any = None,
    directives: Any = None,
    judgment_points: Any = None,
    decisions: Any = None,
    narration: str = "",
    next_move: str = "",
) -> dict[str, Any]:
    """Construct a decision-object envelope with exactly the 8 canonical keys.

    Every keyword is optional and defaults to an empty/neutral value so
    partial computations (e.g. a preflight-only pass) can still emit a
    structurally valid envelope. Validate the result with `_emit` before
    returning it across a process boundary.
    """
    envelope = {
        "artifact": artifact if artifact is not None else {},
        "preflight": preflight if preflight is not None else {},
        "gates": gates if gates is not None else {},
        "directives": directives if directives is not None else [],
        "judgment_points": judgment_points if judgment_points is not None else [],
        "decisions": decisions if decisions is not None else {},
        "narration": narration,
        "next_move": next_move,
    }
    return envelope


def _emit(obj: Mapping[str, Any]) -> Mapping[str, Any]:
    """The single fail-loud chokepoint every decision-object producer routes through.

    Validates the 8-key envelope shape before any serialization/return.
    Raises `DecisionObjectError` on a malformed envelope -- callers must not
    catch-and-continue past this; a malformed envelope is a producer bug, not
    a recoverable runtime condition.
    """
    if not isinstance(obj, Mapping):
        raise DecisionObjectError(
            f"decision-object envelope must be a mapping, got {type(obj).__name__}"
        )

    actual_keys = set(obj.keys())
    expected_keys = set(ENVELOPE_KEYS)

    missing = expected_keys - actual_keys
    extra = actual_keys - expected_keys
    if missing or extra:
        raise DecisionObjectError(
            "decision-object envelope key mismatch: "
            f"missing={sorted(missing)} extra={sorted(extra)} "
            f"expected={sorted(expected_keys)}"
        )

    unclassified = find_unclassified_gate_nothing_points(
        obj.get("judgment_points") or [], obj.get("directives") or []
    )
    if unclassified:
        ids = ", ".join(sorted(str(p.get("id")) for p in unclassified))
        raise DecisionObjectError(
            f"judgment point(s) {ids} carry a recommendation, gate no directive "
            "on this envelope, and set no `reportable`. Pass reportable=True at "
            "the builder if the EM only notes the answer, or reportable=False if "
            "the EM must act on it."
        )

    return obj


# Public alias -- the conformance suite and external callers import `emit`;
# `_emit` is the internal chokepoint name called out in the spec.
emit = _emit


# ---------------------------------------------------------------------------
# Reader of a PERSISTED envelope's judgment-point shape.
#
# `apply_base.judgment_points_by_id` is the in-process sibling: it takes the
# `judgment_points` list this process just built and indexes it without shape
# checks, because a producer bug there should raise. This one takes an object
# re-read from `.git/coordinator-sessions/decisions/*.json`, where a malformed
# entry is a data state rather than a producer bug, and it lives beside the
# writer so a rename of `judgment_points` or a point's `id` moves one site
# instead of silently starving one of its two readers (`resume` +
# `pickup_assemble.apply`).
#
# Review: overengineering-reviewer -- `legal_disposition_values` used to live
# here too on the same two-readers argument, but it has exactly one
# production caller (`resume.resume_decisions`, the file it was extracted
# from); moved back to `resume.py` as `_legal_disposition_values`, private to
# its sole reader.
# ---------------------------------------------------------------------------

def judgment_points_by_id(
    decision_object: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    """Index a persisted decision object's `judgment_points[]` by `id`.

    Pure extraction, no policy: a non-list `judgment_points`, a non-mapping
    entry, or an entry with a falsy `id` is skipped, and the caller keeps its
    own register on top -- `resume.resume_decisions` refuses loudly on an
    answer naming an id absent from the returned map, while
    `pickup_assemble.apply._read_session_dispositions` reads an empty map as
    "nothing to add". Neither register belongs here.

    NOT `apply_base.judgment_points_by_id` -- that in-process sibling takes
    the `list` this process just built (not a persisted `Mapping`) and raises
    on a malformed entry rather than skipping it. Same base name, opposite
    argument shape, opposite malformed-entry behavior -- import this one by
    its full qualified path or an explicit alias, never a bare
    `judgment_points_by_id` re-export, so a reader can't reach for the wrong
    one. (Review: code-reviewer -- Finding 4, naming-collision hazard.)
    """
    points = decision_object.get("judgment_points")
    if not isinstance(points, list):
        return {}
    return {
        point["id"]: point
        for point in points
        if isinstance(point, Mapping) and point.get("id")
    }


class ExitCodeBase(enum.IntEnum):
    """Base exit-code enumeration pattern shared by every skill's CLI.

    `SUCCESS = 0` is the only fixed member. A skill defines its own,
    specific exit codes via `extend_exit_codes` rather than this module
    hardcoding any one skill's (e.g. pickup's) exit-code set.
    """

    SUCCESS = 0


def extend_exit_codes(name: str, **codes: int) -> Type[enum.IntEnum]:
    """Build a skill-specific exit-code IntEnum anchored at `SUCCESS = 0`.

    Example: `extend_exit_codes("PickupExitCode", BLOCKED=1, STALE_CLAIM=2)`.

    Raises `ValueError` if `codes` contains a `SUCCESS` key -- `SUCCESS = 0`
    is structurally the only fixed member; silently accepting a caller's
    `SUCCESS=...` override (via `dict.update`) would let a typo'd call site
    clobber the anchor with no error (Review: code-reviewer -- Finding 4).
    """
    if "SUCCESS" in codes:
        raise ValueError(
            "extend_exit_codes: 'SUCCESS' is a fixed member (always 0) and "
            "cannot be overridden via **codes"
        )
    members: dict[str, int] = {"SUCCESS": int(ExitCodeBase.SUCCESS)}
    members.update(codes)
    return enum.IntEnum(name, members)
