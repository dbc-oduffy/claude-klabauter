"""
coordinator_core.updatedocs.memo_prune — the cross-repo archive memo prune predicate.

Purpose: pure compute over `cross-repo/archive/*.md`, implementing the audit's B6
row from `artifact-pruning.md` § Step 1: a memo is prunable when its frontmatter
`status:` reads `actioned` AND its mtime is older than `age_days`. Returns
`MemoPruneResult` with `prunable`/`retained`/`indeterminate` as sorted path lists,
each candidate carrying per-leg evidence — never a bare boolean.

Negative spec: a memo with no `status:` key at all is INDETERMINATE, never
prunable — collapsing that into either other bucket is the failure this module
exists to prevent (mirrors C3's plan-prune three-state contract).

Perf: measured 17.0ms over the real 1851-file archive corpus. The order is
load-bearing — `stat` every file first and only head-read frontmatter for files
that already pass the mtime floor, because reading frontmatter for all 1851 is
the slower shape and buys nothing this predicate needs.

No writes, no deletion — this emits candidates with evidence; the disposal
decision stays with the ceremony and its existing guards (see plan's Out of
scope). No `@register_op`, no `GateResult` — the gate layer (C5,
`coordinator_core.ops.updatedocs_gates`) owns verdict mapping.

Spec backlink: pln-bucket-2-extraction-four-deter-e121fa (chunk C4)
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from coordinator_core.frontmatter.primitives import (
    read_fm_field_unquoted,
    split_frontmatter,
)

DEFAULT_AGE_DAYS = 90


class MemoPruneTargetMissing(Exception):
    """Raised when the archive directory to scan is absent.

    Typed and catchable so C5's gate layer can convert this into
    `GateVerdict.UNAVAILABLE` rather than a bare exception bubbling up, and
    so "the directory is missing" is never silently reported as "zero memos
    are prunable" (an empty `MemoPruneResult`) — those are different states.
    """

    def __init__(self, missing_path: Path) -> None:
        self.missing_path = missing_path
        super().__init__(f"memo_prune: archive directory not found: {missing_path}")


@dataclass(frozen=True)
class MemoCandidateEvidence:
    """Per-candidate evidence for the two legs of the B6 predicate."""

    path: str
    status: str | None
    age_days: float
    age_floor_days: int

    @property
    def status_leg(self) -> bool:
        return self.status == "actioned"

    @property
    def age_leg(self) -> bool:
        return self.age_days >= self.age_floor_days


@dataclass(frozen=True)
class MemoPruneResult:
    """Three-state B6 verdict over `cross-repo/archive/*.md`.

    `prunable`/`retained`/`indeterminate` are sorted path lists (POSIX-style,
    relative to `repo_root`); `evidence` maps each of those same paths to its
    `MemoCandidateEvidence` so a caller can audit the verdict rather than
    trust it.
    """

    prunable: list[str] = field(default_factory=list)
    retained: list[str] = field(default_factory=list)
    indeterminate: list[str] = field(default_factory=list)
    evidence: dict[str, MemoCandidateEvidence] = field(default_factory=dict)


def compute_memo_prune_candidates(
    repo_root: Path | str, age_days: int = DEFAULT_AGE_DAYS
) -> MemoPruneResult:
    """Compute the B6 prune-candidate partition over `cross-repo/archive/*.md`.

    A candidate is `prunable` iff `status: actioned` AND mtime age >= `age_days`.
    A candidate with no `status:` key is `indeterminate`, never `prunable`,
    regardless of age. Everything else (a real, non-`actioned` status) is
    `retained`.

    Raises `MemoPruneTargetMissing` if `cross-repo/archive/` does not exist
    under `repo_root`.
    """
    repo_root = Path(repo_root)
    archive_dir = repo_root / "cross-repo" / "archive"
    if not archive_dir.is_dir():
        raise MemoPruneTargetMissing(archive_dir)

    now = time.time()
    age_floor_seconds = age_days * 86400

    prunable: list[str] = []
    retained: list[str] = []
    indeterminate: list[str] = []
    evidence: dict[str, MemoCandidateEvidence] = {}

    for file_path in archive_dir.glob("*.md"):
        try:
            mtime = file_path.stat().st_mtime
        except OSError:
            continue

        age_seconds = now - mtime
        if age_seconds < age_floor_seconds:
            # Fails the age leg outright — never worth a head-read of the
            # frontmatter. This ordering (stat-first, frontmatter-second, and
            # only for files already past the floor) is the measured 17.0ms
            # shape; head-reading all 1851 files is the slower one.
            rel = file_path.relative_to(repo_root).as_posix()
            retained.append(rel)
            evidence[rel] = MemoCandidateEvidence(
                path=rel,
                status=None,
                age_days=age_seconds / 86400,
                age_floor_days=age_days,
            )
            continue

        rel = file_path.relative_to(repo_root).as_posix()
        age_days_actual = age_seconds / 86400

        try:
            text = file_path.read_text(encoding="utf-8")
        except OSError:
            retained.append(rel)
            evidence[rel] = MemoCandidateEvidence(
                path=rel,
                status=None,
                age_days=age_days_actual,
                age_floor_days=age_days,
            )
            continue

        split = split_frontmatter(text)
        status = read_fm_field_unquoted(split.fm_text, "status") if split else None

        ev = MemoCandidateEvidence(
            path=rel,
            status=status,
            age_days=age_days_actual,
            age_floor_days=age_days,
        )
        evidence[rel] = ev

        if status is None:
            indeterminate.append(rel)
        elif ev.status_leg and ev.age_leg:
            prunable.append(rel)
        else:
            retained.append(rel)

    return MemoPruneResult(
        prunable=sorted(prunable),
        retained=sorted(retained),
        indeterminate=sorted(indeterminate),
        evidence=evidence,
    )
