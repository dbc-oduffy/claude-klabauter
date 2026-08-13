"""
FileAttribution — per-(session_id, file_path) aggregate view over session→file
touches. Pydantic port of coordinator-claude
`coordinator/cockpit-contract/src/entities/file-attribution.ts` (Zod source).

The source is now an on-demand derivation over Claude Code natural transcripts
(~/.claude/projects/<project>/<session_id>.jsonl) via bin/derive-file-attribution.py
— there is no producer and no persisted ledger. A session's raw touches
(hundreds of tool calls) collapse to ONE row per DISTINCT file a session
touched, with link_type breakdown counts and edit-metadata sums. This bounds
snapshot size by distinct-file count without dropping any file from the record.

D-FA-SHAPE decision: per-(session, file_path) aggregate is the default
emission shape. The exact granularity cockpit needs is confirmed in the C7
Cockpit ask memo — if cockpit requests raw rows, this aggregate can be
superseded in a future bump.

Honesty markers (completeness, capture_source, provenance_completeness) are
the worst-case values across all aggregated ledger rows for this
(session_id, file_path) pair. They must never be stripped at the projection
boundary — AC6 mandates their presence.

Spec backlinks:
  - schemas/file-attribution.schema.json (raw ledger row schema)
  - docs/plans/2026-06-30-ccos-8-cockpit-read-contract-spine-entities.md § C3
  - docs/wiki/cockpit-contract-entity-addition-protocol.md §Steps
  - Decision D-FA-SHAPE (plan § Decisions)
  - docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § T4e
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from coordinator_core.contract.cockpit_schema.provenance import ContentHash, ProvenanceEnvelope

SafeInt = Annotated[int, Field(ge=-9007199254740991, le=9007199254740991)]

FileAttributionOperation = Literal["edit", "create", "delete", "rename", "bash"]
"""Closed enum for last_operation — mirrors metadata.operation in the raw ledger schema."""

FileAttributionCompleteness = Literal["complete", "partial", "unknown"]
"""
Harvest completeness — worst-case across aggregated rows.
'complete': all expected fields for each row's link_type are present and authoritative.
'partial': one or more metadata fields absent in at least one aggregated row.
'unknown': could not determine completeness for at least one row.
"""

FileAttributionCaptureSource = Literal["journal_projection", "hook_capture", "derived"]
"""
Capture source — worst-case across aggregated rows.
'journal_projection': rows sourced from the Stop-hook harvest pipeline (legacy
path; primary source is now transcript derivation via derive-file-attribution.py).
'hook_capture': rows sourced from the real-time file-touch tracking hook.
'derived': rows computed or inferred from other captured data.
"""

FileAttributionProvenanceCompleteness = Literal["complete", "unknown"]
"""
Provenance completeness — worst-case across aggregated rows.
'complete': full provenance chain known for all aggregated rows.
'unknown': at least one row's provenance chain could not be fully verified.
"""


class FileAttribution(BaseModel):
    """
    One row per DISTINCT file touched by a session.

    Primary key: (session_id, file_path).
    Connector-injected: repo, coordinator_root_path, provenance.
    """

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
    """Connector-injected registry shortname."""
    coordinator_root_path: str
    """Connector-injected — matches other cockpit emission entities."""
    session_id: str
    """Session that touched this file. Part of the (session_id, file_path) primary key."""
    file_path: str
    """
    Repo-relative path of the file. Part of the (session_id, file_path)
    primary key. Normalised to forward slashes — directly comparable to
    git-ls-files output.
    """
    edited_count: SafeInt
    """Count of 'edited' link_type events for this file in this session."""
    read_count: SafeInt
    """Count of 'read' link_type events for this file in this session."""
    referenced_count: SafeInt
    """Count of 'referenced' link_type events for this file in this session."""
    lines_added: SafeInt | None
    """
    Sum of linesAdded across all 'edited' rows for this file. Null when no
    edited rows carry linesAdded (e.g. all Bash-mediated edits).
    """
    lines_removed: SafeInt | None
    """Sum of linesRemoved across all 'edited' rows for this file. Null when no edited rows carry linesRemoved."""
    last_operation: FileAttributionOperation | None
    """
    The last operation type across all 'edited' rows for this file. Null when
    no 'edited' rows exist for this file (pure reads/references).
    """

    # ── Honesty markers (worst-case across aggregated rows) ─────────────────
    # These must never be stripped at the projection boundary — AC6.

    completeness: FileAttributionCompleteness
    """Harvest completeness — worst-case across all aggregated ledger rows. See FileAttributionCompleteness."""
    capture_source: FileAttributionCaptureSource
    """How the underlying attribution data was obtained — worst-case across rows. See FileAttributionCaptureSource."""
    provenance_completeness: FileAttributionProvenanceCompleteness
    """Whether the provenance chain is fully known — worst-case across rows. See FileAttributionProvenanceCompleteness."""

    provenance: ProvenanceEnvelope
    """Full source-grounding provenance envelope; derivation: "parsed"."""
    content_hash: ContentHash | None = None
    """
    R5 content-hash change-signal (optional; sibling of provenance). Omitted by
    claude-klabauter for records with no resolvable single source file (rolled-up
    aggregates, empty-path computed records). Version-neutral optional —
    absent on all existing records. Spec: producer-contract § 3.3.
    """
