from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from coordinator_core.contract.cockpit_schema.common import IsoDateTime
from coordinator_core.contract.cockpit_schema.provenance import ContentHash, ProvenanceEnvelope

RoutineSignalKind = Literal[
    "weekly",
    "bug-sweep",
    "docs",
    "arch-audit",
    "dormant-repo",
    "distill-backlog",
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
    claude-klabauter for records with no resolvable single source file (rolled-up aggregates,
    empty-path computed records). Version-neutral optional — absent on all existing
    records. Spec: producer-contract § 3.3.
    """
