"""
TrackerSummary — summary view over docs/project-tracker.md lifecycle and
status. Pydantic port of coordinator-claude
`coordinator/cockpit-contract/src/entities/tracker-summary.ts` (Zod source).

Spec backlink: schemas/tracker.yaml + docs/plans/2026-06-27-emit-new-record-types-producer-wiring.md
§ B1 + DECISIONS.md D9 + D5.

Composite primary key: (repo, coordinator_root_path, path). `title` is
emitted verbatim from tracker frontmatter.

`repo` and `coordinator_root_path` are connector-injected (D4). Nullable
fields follow D9 (present-as-null, not optional).

Spec backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § T4e
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from coordinator_core.contract.cockpit_schema.common import IsoDate
from coordinator_core.contract.cockpit_schema.provenance import ContentHash, ProvenanceEnvelope

TrackerStatus = Literal["active", "archived"]


class TrackerSummary(BaseModel):
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
    """Connector-injected — matches other summary entities."""
    path: str
    """Relative path within the repo (e.g. "docs/project-tracker.md"). Composite key with repo + coordinator_root_path."""
    title: str
    """First H1 or frontmatter title of the tracker document."""
    created: IsoDate
    """ISO calendar date (YYYY-MM-DD) from tracker frontmatter."""
    status: TrackerStatus
    """Tracker lifecycle state from frontmatter."""
    provenance: ProvenanceEnvelope

    # Nullable fields (D9 present-as-null).

    owner: str | None
    """Owning team or person; null if not declared in frontmatter."""
    items: list[str] | None
    """Action items tracked by this board; null if not declared."""
    content_hash: ContentHash | None = None
    """
    R5 content-hash change-signal (optional; sibling of provenance). Omitted by
    claude-klabauter for records with no resolvable single source file (rolled-up
    aggregates, empty-path computed records). Version-neutral optional —
    absent on all existing records. Spec: producer-contract § 3.3.
    """
