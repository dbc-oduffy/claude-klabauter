"""
RoadmapDagEdge — pydantic port of DoE
`coordinator/cockpit-contract/src/entities/roadmap-dag-edge.ts` (Zod source).
A directed dependency edge in a roadmap DAG.

Represents a single "blocks" relationship between two stubs within a roadmap
as emitted by claude-klabauter's `coordinator_core/ops/roadmap_dag.py`. The seam stores
one direction; reverse is derived by consumers.

Spec backlinks:
  - docs/plans/2026-07-06-cockpit-contract-v260-initiative-status-widen-roadmap-dag.md § Chunk B
  - docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § T4e

`type` is `Literal["blocks"]` — intentionally rigid. Adding an edge type is a
governed contract bump (not an oversight); a future reader who considers
widening to an enum should open a new version rather than loosening this type
in place.

Logical identity: (repo, roadmap_id, from, to).
`coordinator_root_path` is an ADDITIVE connector key present on every
connector-emitted entity — it is NOT part of the logical DAG identity.
rag's edge table MUST key on the 4-field logical identity, not the 5-field
connector composite.

`from` is a reserved word in Python — the field is declared as `from_` with
`Field(alias="from")` so both parse (`model_validate`) and emission
(`model_dump(by_alias=True)` / `model_json_schema()`, which defaults to
`by_alias=True`) speak the wire name `from`, byte-identical to the Zod
source's `from` key.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from coordinator_core.contract.cockpit_schema.provenance import ContentHash, ProvenanceEnvelope


class RoadmapDagEdge(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

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
    # Stable identifier for the roadmap this edge belongs to.
    roadmap_id: str
    # stub_id of the blocking (upstream) node.
    from_: str = Field(alias="from")
    # stub_id of the blocked (downstream) node.
    to: str
    # Edge type — rigidly "blocks". Adding a new edge type is a governed
    # contract bump, not an in-place widen.
    type: Literal["blocks"]
    provenance: ProvenanceEnvelope

    # R5 content-hash change-signal (optional; sibling of provenance). Omitted
    # by claude-klabauter for records with no resolvable single source file (rolled-up
    # aggregates, empty-path computed records). Version-neutral optional —
    # absent on all existing records. Spec: producer-contract § 3.3.
    content_hash: ContentHash | None = None
