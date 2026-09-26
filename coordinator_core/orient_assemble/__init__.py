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
from pathlib import Path
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

_READER_MODULES = (
    readers_clean_ops,
    readers_handoff_triage,
    readers_branch_reconcile,
    readers_health_reaper,
)

CADENCES: tuple[str, ...] = ("session", "day", "week")

OrientExitCode = extend_exit_codes("OrientExitCode", USAGE=2, TRANSPORT_FAIL=3)

_SCHEMA_LEGAL_NULL_RECOMMENDATION_REASONS = frozenset(
    {"insufficient-evidence", "recommendation-forbidden"}
)


def _assert_null_recommendation_reason_legal(judgment_point: dict[str, Any]) -> dict[str, Any]:
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


def brief(cadence: str, *, repo_root: str | None = None) -> dict[str, Any]:
    """Compute the cadence-parameterized orient decision object.

    Calls all four reader families' `collect(cadence, repo_root=repo_root)`
    and concatenates their `directives`/`judgment_points` into the emitted
    envelope. Each reader family self-gates its own cadence scope (e.g. the
    health-reaper family's day-cadence-only handoff-archival probe) — this
    seam calls all four unconditionally for every cadence. Read-only;
    performs no disk mutation and no git fetch (the reap-family's one
    accepted `--dry-run` subprocess is documented in `readers_health_reaper.py`).
    This claim used to be false: `readers_health_reaper._read_hook_currency`
    ran `ensure_hooks_fleet` unconditionally, rewriting stale `prepare-commit-
    msg` hooks across every registered repo as a side effect of orienting a
    session. Fixed 2026-08-31 (C1+C2 of docs/plans/2026-08-31-orient-assemble-
    stops-running-a-fleet-re.md): that reader now calls the `--check-only`
    form, so this claim is a fact `test_readers_perform_no_disk_mutation.py`
    enforces, not prose the next reader added has to remember to keep true.

    `repo_root` is keyword-only and forwarded uniformly to every reader
    (mirrors `backlog_grind_assemble`'s `run_id` threading precedent — see
    this module's plan retraction on why `repo_root` itself is NOT copied
    from that module's shape). Defaults to `None`, meaning "resolve as
    today" — this chunk changes no existing caller's behaviour. Every
    reader accepts and ignores it in this chunk; C3-C7 consume it one
    reader at a time.

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
        try:
            result = reader.collect(cadence, repo_root=repo_root)
        except Exception as exc:  # noqa: BLE001 - see the block comment below
            print(
                f"orient-assemble: reader {reader.__name__.rsplit('.', 1)[-1]} "
                f"failed ({type(exc).__name__}: {exc}); its directives and "
                f"judgment points are absent from this brief",
                file=sys.stderr,
            )
            continue
        directives.extend(result.directives)
        judgment_points.extend(result.judgment_points)
    judgment_points = [
        _assert_null_recommendation_reason_legal(jp) for jp in judgment_points
    ]

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


def _resolve_target_root(target_root_arg: str | None) -> str:
    if target_root_arg is not None:
        resolved = Path(target_root_arg).resolve()
        if not resolved.is_dir():
            raise RuntimeError(
                f"--target-root {target_root_arg!r} is not a directory "
                f"(resolved to {resolved})"
            )
        return str(resolved)
    from coordinator_core.lifecycle import find_repo_root
    return str(find_repo_root())


def _usage() -> int:
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
    if argv[:1] and argv[0] in ("--help", "-h"):
        print(
            "usage: orient-assemble brief --cadence {session|day|week} "
            "[--target-root <path>]"
        )
        return int(OrientExitCode.SUCCESS)

    if not argv or argv[0] != "brief":
        return _usage()

    rest = argv[1:]
    cadence: str | None = None
    target_root_arg: str | None = None
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "--cadence":
            if i + 1 >= len(rest):
                return _usage()
            cadence = rest[i + 1]
            i += 2
        elif tok == "--target-root":
            if i + 1 >= len(rest):
                return _usage()
            target_root_arg = rest[i + 1]
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

    try:
        target_root = _resolve_target_root(target_root_arg)
    except RuntimeError as exc:
        if target_root_arg is not None:
            print(f"orient-assemble: {exc}", file=sys.stderr)
        else:
            print(
                "orient-assemble: no --target-root given and cwd is not "
                "inside a git working tree. Run from a git repo or pass "
                f"--target-root <path>. Detail: {exc}",
                file=sys.stderr,
            )
        return int(OrientExitCode.USAGE)

    decision_object = brief(cadence, repo_root=target_root)
    print(json.dumps(decision_object, indent=2, sort_keys=True))
    return int(ExitCodeBase.SUCCESS)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
