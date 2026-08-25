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
  - docs/plans/2026-06-30-ccos-8-opticon-read-contract-spine-entities.md §Enrichment 3.B
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

    # ── Connector-injected fields ────────────────────────────────────────
    # Registry shortname — injected at emit time.
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
    # Absolute coordinator root path, injected as ".".
    coordinator_root_path: str

    # ── Required source fields ───────────────────────────────────────────
    # Harness UUID for this session — the canonical key. Shared identifier
    # space with ccos-3 linked_sessions and ccos-4 journal keys.
    session_id: str
    # Session role taxonomy. 'workstream' = synthetic container node;
    # 'session' = normal leaf; 'blitz' = bug-blitz or mise-en-place session.
    session_type: SessionType
    # Workstream slug grouping this session (the container identity, not
    # branch).
    workstream: str

    # ── Nullable fields (D9: present-as-null, never optional/absent) ────
    # Git branch recorded as lateral metadata. Derived from handoff `branch:`
    # field. NOT a workstream identity per the nimbalyst invariant.
    branch: str | None
    # Session ID of the parent session. Null for workstream-root sessions
    # (kind: spinoff* or predecessor: none) and for synthetic
    # workstream-type nodes.
    parent_session_id: str | None
    # Handoff paths this entry was derived from. Empty/null for
    # handoff-less sessions.
    linked_handoffs: list[str] | None

    # ── Flattened system block (source schema: system.*) ────────────────
    # The system block is flattened to top-level fields for emission
    # consistency. The on-disk source files retain the nested shape read by
    # get_provenance_completeness().
    # Session ID that created this hierarchy entry. ccos-3 system block field.
    created_by_session: str | None
    # ccos-3 provenance completeness. 'complete' when derive bridge
    # (consumed_by) is present; 'unknown' otherwise. KEPT separate from
    # `completeness` for the shared `get_provenance_completeness()` resolver
    # in `bin/lib/provenance.py:21-43`.
    #
    # NEGATIVE SPEC — why these fields are nullable here but REQUIRED in
    # FileAttribution: SessionHierarchy records may be ingested from older
    # projector versions that did not populate
    # system.provenance_completeness / system.completeness (older-projector
    # ingest path). null = "field was absent in the source record" (distinct
    # from 'unknown' which means "derivation was attempted but
    # inconclusive"). FileAttribution is derived at emit time from current
    # ledger rows and always has these fields set — never an
    # older-projector issue — so null is semantically impossible and the
    # field is correctly required there.
    provenance_completeness: SessionHierarchyProvenanceCompleteness | None
    # ccos-4 harvest-honesty: how the session_id was obtained.
    # Values: 'derived_handoff_lineage' | 'agent_sessions' | 'journal' | 'authored'.
    # Nullable — same older-projector ingest rationale as
    # provenance_completeness above.
    capture_source: str | None
    # ccos-4 harvest-honesty: data completeness.
    # 'complete' when all relationship fields are derived; 'partial' when
    # session_id is known but some fields are absent; 'unknown' when
    # derivation was not attempted. Nullable — same older-projector ingest
    # rationale as provenance_completeness above.
    completeness: HierarchyCompleteness | None

    # ── Provenance ────────────────────────────────────────────────────────
    # Source-grounding envelope; `derivation: "parsed"` for direct spine
    # reads.
    provenance: ProvenanceEnvelope
    # R5 content-hash change-signal (optional; sibling of provenance).
    # Omitted by makima for records with no resolvable single source file
    # (rolled-up aggregates, empty-path computed records). Version-neutral
    # optional — absent on all existing records. Spec: producer-contract
    # § 3.3.
    content_hash: ContentHash | None = None
