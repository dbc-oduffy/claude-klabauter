"""
HealthStatusSummary — pydantic port of coordinator-claude
`coordinator/cockpit-contract/src/entities/health-status-summary.ts` (Zod
source). Summary view over state/health/*.md lifecycle and health posture.

Spec backlink: schemas/health-status.yaml + docs/plans/2026-06-27-emit-new-record-types-producer-wiring.md
§ B1 + DECISIONS.md D9 + D5.
Spec backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § T4e

Composite primary key: (repo, coordinator_root_path, path). `title` is
emitted verbatim from health-status frontmatter.

`repo` and `coordinator_root_path` are connector-injected (D4).
Nullable fields follow D9 (present-as-null, not optional).

AXIS DISTINCTION — `status` and `health` are orthogonal:
  `status` = lifecycle axis (active | archived) — is this record still live?
  `health` = posture axis (HEALTHY | WATCH | ACTION | CRITICAL) — what does
  it report?
Do NOT conflate. Liveness queries key on `status` only.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from coordinator_core.contract.cockpit_schema.common import IsoDate
from coordinator_core.contract.cockpit_schema.provenance import ContentHash, ProvenanceEnvelope

HealthStatusLifecycle = Literal["active", "archived"]
"""Lifecycle axis — is this health-status record still active?"""

HealthPosture = Literal["HEALTHY", "WATCH", "ACTION", "CRITICAL"]
"""
Posture axis — the health signal reported by this record.
Ordered from most-healthy to most-critical: HEALTHY -> WATCH -> ACTION -> CRITICAL.
"""


class HealthStatusSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Connector-injected registry shortname.
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
    # Connector-injected — matches other summary entities.
    coordinator_root_path: str
    # Relative path within the repo (e.g. "state/health/2026-06-27-weekly.md").
    # Composite key with repo + coordinator_root_path.
    path: str
    # First H1 or frontmatter title of the health-status document.
    title: str
    # ISO calendar date (YYYY-MM-DD) from health-status frontmatter.
    created: IsoDate
    # Lifecycle state — whether this record is still active (NOT the posture).
    status: HealthStatusLifecycle
    # Health posture signal — the reported condition, orthogonal to `status`.
    health: HealthPosture
    provenance: ProvenanceEnvelope

    # Nullable fields (D9 present-as-null).

    # Owning team or person; null if not declared in frontmatter.
    owner: str | None
    # Free-text summary of the health report; null if absent.
    summary: str | None

    # R5 content-hash change-signal (optional; sibling of provenance). Omitted
    # by claude-klabauter for records with no resolvable single source file (rolled-up
    # aggregates, empty-path computed records). Version-neutral optional —
    # absent on all existing records. Spec: producer-contract § 3.3.
    content_hash: ContentHash | None = None
