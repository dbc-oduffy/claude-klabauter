"""
coordinator_core.backlog_grind_assemble.readers_debt — debt-triage reader
(C3d): self-gating `collect(cadence)`, and this cluster's clustering owner.

Purpose: computes the read-half of `coordinator/skills/debt-triage/SKILL.md`
— `state/debt-backlog/`, `state/bug-backlog/` (Step 1b cross-reference), and
`state/improvement-queue/` (Step 1d) — and surfaces it as ONE batched
untrusted-gate judgment point mirroring Step 5's own "This is the terminus's
PM gate" framing (five asks: close-approval, YAGNI/scope, prioritization,
deferral agreement, and surviving-improvement-queue-entry disposition under
`docs/wiki/queue-terminus-doctrine.md`'s four outcome classes). This reader
computes evidence; it never resolves the gate itself (offer, never verdict —
`coordinator_core.contract.decision_object.judgment`'s own contract).

Clustering (this reader's C3d-specific remit): delegates to
`coordinator_core.clustering.candidates.detect_candidates`
(`MIN_CLUSTER_SIZE = 3`, DR-209) over currently-open `improvement-queue`
records, run AHEAD of the Step 5 presentation exactly as
`debt-triage/SKILL.md` § Step 6b specifies ("Clustering, ahead of the Step 5
presentation") — clustering runs over the full open set before the PM's
survives/doesn't-survive call, not after. Per that same section, the
`directory` signal is suppressed (`_SUPPRESSED_CLUSTER_SIGNAL`): on a
single-project-queue corpus it returns one cluster containing everything and
is structurally useless for triage. This is the ONLY local retention of
Step 6b's clustering procedure — per D-4's pinned contract
(`docs/plans/2026-07-26-backlog-grind-computed-frontage.md` § D-4), neither
this reader nor `debt-triage/SKILL.md` (C10) carries the clustering
DEGRADATION LADDER (registered op → `detect-initiative-candidates` CLI → EM
judgment) as prose/logic — that ladder lives exactly once, cited by name, at
`queue-terminus-doctrine.md` § Clustering. This module calls
`coordinator_core.clustering.candidates` directly (the in-process rung of
that ladder now that the algorithm has been relocated into an importable
package, per `candidates.py`'s own "moved verbatim ... from
coordinator/bin/detect-initiative-candidates" docstring) — it does not walk
the ladder's CLI/EM-judgment rungs itself; that degradation-order judgment
is `queue-terminus-doctrine.md`'s to state, not this reader's to restate.

Universal-vs-project-specific classification of `improvement-queue` entries
(SKILL.md Step 1d — "would apply if a different project type used the
coordinator pipeline?") is semantic judgment, not a disk predicate, and is
NOT force-classified here — `_load_improvement_queue` surfaces every open
entry as evidence; the classification itself stays inside the batched
judgment point this reader emits, exactly as `debt-triage/SKILL.md` frames
it as the EM's own call.

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

Contract: coordinator-claude coordinator/skills/debt-triage/SKILL.md (the surface
this reader computes for) and coordinator/docs/wiki/computed-skills.md (the
MECHANICAL/JUDGMENT discriminator this module's split follows).
Spec backlink: docs/plans/2026-07-26-backlog-grind-computed-frontage.md,
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
    - Does NOT retain the clustering DEGRADATION LADDER as prose or logic —
      only the `directory`-signal suppression, which is genuinely
      project-local tuning, not the algorithm (D-4's pinned contract).
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

from coordinator_core.clustering.candidates import detect_candidates
from coordinator_core.contract.decision_object.judgment import (
    build_disposition,
    build_untrusted_gate_judgment_point,
)
from coordinator_core.ops.agent_worktree_sweep import _repo_root as _wt_repo_root
from coordinator_core.ops.queue_family import load_family_records

__all__ = ["ReaderResult", "collect"]

#: the one cadence this reader self-gates on — the seam calls every reader
#: unconditionally for every cadence and trusts each to self-gate (mirrors
#: `orient_assemble.readers_health_reaper`'s day-cadence-only gating, one
#: layer up: there the gate is a cadence VALUE test, here it is a cadence
#: IDENTITY test, since this reader owns exactly one of backlog-grind's five
#: mirror surfaces rather than a severity-tuned subset of a shared one).
_CADENCE = "debt-triage"

#: D-4's one retained piece of local tuning — see the module docstring.
_SUPPRESSED_CLUSTER_SIGNAL = "directory"


@dataclass(frozen=True)
class ReaderResult:
    """One reader family's contribution to the backlog-grind decision-object
    envelope. Shape modeled on (not imported from — a separate package
    cannot reach across without a shared module neither this chunk nor its
    four siblings is scoped to author) `coordinator_core.orient_assemble.
    reader_result.ReaderResult`: same two fields, same defaults, same
    frozen-dataclass shape. `coordinator_core.test_backlog_grind_assemble`
    (C1) pins this as a duck-typed shape — `directives`/`judgment_points`
    attributes — rather than a concrete imported type, precisely so C2/C3
    are free to choose (or not choose) a single shared dataclass across all
    five readers without this file needing to change either way."""

    directives: list[dict[str, Any]] = field(default_factory=list)
    judgment_points: list[dict[str, Any]] = field(default_factory=list)


def _resolve_repo_root() -> Optional[Path]:
    """Discover the calling repo's worktree root via `git rev-parse
    --show-toplevel` from cwd — the same resolution `orient_assemble.
    readers_clean_ops` already uses (`_wt_repo_root()`, imported here under
    its private name to match that established precedent rather than
    inventing a second resolver). Returns `None` when no git repo is
    discoverable (fresh/non-repo cwd) — callers treat that as "nothing to
    read", never a crash."""
    root = _wt_repo_root()
    return Path(root) if root else None


def _open_records(records: list[dict]) -> list[dict]:
    """Filter a `load_family_records` result down to entries whose
    `status` is not `closed` — `closed` entries are already archived out of
    the active triage surface by construction, but a defensive filter here
    costs nothing and protects against a stray un-archived `status: closed`
    entry still sitting in the live directory."""
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
    """Step 1b's mechanical subset: flag debt/bug entry pairs whose
    frontmatter `surface` values match EXACTLY. This is deliberately
    narrower than the skill's own "file path or description similarity"
    test — description similarity is a semantic judgment the EM applies at
    Step 1b itself, not a mechanical predicate this reader can resolve; an
    exact-surface match is the unambiguous subset that IS a disk predicate,
    so only that subset is surfaced here as pre-computed evidence for the
    EM's own similarity pass, never a substitute for it."""
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


def _cluster_candidates(improvement_records: list[dict]) -> list[dict]:
    """Cluster currently-open `improvement-queue` records ahead of the
    Step 5 presentation (see module docstring) and drop the suppressed
    `directory` signal. `detect_candidates` already applies the
    `MIN_CLUSTER_SIZE = 3` floor (DR-209) — nothing here re-derives that
    threshold."""
    open_records = _open_records(improvement_records)
    clusters = detect_candidates(open_records)
    return [c for c in clusters if c.get("signal") != _SUPPRESSED_CLUSTER_SIGNAL]


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
    clusters: list[dict],
) -> dict[str, Any]:
    """Build the ONE batched untrusted-gate judgment point mirroring
    `debt-triage/SKILL.md` Step 5's own five-item PM gate ("This is the
    terminus's PM gate" — DEC-7, `docs/plans/2026-07-23-queue-triage-
    terminates-in-batons.md`). Built with `build_untrusted_gate_judgment_
    point` — never `build_judgment_point` — because Step 5 is explicitly
    the PM's own disposition call (close-approval, YAGNI/scope,
    prioritization, deferral agreement, and surviving-improvement-queue-
    entry classification), not a recommendation this engine is positioned
    to offer. No directive resolves off either disposition — per the
    SUBSTRATE CHECK in the module docstring, the debt-backlog terminus op
    is absent, so there is no execution-ready write for either disposition
    to gate; this judgment point is evidence-and-ask only, exactly like
    `orient_assemble.readers_health_reaper`'s week-cadence marker-freshness
    gate (also un-gated to any directive)."""
    severity_counts = _severity_breakdown(debt_open)
    severity_str = ", ".join(f"{sev}={n}" for sev, n in sorted(severity_counts.items())) or "none"
    cluster_summary = (
        "; ".join(
            f"{c['signal']}={c['value']!r} ({len(c['items'])} items, label={c['suggestedLabel']!r})"
            for c in clusters
        )
        or "no clusters at MIN_CLUSTER_SIZE=3 floor"
    )
    overlap_summary = (
        "; ".join(f"{o['surface']!r}: {o['debt_path']} <-> {o['bug_path']}" for o in bug_overlaps)
        or "no exact-surface overlaps"
    )
    evidence = (
        f"debt-backlog open={len(debt_open)} (by severity: {severity_str}) | "
        f"improvement-queue open={len(improvement_open)} | "
        f"bug/debt exact-surface overlaps: {overlap_summary} | "
        f"improvement-queue clusters (directory signal suppressed, "
        f"MIN_CLUSTER_SIZE=3): {cluster_summary}"
    )
    return build_untrusted_gate_judgment_point(
        id="j-debt-triage-batched-pm-gate",
        question=(
            "Debt-triage Step 5 batched gate: (1) approve closing no-longer-"
            "applicable debt-backlog items, (2) YAGNI/scope decisions on "
            "flagged items, (3) prioritize immediate-action items, "
            "(4) agree deferral reasoning, (5) disposition surviving "
            "project-specific improvement-queue entries under the four "
            "queue-terminus outcome classes (see "
            "docs/wiki/queue-terminus-doctrine.md), using the clustering "
            "evidence above for themed-baton candidates."
        ),
        dispositions=[
            build_disposition("reviewed_and_disposed"),
            build_disposition("defer_to_next_triage"),
        ],
        evidence=evidence,
        reason="pm-owned-terminus-disposition",
    )


def collect(cadence: str, *, run_id: Optional[str] = None) -> ReaderResult:
    """Compute this reader family's directives/judgment_points for
    `cadence`. Self-gates to `debt-triage` only — every other cadence
    returns an empty `ReaderResult()`, per the seam's contract of calling
    every reader unconditionally and trusting each to self-gate.

    `run_id` names which run of the ASKING surface is asking. This reader
    has no per-run record family to resolve it against, so it accepts the
    parameter and ignores it: the seam threads it to all five readers
    uniformly for every cadence (`__init__.py`'s negative-spec against a
    per-surface branch), and self-gating on it is each reader's own job,
    exactly as self-gating on `cadence` is.
    """
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
    clusters = _cluster_candidates(improvement_open)

    jp = _build_batched_pm_gate(
        debt_open=debt_open,
        bug_overlaps=bug_overlaps,
        improvement_open=improvement_open,
        clusters=clusters,
    )
    return ReaderResult(directives=[], judgment_points=[jp])
