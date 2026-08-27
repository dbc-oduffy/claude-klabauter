"""
coordinator_core.orient_assemble — the `orient-assemble` computed-skill engine.

Purpose: one cadence-parameterized read-only compute replacing the three
duplicated orient spines (`workday-start.md`, `workweek-start.md`,
`workstream-start/SKILL.md`). Cadence is a parameter, never three code
paths — `brief(cadence)` tunes severity/depth knobs (day = red-and-stale +
reap; session = red-only + warn; week = lighter) over ONE shared compute,
per `docs/plans/2026-07-24-computed-skills-b2-ceremony-start.md` § Approach.

Contract (frozen, reviewed): DoE-claude coordinator/docs/wiki/computed-skills.md
Spec backlink: DoE-claude:pln-computed-skills-b2-ceremony-st-e82420, chunk C1
Registration seam: this module ships no bash veneer — it is consumed
directly by the `coordinator/bin/orient-assemble` trampoline, mirroring
`coordinator_core.pickup_assemble`'s template-variant #1 shape.

Reader wiring (integration pass, follow-up to C1-C2d): `brief()` calls each
reader family's `collect(cadence)` (readers_clean_ops, readers_handoff_triage,
readers_branch_reconcile, readers_health_reaper) and concatenates their
`directives`/`judgment_points` into the envelope. Cadence-scoping (e.g. the
health-reaper family's day-cadence-only handoff-archival probe) is decided
inside each reader's own `collect()`, never re-decided here — this seam
calls all four unconditionally for every cadence and trusts each reader to
self-gate.

READ-ONLY, by construction: this module (both now and once C2a-C2d land)
only reads disk/git state. Mutating actions are returned as `directives[]`
entries naming an existing atomic CLI — this module never shells out to a
mutating verb, never writes a file, and never runs `git fetch`.

Envelope discipline (AC per C1): every exit routes through the SHIPPED
`coordinator_core.contract.decision_object.envelope.build_envelope`/`_emit`
chokepoint — this module does NOT re-derive its own key-validation
chokepoint the way `coordinator_core.pickup_assemble` (pre-dating the
extracted contract library) does. A decision object is returned on every
exit, never a bare exit code.

Negative-spec:
    - Do NOT add a mutating code path here — a finding that "the assembler
      should just do X" for any X that writes to disk belongs in a
      `directives[]` entry, not a new function body in this module.
    - Do NOT re-derive `_emit`/`build_envelope` locally (the pattern
      `coordinator_core.pickup_assemble` uses) — import and call the
      shipped `contract/decision_object/envelope.py` chokepoint (AC of
      this chunk; see § computed-skills.md's "consumes, does not rebuild"
      clause).
    - Do NOT reshape a reader's own `directives`/`judgment_points` entries
      inside this module — normalize only at this seam (concatenation), not
      by mutating a reader's dict shape; every reader already emits the
      same `ReaderResult` shape, so no per-reader translation is needed.
"""
from __future__ import annotations

import json
import sys
from typing import Any

from coordinator_core.contract.decision_object.envelope import (
    ExitCodeBase,
    build_envelope,
    extend_exit_codes,
    _emit as _envelope_emit,
)
from coordinator_core.contract.decision_object.judgment import partition_reportable
from coordinator_core.orient_assemble import (
    readers_branch_reconcile,
    readers_clean_ops,
    readers_handoff_triage,
    readers_health_reaper,
)

#: The four reader families this seam wires into `brief()`. Each exposes
#: `collect(cadence) -> ReaderResult` (directives + judgment_points); cadence
#: self-gating (e.g. readers_health_reaper's day-only reaper probe) lives
#: inside each reader's own `collect()`, not here.
_READER_MODULES = (
    readers_clean_ops,
    readers_handoff_triage,
    readers_branch_reconcile,
    readers_health_reaper,
)

#: The three cadences `orient-assemble brief --cadence` accepts. Cadence
#: tunes severity/depth knobs over ONE shared compute — it is never a
#: branch into three separate code paths (Approach § "Cadence is a
#: parameter, not three code paths").
CADENCES: tuple[str, ...] = ("session", "day", "week")

