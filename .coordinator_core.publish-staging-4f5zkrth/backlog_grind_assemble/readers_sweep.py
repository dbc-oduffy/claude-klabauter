"""
coordinator_core.backlog_grind_assemble.readers_sweep — C3c reader: the
`bug-sweep` cadence's own contribution to the backlog-grind decision object.

Purpose, per `docs/plans/2026-07-26-backlog-grind-computed-frontage.md`
chunk C3c: expose `collect(cadence) -> ReaderResult` over `/bug-sweep`'s
own computed surface — the `state/bug-backlog/` queue read and the single
Tier-U-gated path (Phase 1 Track B's full-test-suite authorization ask,
`coordinator/skills/bug-sweep/SKILL.md` § Track B). This reader is one of
five siblings (`readers_blitz`, `readers_mise`, `readers_sweep`,
`readers_debt`, `readers_dogfood`) authored in parallel against strictly
disjoint files; it never imports or reshapes another reader's dict, and it
is blind to their in-flight state by construction.

Self-gating (D-2, mirrors `orient_assemble.readers_health_reaper`'s
day-cadence-only gating): `__init__.py` (C3, the thin concatenating seam)
calls every reader's `collect(cadence)` unconditionally for every cadence
and trusts each reader to return an empty `ReaderResult` when the cadence
isn't its own. This module owns that gate for the `"bug-sweep"` cadence
only — it never inspects or branches on any other cadence's name beyond
the equality check below.

Queue read discipline (D-6, AC6, scoped to this file): the only backlog
read in this module goes through `coordinator_core.ops.queue_family.
load_family_records` — no directory-globbing and no direct YAML-frontmatter
parsing anywhere in this file, per that module's own negative-spec
forbidding a caller from reimplementing its read seam.

Tier-U routing (D-3/D-6 sibling contract, this chunk's own instruction):
the PM-authorization ask for bug-sweep's Track B full-suite invocation is
built via `directives.build_tier_u_grant_flow` — this module never
re-derives the grant token shape, the `tier-u-grant-cli` argv shape, or a
local `build_untrusted_gate_judgment_point` call for it.

Spec backlink: DoE-claude:pln-b7-backlog-grind-cluster-compu-bebb7c,
chunk C3c.

Negative-spec:
    - Does NOT glob `state/bug-backlog/` or parse its YAML directly — every
      queue read routes through `queue_family.load_family_records` (AC6).
    - Does NOT reshape, read, or import any sibling reader's module
      (`readers_blitz`, `readers_mise`, `readers_debt`, `readers_dogfood`)
      — each is authored in parallel against a disjoint file.
    - Does NOT wire itself into `brief()` — `__init__.py` (C3) owns that
      concatenation; this module exposes `collect(cadence)` only.
    - Does NOT re-derive the Tier-U grant token/CLI-argv shape locally —
      `directives.build_tier_u_grant_flow` is the single owner of that
      shape (this chunk's own instruction, mirroring D-6's "thin caller,
      never a new parser" scope onto the grant flow as well).
    - Does NOT mutate anything or shell out — `collect()` is read-only,
      same discipline as every other backlog-grind reader (AC2's purity
      requirement on `brief()` is only provable if every reader it calls
      is itself pure).
"""

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

#: This reader's own cadence name — one of the five surfaces enumerated by
#: `coordinator_core.test_backlog_grind_assemble._CADENCES`
#: (`"bug-blitz"`, `"mise-en-place"`, `"bug-sweep"`, `"debt-triage"`,
#: `"dogfood"`). Self-gating below compares `cadence` against this literal;
#: every other cadence gets an empty `ReaderResult` from this module.
_CADENCE = "bug-sweep"

#: The `queue_family.FAMILY_TO_RECORD_TYPE` key for bug-sweep's own queue
#: surface (`state/bug-backlog/`) — the only family this reader touches.
_BUG_BACKLOG_FAMILY = "bug-backlog"

_OPEN_STATUS = "open"


@dataclass(frozen=True)
class ReaderResult:
    """This reader family's contribution to the decision-object envelope.

    Modeled on `coordinator_core.orient_assemble.reader_result.ReaderResult`
    (same two fields, same frozen-dataclass shape) per this chunk's own
    instruction to model the shape on that module rather than invent a new
    one. Not imported from there directly — `orient_assemble` and
    `backlog_grind_assemble` are sibling packages with no dependency edge
    between them, and this chunk's write scope is this file alone; C3 (the
    seam) concatenates by duck-typed attribute access
    (`.directives`/`.judgment_points`), so the exact type identity is not
    load-bearing across the package boundary.
    """

    directives: list[dict[str, Any]] = field(default_factory=list)
    judgment_points: list[dict[str, Any]] = field(default_factory=list)


def _open_bug_backlog_records(repo_root: Path) -> list[dict]:
    """Every `status: open` record in `state/bug-backlog/`, read exclusively
    through `queue_family.load_family_records` (AC6)."""
    records = load_family_records(_BUG_BACKLOG_FAMILY, repo_root)
    return [
        record
        for record in records
        if (record.get("frontmatter") or {}).get("status") == _OPEN_STATUS
    ]


def _pre_dispatch_verification_due(open_count: int) -> ReaderResult:
    """When `state/bug-backlog/` carries open entries, surface the
    Pre-Dispatch backlog-verification step (`bug-sweep/SKILL.md` §
    Pre-Dispatch) as a judgment point rather than a directive: whether and
    how to re-verify each cited `file:line` against current HEAD is a
    semantic read the engine cannot resolve mechanically (the skill's own
    measured rate — 11/20 items already fixed in one run — is exactly the
    kind of judgment the discharge test leaves to the operator, not a
    disk/git predicate). No `recommendation` is attached — this engine has
    no opinion on whether re-verification is warranted this run, only that
    open entries exist for it to apply to."""
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
    running). Built via `directives.build_tier_u_grant_flow` — the shared
    C2 builder — never re-derived locally, per this chunk's own routing
    instruction."""
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
    return ReaderResult(directives=[write_directive], judgment_points=[jp])


def collect(cadence: str, *, run_id: Optional[str] = None) -> ReaderResult:
    """Compute the `bug-sweep` cadence's directives/judgment_points.

    Self-gates on `cadence` internally — `__init__.py` (C3) calls this
    unconditionally for every one of the five cadences and trusts this
    function to return an empty `ReaderResult` for the other four, per
    `orient_assemble.readers_health_reaper`'s precedent.

    `run_id` names which run of the ASKING surface is asking. This reader
    has no per-run record family to resolve it against, so it accepts the
    parameter and ignores it: the seam threads it to all five readers
    uniformly for every cadence (`__init__.py`'s negative-spec against a
    per-surface branch), and self-gating on it is each reader's own job,
    exactly as self-gating on `cadence` is.

    Resolves the calling repo's root via the same
    `check_weekly_staleness._resolve_state_root()` seam
    `readers_health_reaper._read_marker_freshness` already uses — this is
    the CALLING project's own `state/bug-backlog/` (per-repo, or makima's
    central state only when the calling git root IS the meta-repo), never
    `project-makima`'s own repo root. When the state root can't be
    resolved (no git root, or makima unresolvable), returns an empty
    `ReaderResult` rather than raising — an unresolvable state root is not
    this reader's failure to report, and every other cadence already
    tolerates the same absence.
    """
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
