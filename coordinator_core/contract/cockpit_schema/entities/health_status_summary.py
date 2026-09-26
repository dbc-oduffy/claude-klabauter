"""
HealthStatusSummary — pydantic port of DoE
`coordinator/cockpit-contract/src/entities/health-status-summary.ts` (Zod
source). Summary view over state/health/*.md lifecycle and health posture.

Spec backlink: schemas/health-status.yaml + docs/plans/2026-06-27-emit-new-record-types-producer-wiring.md
§ B1 + DECISIONS.md D9 + D5.
Spec backlink: DoE-claude:pln-bash-to-naked-python-engine-mi-c09292 § T4e

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
    path: str
    title: str
    # ISO calendar date (YYYY-MM-DD) from health-status frontmatter.
    created: IsoDate
    status: HealthStatusLifecycle
    health: HealthPosture
    provenance: ProvenanceEnvelope


    owner: str | None
    summary: str | None

    content_hash: ContentHash | None = None