#: Exit-code contract, locally scoped to this CLI (mirrors
#: `coordinator_core.pickup_assemble`'s own locally-scoped enumeration —
#: NOT inherited from any house convention; see envelope.py's
#: `extend_exit_codes` docstring).
OrientExitCode = extend_exit_codes("OrientExitCode", USAGE=2, TRANSPORT_FAIL=3)

#: DoE schema-of-record (`decision-object.schema.json`) constraint: when a
#: judgment point's `recommendation` is `null`, `reason` MUST be one of
#: these two enum values — free text is only schema-legal when `recommendation`
#: is non-null. Every reader family here builds its no-verdict judgment
#: points via `build_judgment_point(None, ..., reason=<free text>)`, which is
#: schema-valid at the constructor level (the constructor doesn't enforce
#: this conditional) but fails `jsonschema.validate` once assembled into a
#: real envelope. Normalized here at the merge seam, not inside any reader.
_SCHEMA_LEGAL_NULL_RECOMMENDATION_REASONS = frozenset(
    {"insufficient-evidence", "recommendation-forbidden"}
)


def _assert_null_recommendation_reason_legal(judgment_point: dict[str, Any]) -> dict[str, Any]:
    """Fail loud if a judgment point's `reason` is schema-illegal when
    `recommendation` is null.

    Note on `evidence` (Review: code-reviewer — Finding 5): several reader
    families' no-verdict judgment points append a free-text human rationale
    onto `evidence` via `f"{evidence} | reason: <prose>"` string
    concatenation (see `readers_clean_ops._read_memo_surface`/
    `_read_worktree_sweep`, `readers_branch_reconcile._read_auto_reconcile`,
    `readers_health_reaper._read_exec_bit_check`/`_read_marker_freshness`).
    This is a deliberate stopgap: the schema's `reason` field on a
    null-recommendation judgment point is constrained to the two enum
    values this function checks (`insufficient-evidence`/
    `recommendation-forbidden`), leaving no structured field for the human
    explanation of *why* recommendation is forbidden. `evidence` is where
    that rationale lives instead. Do NOT "clean up" the `| reason: ...`
    suffix out of `evidence` thinking it should be the bare evidence
    string alone — that rationale has no other home until the schema gains
    a dedicated field.

    Each reader family is responsible for classifying its OWN no-verdict
    judgment points at source (`"insufficient-evidence"` when the engine
    genuinely lacks data to form a verdict, `"recommendation-forbidden"`
    when it could form one but must not — a PM/human call). This seam does
    not coerce or guess on a reader's behalf; a reader that emits free text
    here is a schema violation in that reader, not something to paper over
    at merge time. Returns the judgment point unchanged when legal.
    """
    if judgment_point.get("recommendation") is not None:
        return judgment_point
    reason = judgment_point.get("reason", "")
    if reason in _SCHEMA_LEGAL_NULL_RECOMMENDATION_REASONS:
        return judgment_point
    raise ValueError(
        f"orient_assemble: judgment point {judgment_point.get('id')!r} has "
        f"recommendation=null but reason={reason!r}, which is not one of "
        f"{sorted(_SCHEMA_LEGAL_NULL_RECOMMENDATION_REASONS)} — the "
        "originating reader must set `reason` to one of those two enum "
        "values at source, moving its free-text explanation into `evidence`."
    )


