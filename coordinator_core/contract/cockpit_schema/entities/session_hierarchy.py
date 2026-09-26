"""
SessionHierarchy — pydantic port of DoE
`coordinator/cockpit-contract/src/entities/session-hierarchy.ts` (Zod source).
Cockpit emission entity for a single session/workstream node in the
per-machine session hierarchy projection (ccos-5).

Source records live at `state/session-hierarchy.*.json`; the source schema is
`schemas/session-hierarchy.schema.json`. This entity flattens the `system`
block into top-level fields for emission consistency with other cockpit
entities (the `get_provenance_completeness()` resolver in
`bin/lib/provenance.py` reads the on-disk source files — NOT the emission
projection — so flattening here is safe).

Spec backlinks:
  - schemas/session-hierarchy.schema.json
  - docs/plans/2026-06-30-ccos-8-cockpit-read-contract-spine-entities.md §Enrichment 3.B
  - docs/wiki/cockpit-contract-entity-addition-protocol.md §Steps
  - docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § T4e

D9 nullability discipline: every optional field is `T | None` with no default
(present-as-null) — never `T | None = None` (true optional). A field absent
from the JSON is REJECTED, not silently skipped.

Composite primary key: (repo, coordinator_root_path, session_id). `repo` and
`coordinator_root_path` are connector-injected at emit time.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from coordinator_core.contract.cockpit_schema.provenance import ContentHash, ProvenanceEnvelope

SessionType = Literal["session", "workstream", "blitz"]
"""Session role taxonomy — closed enum per the source schema."""

SessionHierarchyProvenanceCompleteness = Literal["complete", "unknown"]
"""
Provenance completeness — closed enum; ccos-3 field.
Prefixed SessionHierarchy- to avoid collision with any future
provenance.py-resident ProvenanceCompleteness. FileAttribution uses the
analogous FileAttributionProvenanceCompleteness pattern.
"""

HierarchyCompleteness = Literal["complete", "partial", "unknown"]
"""Harvest-honesty completeness — closed enum; ccos-4 sibling."""


class SessionHierarchy(BaseModel):
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

    session_id: str
    session_type: SessionType
    workstream: str

    branch: str | None
    parent_session_id: str | None
    linked_handoffs: list[str] | None

    created_by_session: str | None
    # NEGATIVE SPEC — why these fields are nullable here but REQUIRED in
    provenance_completeness: SessionHierarchyProvenanceCompleteness | None
    capture_source: str | None
    completeness: HierarchyCompleteness | None

    provenance: ProvenanceEnvelope
    content_hash: ContentHash | None = None
