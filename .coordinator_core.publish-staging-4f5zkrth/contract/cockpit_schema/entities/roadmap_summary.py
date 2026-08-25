"""
RoadmapSummary — summary view over state/roadmap/<slug>/OVERVIEW.md lifecycle
and status. Pydantic port of DoE
`coordinator/cockpit-contract/src/entities/roadmap-summary.ts` (Zod source).

Spec backlink: schemas/roadmap.yaml + docs/plans/2026-06-27-emit-new-record-types-producer-wiring.md
§ B1 + DECISIONS.md D9 + D5.

Composite primary key: (repo, coordinator_root_path, path). `title` is emitted
verbatim from roadmap frontmatter.

`repo` and `coordinator_root_path` are connector-injected (D4).
Nullable fields follow D9 (present-as-null, not optional).

Spec backlink: DoE-claude:pln-bash-to-naked-python-engine-mi-c09292 § T4e
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..common import IsoDate, IsoDateTime
from ..provenance import ContentHash, ProvenanceEnvelope
from .deliverable_spine import DeliverableStatus, WorkstreamType

RoadmapStatus = Literal["planning", "active", "blocked", "shipped", "archived"]


class _RoadmapRollUp(BaseModel):
    """
    Derived-at-emit single-initiative scalar roll-up computed by makima's
    roadmap_dag.py over all stubs belonging to this roadmap. `total` is the
    stub count; `by_status` maps each DeliverableStatus value to its count;
    `pct_shipped` is shipped/total (0-100, two decimal places) or null when
    total is zero. Anonymous nested shape (inlined, no $ref).
    """

    model_config = ConfigDict(extra="forbid")

    total: float
    by_status: dict[str, float]
    pct_shipped: float | None


class RoadmapSummary(BaseModel):
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
    """
    Relative path within the repo (e.g. "state/roadmap/my-roadmap/OVERVIEW.md").
    Composite key with repo + coordinator_root_path.
    """
    title: str
    """First H1 or frontmatter title of the roadmap document."""
    created: IsoDate
    """ISO calendar date (YYYY-MM-DD) from roadmap frontmatter."""
    status: RoadmapStatus
    """Roadmap lifecycle state from frontmatter."""
    provenance: ProvenanceEnvelope

    # Nullable fields (D9 present-as-null).
    owner: str | None
    """Owning team or person; null if not declared in frontmatter."""
    horizon: str | None
    """Free-form time-horizon label (e.g. "Q3 2026"); null if absent."""
    roadmap_id: str | None
    """Stable identifier for this roadmap; null if not set."""
    stub_id: str | None
    """Globally-unique slug-prefixed stub code (e.g. "ccos-1"); null if not set."""
    items: list[str] | None
    """Plan slugs or stub_ids this roadmap tracks; null if not declared."""
    blocks: list[str] | None
    """Roadmap_id values this roadmap gates; null if not declared."""
    blocked_by: list[str] | None
    """Roadmap_id values that gate this roadmap; null if not declared."""

    # Deliverable spine identity fields (D9 present-as-null).
    # Spec backlink: pln-fleet-deliverable-spine-identity-and-facets-2b331c § C1, D5.
    purpose: str | None
    """
    Prose facet: one-sentence business-legible purpose statement for this
    roadmap. D9: nullable, never optional — null when not authored.
    """
    deliverable_id: str | None
    """
    Durable join key for the deliverable this roadmap belongs to.
    D9: nullable, never optional — null during the pre-backfill window.
    """
    initiative: str | None
    """
    Initiative FK — references state/initiatives/<id>.yaml.
    D9: nullable, never optional — null when no initiative attaches.
    """
    caption: str | None
    """Business-legible one-sentence outcome line (CEO-board caption). D9: nullable, never optional."""
    status_reason: str | None
    """Free-text one-liner explaining the lifecycle status. D9: nullable, never optional."""
    last_meaningful_activity: IsoDateTime | None
    """
    Derived at emit: ISO-8601 UTC timestamp of the most-recent commit
    touching this deliverable's artifacts. D9: nullable, never optional.
    """
    workstream_type: WorkstreamType | None
    """
    Workstream type — derived from associated handoff category at emit; null
    when no handoff is linked. D9: nullable, never optional.
    """
    shipped_sha: str | None
    """
    Derived at emit: verified merge SHA (ancestor of origin/main).
    Null when not yet merged or fetch failed. D9: nullable, never optional.
    """
    deliverable_status: DeliverableStatus | None
    """
    Derived at emit: deliverable lifecycle status. Named `deliverable_status`
    to avoid collision with RoadmapSummary.status (RoadmapStatus).
    D9: nullable, never optional.
    """
    roll_up: _RoadmapRollUp | None
    """
    Derived-at-emit single-initiative scalar roll-up computed by makima's
    roadmap_dag.py over all stubs belonging to this roadmap. Null when the
    DAG is not computed for this roadmap (no stubs resolved, or DAG pass not
    run).

    Spec backlinks:
      docs/plans/2026-07-06-cockpit-contract-v260-initiative-status-widen-roadmap-dag.md § C
      project-rag/cross-repo/archive/2026-07-05-roadmap-dag-emit-rag-query-seam.md

    D9: nullable, never optional.
    """
    critical_path: list[str] | None
    """
    Ordered longest-chain stub_id list representing the critical path through
    the roadmap DAG — the sequence of stubs whose sequential dependency chain
    has the maximum length. Derived at emit by makima's roadmap_dag.py; null
    when the DAG is not computed for this roadmap.

    Spec backlinks:
      docs/plans/2026-07-06-cockpit-contract-v260-initiative-status-widen-roadmap-dag.md § C
      project-rag/cross-repo/archive/2026-07-05-roadmap-dag-emit-rag-query-seam.md

    D9: nullable, never optional.
    """
    scan_incomplete: bool
    """
    Derived at emit: True when the handoff scan backing roll_up could not read
    part of the subtree, making roll_up.total an undercount. A consumer deriving
    a percentage from roll_up MUST treat True as "this number is incomplete"
    rather than rendering it as fact.

    Deliberately NOT nullable, unlike its neighbours here: a consumer that has
    to distinguish absent-from-false cannot tell a clean scan from an old
    producer, which is the exact failure this field exists to close. Present
    always — False on a clean scan, never absent, never null.

    Spec backlinks:
      project-opticon/cross-repo/archive/2026-08-11-project-opticon-em-corrections-and-one-live-pct-shipped-defect.md
      state/sizings/2026-08-11-opticon-emission-defects-dropped-scan-in.yaml

    D9 exception ratified by claude-central-em 2026-08-11 (bilateral gate,
    MINOR additive 3.10.0 -> 3.11.0): required and key-present-always.
    """
    scan_errors: list[str]
    """
    Derived at emit: human-readable reasons the scan was incomplete, empty on a
    clean scan. Item type is pinned to string by the same ratification — not a
    structured error object — so consumers may render them directly.

    Present always, empty-list on a clean scan; never absent, never null. See
    scan_incomplete for why these two depart from the nullable convention.
    """

    content_hash: ContentHash | None = None
    """
    R5 content-hash change-signal (optional; sibling of provenance). Omitted by
    makima for records with no resolvable single source file (rolled-up aggregates,
    empty-path computed records). Version-neutral optional — absent on all existing
    records. Spec: producer-contract § 3.3.
    """
