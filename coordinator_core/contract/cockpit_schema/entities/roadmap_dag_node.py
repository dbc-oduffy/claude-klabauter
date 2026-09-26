"""
RoadmapDagNode — a single stub-node in a roadmap dependency graph. Pydantic
port of DoE `coordinator/cockpit-contract/src/entities/roadmap-dag-node.ts`
(Zod source).

Represents one stub's position within a roadmap DAG as emitted by claude-klabauter's
`coordinator_core/ops/roadmap_dag.py`. Each node carries the stub's current
phase status plus scheduling metadata (sprint, wave) and the SHA at which it
shipped.

Spec backlinks:
  - docs/plans/2026-07-06-cockpit-contract-v260-initiative-status-widen-roadmap-dag.md § Chunk B
  - docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § T4e

Logical identity: (repo [= repo_key], roadmap_id, stub_id).
`coordinator_root_path` is an ADDITIVE connector key present on every
connector-emitted entity — it is NOT part of the logical DAG identity. rag's
durable node/edge table MUST key on the 3-field logical identity, not the
4-field connector composite.

`status` is a free string (NOT coupled to RoadmapStatus enum) because node
status reflects per-stub roadmap phase and must remain permissive to avoid
re-coupling across minor roadmap taxonomy changes.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from coordinator_core.contract.cockpit_schema.provenance import ContentHash, ProvenanceEnvelope


class RoadmapDagNode(BaseModel):

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
    roadmap_id: str
    stub_id: str
    status: str | None
    sprint: str | int | float | None
    wave: str | int | float | None
    shipped_sha: str | None
    provenance: ProvenanceEnvelope
    content_hash: ContentHash | None = None
