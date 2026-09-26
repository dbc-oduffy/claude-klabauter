
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from coordinator_core.backlog_grind_assemble import directives
from coordinator_core.contract.decision_object.judgment import (
    build_disposition,
    build_judgment_point,
)
from coordinator_core.ops.check_weekly_staleness import _resolve_state_root
from coordinator_core.ops.queue_family import load_family_records

#: `coordinator_core.test_backlog_grind_assemble._CADENCES`
_CADENCE = "bug-sweep"

#: The `queue_family.FAMILY_TO_RECORD_TYPE` key for bug-sweep's own queue
_BUG_BACKLOG_FAMILY = "bug-backlog"

_OPEN_STATUS = "open"


@dataclass(frozen=True)
class ReaderResult:

    directives: list[dict[str, Any]] = field(default_factory=list)
    judgment_points: list[dict[str, Any]] = field(default_factory=list)


def _open_bug_backlog_records(repo_root: Path) -> list[dict]:
    records = load_family_records(_BUG_BACKLOG_FAMILY, repo_root)
    return [
        record
        for record in records
        if (record.get("frontmatter") or {}).get("status") == _OPEN_STATUS
    ]


def _pre_dispatch_verification_due(open_count: int) -> ReaderResult:
    return ReaderResult(
        judgment_points=[
            build_judgment_point(
                None,
                id="j-bug-sweep-pre-dispatch-verification-due",
                question=(
                    f"{open_count} open state/bug-backlog/ entr"
                    f"{'y' if open_count == 1 else 'ies'} on disk — run the "
                    "Pre-Dispatch verify-against-HEAD pass before Phase 1 "
                    "dispatch?"
                ),
                dispositions=[
                    build_disposition("run_pre_dispatch_verification"),
                    build_disposition("skip_this_run"),
                ],
                evidence=(
                    f"state/bug-backlog/ open-entry count={open_count} | "
                    "reason: whether a cited file:line still reflects the "
                    "backlog entry's claim is a semantic read against "
                    "current HEAD, never a disk/git predicate the engine "
                    "can resolve on its own"
                ),
                reason="insufficient-evidence",
            )
        ]
    )


def _tier_u_grant_flow(open_count: int) -> ReaderResult:
    """Bug-sweep's single Tier-U-gated path (Phase 1 Track B: the full
    test-suite invocation `/bug-sweep` asks the PM to authorize before
    running) plus the two confirm-green `check` rechecks
    `bug-sweep/SKILL.md:91,163` mandate against that same session-scoped
    grant, with no second ask. Built via `directives.build_tier_u_grant_flow`
    / `build_tier_u_grant_check` — the shared C2 builders — never re-derived
    locally, per this chunk's own routing instruction, mirroring
    `readers_blitz.py::_tier_u_grant_flow`.

    Both `check` directives carry a `depends_on` edge to the grant's own
    WRITE DIRECTIVE id (never its judgment-point id), per
    `build_tier_u_grant_check`'s own contract — that is what keeps either
    recheck from dispatching before the token exists. `SKILL.md:91`'s
    "immediately before firing Track B" recheck and `:163`'s post-fix
    recheck are two independently-dispatchable directives, not one directive
    reported twice: `check_tier_u_grant` gates on liveness, and the token
    can be revoked or its session can die between the two points this
    mandates re-checking.
    """
    jp, write_directive = directives.build_tier_u_grant_flow(
        jp_id="j-bug-sweep-tier-u-grant",
        write_directive_id="d-bug-sweep-tier-u-grant-write",
        subject="bug-sweep Track B (full test suite over the fix-now diff)",
        evidence=(
            f"state/bug-backlog/ open-entry count={open_count} at collect "
            "time | reason: Track B's suite invocation is Tier-U and "
            "/bug-sweep holds no implicit authorization grant "
            "(bug-sweep/SKILL.md § Track B)"
        ),
        reason="insufficient-evidence",
        note="bug-sweep Track B test-suite authorization",
    )
    pre_track_b_check = directives.build_tier_u_grant_check(
        id="d-bug-sweep-tier-u-grant-check-pre-track-b",
        depends_on=write_directive["id"],
    )
    post_fix_check = directives.build_tier_u_grant_check(
        id="d-bug-sweep-tier-u-grant-check-post-fix",
        depends_on=write_directive["id"],
    )
    return ReaderResult(
        directives=[write_directive, pre_track_b_check, post_fix_check],
        judgment_points=[jp],
    )


def collect(cadence: str, *, run_id: Optional[str] = None) -> ReaderResult:
    if cadence != _CADENCE:
        return ReaderResult()

    state_root_str = _resolve_state_root()
    if not state_root_str:
        return ReaderResult()

    repo_root = Path(state_root_str).parent
    open_count = len(_open_bug_backlog_records(repo_root))

    results = [_tier_u_grant_flow(open_count)]
    if open_count > 0:
        results.append(_pre_dispatch_verification_due(open_count))

    all_directives: list[dict[str, Any]] = []
    all_judgment_points: list[dict[str, Any]] = []
    for result in results:
        all_directives.extend(result.directives)
        all_judgment_points.extend(result.judgment_points)
    return ReaderResult(directives=all_directives, judgment_points=all_judgment_points)
