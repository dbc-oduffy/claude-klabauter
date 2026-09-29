"""
coordinator_core.ops.completion_record_sweep — READ-ONLY hollow-completion-
record sweep.

Purpose: enumerate every record under ``archive/completed/**/*.md`` in one
or more repo roots and report which fail `completion_record_integrity.
hollow_reasons` -- unconditionally, INCLUDING a record a live workstream is
still actively authoring (the writers' `assert_finalize_ready`/
`assert_fields_finalize_ready` only run at each writer's own finalize call
site, and refuse on any single failing check there -- this sweep has no such
finalize-only scoping, so it also surfaces records nobody has tried to
finalize yet). This sweep is a diagnostic, not a gate.

`summarize_by_check` breaks a findings list down per-repo, per-reason --
the read this report needs to tell "genuinely hollow" apart from
"resolution noise" (a `chain:` that predates the `--governing-plan-slug`
convention and was never a real plan filename, mostly showing up as
`plan-unresolvable`/`plan-not-landed`).

Never mutates, moves, or deletes a record -- acting on a finding is the
caller's (EM's) decision.

No per-record subprocess: every check is a `pathlib`/text read, matching
the ``<500ms process time per repo`` requirement -- ``git`` is never
invoked.

Spec backlink: docs/plans/2026-09-28 hollow-completion-record refusal chunk.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, NamedTuple

from coordinator_core.completion_record_integrity import (
    build_plan_index,
    hollow_reasons_and_governing_plan_info,
)


class HollowRecordFinding(NamedTuple):
    """One record failing at least one integrity check."""

    repo_root: str
    record_path: str  # repo-root-relative, forward-slash
    failed_checks: List[str]
    chain_slug: str | None
    plan_path: str | None
    plan_status: str | None


def sweep_repo(repo_root: Path) -> List[HollowRecordFinding]:
    """Every hollow record found under *repo_root*/archive/completed/.

    Returns `[]` when the tree has no ``archive/completed`` directory or
    every record it holds passes all three checks. Ordered by record path
    (deterministic, sorted) for a stable report diff.
    """
    completed_dir = repo_root / "archive" / "completed"
    if not completed_dir.is_dir():
        return []

    # ONE plan-resolution scan for the whole repo (build_plan_index) plus ONE
    # shared status_cache, instead of a per-record filesystem walk AND a
    # second independent governing-plan lookup per record -- see
    # `hollow_reasons_and_governing_plan_info`'s own docstring for why this
    # was previously double work.
    plan_index = build_plan_index(repo_root)
    status_cache: dict = {}

    findings: List[HollowRecordFinding] = []
    for record_path in sorted(completed_dir.rglob("*.md")):
        rel = record_path.relative_to(repo_root).as_posix()
        if "/legacy/" in f"/{rel}":
            continue
        reasons, info = hollow_reasons_and_governing_plan_info(
            record_path, repo_root, plan_index=plan_index, status_cache=status_cache
        )
        if not reasons:
            continue
        findings.append(
            HollowRecordFinding(
                repo_root=str(repo_root),
                record_path=rel,
                failed_checks=reasons,
                chain_slug=info.chain_slug,
                plan_path=info.plan_path,
                plan_status=info.plan_status,
            )
        )
    return findings


def sweep_repos(repo_roots: List[Path]) -> List[HollowRecordFinding]:
    """`sweep_repo` over each of *repo_roots*, concatenated in input order."""
    out: List[HollowRecordFinding] = []
    for root in repo_roots:
        out.extend(sweep_repo(root))
    return out


def summarize_by_check(findings: List[HollowRecordFinding]) -> "dict[str, dict[str, int]]":
    """Per-repo, per-reason failure counts -- `{repo_root: {reason: count}}`.
    A finding with N failed checks increments N counters (once per reason it
    carries), so a repo's per-reason counts do not sum to its total finding
    count when records commonly fail more than one check at once."""
    out: dict[str, dict[str, int]] = {}
    for f in findings:
        bucket = out.setdefault(f.repo_root, {})
        for reason in f.failed_checks:
            bucket[reason] = bucket.get(reason, 0) + 1
    return out
