"""
DecisionGuideSummary — pydantic port of DoE
`coordinator/cockpit-contract/src/entities/decision-guide-summary.ts` (Zod source).
Summary view over a consolidated/distilled DR-corpus container document.

A decision-guide is a sibling of per-file `decision` records: where `decision`
represents an individual DR entry, `decision-guide` is the container document
(the DECISIONS.md or equivalent) that houses a corpus of decisions. This
entity surfaces the document-level metadata for that container.

Spec backlinks:
  - schemas/decision-guide.yaml
  - docs/plans/2026-06-27-cockpit-emission-decision-guide-4th-type.md § C1
  - docs/wiki/canonical-artifact-shapes.md § decision-guide
  - docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § T4e

SINGLE AXIS — this entity has only a lifecycle axis (active | archived). There
is NO posture axis and NO BLOCKED bucket. Do NOT add a HealthPosture-style
enum to this entity.

Composite primary key: (repo, coordinator_root_path, path). `repo` and
`coordinator_root_path` are connector-injected — they do not come from the
document itself.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from coordinator_core.contract.cockpit_schema.common import IsoDate
from coordinator_core.contract.cockpit_schema.provenance import ContentHash, ProvenanceEnvelope

DecisionGuideLifecycle = Literal["active", "archived"]
"""Lifecycle axis — is this decision-guide document still active?"""


class DecisionGuideSummary(BaseModel):
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
    # Relative path within the repo (e.g. "docs/decisions/DECISIONS.md").
    path: str
    title: str
    # ISO calendar date (YYYY-MM-DD) from decision-guide frontmatter.
    created: IsoDate
    status: DecisionGuideLifecycle
    provenance: ProvenanceEnvelope


    owner: str | None
    summary: str | None
    id_range: str | None
    decision_count: float | None

    content_hash: ContentHash | None = None