def brief(cadence: str) -> dict[str, Any]:
    """Compute the cadence-parameterized orient decision object.

    Calls all four reader families' `collect(cadence)` and concatenates
    their `directives`/`judgment_points` into the emitted envelope. Each
    reader family self-gates its own cadence scope (e.g. the health-reaper
    family's day-cadence-only handoff-archival probe) — this seam calls all
    four unconditionally for every cadence. Read-only; performs no disk
    mutation and no git fetch (the reap-family's one accepted `--dry-run`
    subprocess is documented in `readers_health_reaper.py`).

    Raises `ValueError` for a `cadence` outside `CADENCES`, matching
    `backlog_grind_assemble.brief`'s contract. `main()` below validates too,
    so no CLI invocation could ever reach here with a bad value — but every
    reader self-gates by comparing `cadence` against its own scope, so an
    unrecognized string is not inert: it silently matches no gate anywhere
    and returns the session spine under another cadence's name. A direct
    `brief()` caller got a plausible-looking payload for a cadence that does
    not exist. Failing here costs nothing and removes the shape entirely.
    """
    if cadence not in CADENCES:
        raise ValueError(
            f"orient-assemble: unrecognized cadence {cadence!r}; "
            f"must be one of {CADENCES}"
        )

    directives: list[dict[str, Any]] = []
    judgment_points: list[dict[str, Any]] = []
    for reader in _READER_MODULES:
        result = reader.collect(cadence)
        directives.extend(result.directives)
        judgment_points.extend(result.judgment_points)
    judgment_points = [
        _assert_null_recommendation_reason_legal(jp) for jp in judgment_points
    ]

    # Partition at this seam only, over the FULL concatenated directive set
    # (see module docstring's "Reported-point demotion" note): a point is
    # `reported` when it gates no live directive and is not depended on by
    # one. Asked points stay in `judgment_points[]`; reported points are
    # demoted into narration prose so the drift is still visible without
    # asking the EM to answer for something that gates nothing.
    # Scoped to recommendation-carrying points: a Tier-3 point
    # (`recommendation=None`, reason `recommendation-forbidden` or
    # `insufficient-evidence`) is a question the engine deliberately must not
    # answer, so it keeps asking however few directives it gates --
    # `j-session-day-review-due` is the live instance, and demoting it would
    # silence a due review rather than de-noise a settled one.
    recommendation_carrying = [
        jp for jp in judgment_points if jp.get("recommendation") is not None
    ]
    _, reported_points = partition_reportable(recommendation_carrying, directives)
    reported_ids = {jp.get("id") for jp in reported_points}
    total_found = len(judgment_points)
    judgment_points = [
        jp for jp in judgment_points if jp.get("id") not in reported_ids
    ]

    narration = (
        f"orient-assemble brief --cadence {cadence}: "
        f"{len(directives)} directive(s), {len(judgment_points)} "
        f"judgment point(s) asked across four reader families "
        f"({total_found} found total)."
    )
    if reported_points:
        reported_fragments = "; ".join(
            f"{point.get('id', '')} ({point.get('question', '')}) — "
            f"{(point.get('recommendation') or {}).get('rationale', '')}"
            for point in reported_points
        )
        narration += f" Reported (gate nothing, not asked): {reported_fragments}."

    envelope = build_envelope(
        artifact={"cadence": cadence},
        preflight={},
        gates={},
        directives=directives,
        judgment_points=judgment_points,
        decisions={},
        narration=narration,
        next_move=(
            "Review directives[] and judgment_points[] below."
            if (directives or judgment_points)
            else "No findings from any reader family this pass."
        ),
    )
    return dict(_envelope_emit(envelope))


def _usage() -> int:
    """Print a usage error and return the shipped envelope's `_emit`-backed
    usage exit — a decision object is emitted on every exit, including a
    usage error, never a bare exit code (contract § round-trip
    classification)."""
    envelope = build_envelope(
        narration="orient-assemble: usage error.",
        next_move=(
            "Run: orient-assemble brief --cadence {session|day|week}"
        ),
    )
    print(json.dumps(dict(_envelope_emit(envelope)), indent=2, sort_keys=True))
    print(
        "usage: orient-assemble brief --cadence {session|day|week}",
        file=sys.stderr,
    )
    return int(OrientExitCode.USAGE)


def main(argv: list[str]) -> int:
    """`orient-assemble brief --cadence {session|day|week}` CLI entrypoint."""
    if argv[:1] and argv[0] in ("--help", "-h"):
        print("usage: orient-assemble brief --cadence {session|day|week}")
        return int(OrientExitCode.SUCCESS)

    if not argv or argv[0] != "brief":
        return _usage()

    rest = argv[1:]
    cadence: str | None = None
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "--cadence":
            if i + 1 >= len(rest):
                return _usage()
            cadence = rest[i + 1]
            i += 2
        else:
            print(f"orient-assemble: unrecognized argument {tok!r}", file=sys.stderr)
            return _usage()

    if cadence not in CADENCES:
        print(
            f"orient-assemble: --cadence must be one of {CADENCES}, got {cadence!r}",
            file=sys.stderr,
        )
        return _usage()

    decision_object = brief(cadence)
    print(json.dumps(decision_object, indent=2, sort_keys=True))
    return int(ExitCodeBase.SUCCESS)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
