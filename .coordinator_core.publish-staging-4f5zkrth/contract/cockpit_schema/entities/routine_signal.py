"""
RoutineSignal — staleness as a typed derived-signal, NOT a scalar (Camelia P1-D3).
Pydantic port of DoE `coordinator/cockpit-contract/src/entities/routine-signal.ts`
(Zod source).

Staleness is at least six distinct derived quantities, each with different
inputs, thresholds, and units; they must not collapse to one boolean or string.
Light bitemporal: `observed_at` (when the underlying fact was read) vs
`computed_as_of` (the wall-clock the threshold was computed against) so the
dashboard can render "stale (as of 14m ago)" honestly and falsifiably. This is
NOT full effective-dated revision — only the observed/computed pair on derived
signals.

Spec backlink: DoE-claude:pln-bash-to-naked-python-engine-mi-c09292 § T4e
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from coordinator_core.contract.cockpit_schema.common import IsoDateTime
from coordinator_core.contract.cockpit_schema.provenance import ContentHash, ProvenanceEnvelope

RoutineSignalKind = Literal[
    "weekly",  # week-changelog cadence; threshold: >=5 days AND >=15 commits
    "bug-sweep",  # bug-backlog cadence; threshold: >50 commits AND >7 days, OR >14 days AND >20 commits
    "docs",  # update-docs cadence; threshold: any commits since last update-docs run
    "arch-audit",  # architecture audit cadence; threshold: 10 days
    "dormant-repo",  # repo inactivity; threshold: default-branch tip >30 days old
    "distill-backlog",  # undigested archive entries; threshold: >N entries pending (N TBD at tc-3)
]
"""The six named staleness kinds (cockpit-emission corpus § 10). Each has distinct
inputs/thresholds/units — enumerated, never an open string."""

ComputedState = Literal["fresh", "mild", "stale", "unknown"]


class RoutineSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: RoutineSignalKind
    repo: str = Field(
        description=(
            "Owner-qualified repo identity: '<owner>/<repo>'. Owner carries the "
            "producing repo's own casing (producer-authoritative — there is no "
            "independent casing authority); no '.git' suffix. Consumers must not "
            "assume pre-normalization: normalizing to lower(owner)/lower(repo) for "
            "join, dedup, or storage keying is conformant. See DR-022. This "
            "owner-qualified string is the canonical cross-entity join anchor."
        )
    )
    coordinator_root_path: str
    inputs: dict[str, str | float]
    """Kind-specific: {commits_since, days_since, last_sweep_sha, …}. Stored as JSON column in tc-5."""
    threshold: str
    """Human-readable threshold definition. Stored as text in tc-5."""
    computed_state: ComputedState
    overdue: bool
    observed_at: IsoDateTime
    """ISO-8601 UTC — when the underlying fact was read."""
    computed_as_of: IsoDateTime
    """ISO-8601 UTC — wall-clock at threshold computation; enables "stale as of Xm ago"."""
    provenance: ProvenanceEnvelope
    content_hash: ContentHash | None = None
    """
    R5 content-hash change-signal (optional; sibling of provenance). Omitted by
    makima for records with no resolvable single source file (rolled-up aggregates,
    empty-path computed records). Version-neutral optional — absent on all existing
    records. Spec: producer-contract § 3.3.
    """
