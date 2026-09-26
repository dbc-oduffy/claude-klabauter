"""
coordinator_core.backlog_grind_assemble.readers_debt — debt-triage reader
(C3d): self-gating `collect(cadence)`.

Purpose: computes the read-half of `coordinator/skills/debt-triage/SKILL.md`
— `state/debt-backlog/`, `state/bug-backlog/` (Step 1b cross-reference), and
`state/improvement-queue/` (Step 1d) — and surfaces it as ONE batched
untrusted-gate judgment point mirroring Step 5's own "This is the terminus's
PM gate" framing (close-approval, YAGNI/scope, prioritization, and deferral
agreement). This reader computes evidence; it never resolves the gate itself
(offer, never verdict — `coordinator_core.contract.decision_object.judgment`'s
own contract).

No clustering leg (item 19, IBMDT-C22): this reader carries no improvement-
queue clustering and no judgment-point item disposing of surviving
improvement-queue entries under the four queue-terminus outcome classes.
Item (5) of the batched PM gate and `_cluster_candidates` were dropped —
`_load_improvement_queue`'s only surfaced signal is now a pre-fire open-row
count. Clustering over `improvement-queue` (`MIN_CLUSTER_SIZE = 3`, DR-209,
the `directory` signal suppressed) and its DEGRADATION LADDER remain
`queue-terminus-doctrine.md` § Clustering's to state; this reader does not
retain, restate, or invoke them.

Universal-vs-project-specific classification of `improvement-queue` entries
(SKILL.md Step 1d — "would apply if a different project type used the
coordinator pipeline?") is semantic judgment, not a disk predicate, and is
NOT force-classified here — `_load_improvement_queue` surfaces its open-row
count as evidence; the classification itself is out of this reader's
batched judgment point (see item 19 note above).

SUBSTRATE CHECK, re-confirmed at authoring time (2026-07-27, this chunk):
a recursive grep for `debt-backlog` under `coordinator_core/ops/` on this
branch returns hits only in `queue_family.py`, `queue_age_ping.py`,
`queue_cluster.py`, `queue_append.py`, `records_query.py`, and their tests —
NOTHING under `ops/fleet/` (only `archive_queue_entry.py`, the
improvement-queue terminus, and `prune_bugs.py`, the bug-backlog terminus,
live there). The debt-backlog terminus op remains CONFIRMED ABSENT — this
reader therefore emits NO debt-backlog archive/close directive; Step 6's
closure commit and Step 6b's class-1/2/4 terminus writes stay judgment-point
evidence only, never execution-ready `directives[]` entries, until that op
is built (out of this conversion's remit per the plan's own "Out of scope").
If a future re-run of this grep finds the op has since appeared, that is
drift on the next reader-authoring pass to wire up, not something this
module silently absorbs.

Contract: DoE-claude coordinator/skills/debt-triage/SKILL.md (the surface
this reader computes for) and coordinator/docs/wiki/computed-skills.md (the
MECHANICAL/JUDGMENT discriminator this module's split follows).
Spec backlink: DoE-claude:pln-b7-backlog-grind-cluster-compu-bebb7c,
chunk C3d.

Negative-spec:
    - Does NOT directory-glob or raw-YAML-parse a `state/<family>/` queue
      directory itself — every queue read routes through
      `coordinator_core.ops.queue_family.load_family_records` (D-6, AC6,
      scoped to this file only; the sibling readers carry their own
      independent AC6 obligation, not this module's to enforce).
    - Does NOT reshape another reader's dict, and is never called by
      anything other than the seam (C3, `__init__.py`) via its own
      `collect(cadence)` export.
    - Does NOT restate the four-outcome terminus vocabulary (solo baton /
      themed baton / immediate dispatch / close-or-park) — that vocabulary
      is `queue-terminus-doctrine.md`'s; this module's evidence strings name
      the classes without re-deriving their definitions.
    - Does NOT cluster `improvement-queue` records or retain the clustering
      DEGRADATION LADDER as prose or logic (item 19, IBMDT-C22) — that stays
      `queue-terminus-doctrine.md` § Clustering's alone.
    - Does NOT emit a debt-backlog archive/close directive — see the
      SUBSTRATE CHECK above.
    - Does NOT invoke `subprocess`/`git log` to reproduce SKILL.md's
      Pre-Dispatch staleness pre-check — that check inherently needs a
      per-item `git log --since=<finding-date>` walk, which does not belong
      on `brief()`'s hot path; it stays an EM-side pre-check exactly as
      `debt-triage/SKILL.md` already frames it, not something this reader
      backfills.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from coordinator_core.contract.decision_object.judgment import (
    build_disposition,
    build_untrusted_gate_judgment_point,
)
from coordinator_core.ops.agent_worktree_sweep import _repo_root as _wt_repo_root
from coordinator_core.ops.queue_family import load_family_records

__all__ = ["ReaderResult", "collect"]

#: IDENTITY test, since this reader owns exactly one of backlog-grind's five
_CADENCE = "debt-triage"


@dataclass(frozen=True)
class ReaderResult:

    directives: list[dict[str, Any]] = field(default_factory=list)
    judgment_points: list[dict[str, Any]] = field(default_factory=list)


def _resolve_repo_root() -> Optional[Path]:
    root = _wt_repo_root()
    return Path(root) if root else None


def _open_records(records: list[dict]) -> list[dict]:
    return [r for r in records if (r.get("frontmatter") or {}).get("status") != "closed"]


def _severity_breakdown(records: list[dict]) -> dict[str, int]:
    """Count open records by `severity` (or `unspecified` when the field is
    absent — it is optional on both `debt-backlog` and `improvement-queue`
    per `queue_family.FAMILY_FIELDS`)."""
    counts: dict[str, int] = {}
    for rec in records:
        severity = (rec.get("frontmatter") or {}).get("severity") or "unspecified"
        counts[severity] = counts.get(severity, 0) + 1
    return counts


def _cross_reference_overlap(debt_records: list[dict], bug_records: list[dict]) -> list[dict]:
    overlaps: list[dict] = []
    for debt in debt_records:
        debt_surface = (debt.get("frontmatter") or {}).get("surface")
        if not debt_surface:
            continue
        for bug in bug_records:
            bug_surface = (bug.get("frontmatter") or {}).get("surface")
            if bug_surface and bug_surface == debt_surface:
                overlaps.append(
                    {
                        "surface": debt_surface,
                        "debt_path": debt.get("path", ""),
                        "bug_path": bug.get("path", ""),
                    }
                )
    return overlaps


def _load_debt_backlog(repo_root: Path) -> list[dict]:
    return load_family_records("debt-backlog", repo_root)


def _load_bug_backlog(repo_root: Path) -> list[dict]:
    return load_family_records("bug-backlog", repo_root)


def _load_improvement_queue(repo_root: Path) -> list[dict]:
    return load_family_records("improvement-queue", repo_root)


def _build_batched_pm_gate(
    *,
    debt_open: list[dict],
    bug_overlaps: list[dict],
    improvement_open: list[dict],
) -> dict[str, Any]:
    """Build the ONE batched untrusted-gate judgment point mirroring
    `debt-triage/SKILL.md` Step 5's own PM gate ("This is the terminus's PM
    gate" — DEC-7, `docs/plans/2026-07-23-queue-triage-terminates-in-
    batons.md`). Built with `build_untrusted_gate_judgment_point` — never
    `build_judgment_point` — because Step 5 is explicitly the PM's own
    disposition call (close-approval, YAGNI/scope, prioritization, and
    deferral agreement), not a recommendation this engine is positioned to
    offer. No directive resolves off either disposition — per the
    SUBSTRATE CHECK in the module docstring, the debt-backlog terminus op
    is absent, so there is no execution-ready write for either disposition
    to gate; this judgment point is evidence-and-ask only, exactly like
    `orient_assemble.readers_health_reaper`'s week-cadence marker-freshness
    gate (also un-gated to any directive).

    Item (5) (disposition of surviving project-specific improvement-queue
    entries under the four queue-terminus outcome classes) and the
    clustering evidence that fed it are dropped (item 19, IBMDT-C22):
    `improvement_open` contributes only its pre-fire open-row count."""
    severity_counts = _severity_breakdown(debt_open)
    severity_str = ", ".join(f"{sev}={n}" for sev, n in sorted(severity_counts.items())) or "none"
    overlap_summary = (
        "; ".join(f"{o['surface']!r}: {o['debt_path']} <-> {o['bug_path']}" for o in bug_overlaps)
        or "no exact-surface overlaps"
    )
    evidence = (
        f"debt-backlog open={len(debt_open)} (by severity: {severity_str}) | "
        f"improvement-queue open={len(improvement_open)} | "
        f"bug/debt exact-surface overlaps: {overlap_summary}"
    )
    return build_untrusted_gate_judgment_point(
        id="j-debt-triage-batched-pm-gate",
        question=(
            "Debt-triage Step 5 batched gate: (1) approve closing no-longer-"
            "applicable debt-backlog items, (2) YAGNI/scope decisions on "
            "flagged items, (3) prioritize immediate-action items, "
            "(4) agree deferral reasoning."
        ),
        dispositions=[
            build_disposition("reviewed_and_disposed"),
            build_disposition("defer_to_next_triage"),
        ],
        evidence=evidence,
        reason="pm-owned-terminus-disposition",
    )


def collect(cadence: str, *, run_id: Optional[str] = None) -> ReaderResult:
    if cadence != _CADENCE:
        return ReaderResult()

    repo_root = _resolve_repo_root()
    if repo_root is None:
        return ReaderResult()

    debt_records = _load_debt_backlog(repo_root)
    bug_records = _load_bug_backlog(repo_root)
    improvement_records = _load_improvement_queue(repo_root)

    debt_open = _open_records(debt_records)
    bug_open = _open_records(bug_records)
    improvement_open = _open_records(improvement_records)

    bug_overlaps = _cross_reference_overlap(debt_open, bug_open)

    jp = _build_batched_pm_gate(
        debt_open=debt_open,
        bug_overlaps=bug_overlaps,
        improvement_open=improvement_open,
    )
    return ReaderResult(directives=[], judgment_points=[jp])
