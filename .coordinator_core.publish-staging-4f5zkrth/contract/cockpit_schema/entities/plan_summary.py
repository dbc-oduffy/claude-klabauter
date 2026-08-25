"""
PlanSummary — summary view over docs/plans/*.md lifecycle and status.
Pydantic port of DoE `coordinator/cockpit-contract/src/entities/plan-summary.ts`
(Zod source).

Spec backlink: state/roadmap/cockpit-contract-ext-2026-06-22/COORDINATOR-RESOLUTIONS.md
§ R3 + C-ext-4 + Camelia C-F6.

Composite primary key: (repo, coordinator_root_path, path). `author` is emitted
verbatim from plan frontmatter — no git-blame inference (C-F6).

`repo` and `coordinator_root_path` are connector-injected (D4).
Nullable fields follow D9 (present-as-null, not optional).

Spec backlink: DoE-claude:pln-bash-to-naked-python-engine-mi-c09292 § T4e
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from coordinator_core.contract.cockpit_schema.common import IsoDate, IsoDateTime
from coordinator_core.contract.cockpit_schema.entities.deliverable_spine import (
    DeliverableStatus,
    WorkstreamType,
)
from coordinator_core.contract.cockpit_schema.provenance import ContentHash, ProvenanceEnvelope

PlanStatus = Literal[
    "draft",
    "reviewed",
    "approved",
    "executing",
    "landed",
    "implemented",
    "deferred",
    "abandoned",
    "superseded",
]


class PlanSummary(BaseModel):
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
    Relative path within the repo (e.g. "docs/plans/2026-06-22-foo.md").
    Composite key with repo + coordinator_root_path (C-F6).
    """
    title: str
    """First H1 of the plan document."""
    created: IsoDate
    """
    ISO calendar date (YYYY-MM-DD) from plan frontmatter.
    IsoDateTime values (e.g. "2026-06-22T08:00:00Z") fail validation — emitter
    MUST truncate full datetime strings to date-only before emitting.
    """
    author: str
    """Author verbatim from frontmatter — no git-blame (C-F6)."""
    status: PlanStatus
    """Plan lifecycle state from frontmatter."""
    provenance: ProvenanceEnvelope

    # Nullable fields (D9 present-as-null) — present-as-null (no default).
    scope_mode: str | None
    """"atomic" | "fan-out" | etc. — null if not declared in frontmatter."""
    reviewer: str | None
    """Reviewer name from frontmatter; null if unreviewed."""
    branch: str | None
    """Git branch this plan was authored/executing on; null if not set."""
    superseded_by: str | None
    """Path to the superseding plan; null if not superseded."""
    source: str | None
    """Free-text source tag (roadmap slug, spinoff id, etc.); null if absent."""

    # ── Deliverable spine identity fields (D9 present-as-null) ───────────────
    # Spec backlink: pln-fleet-deliverable-spine-identity-and-facets-2b331c § C1.

    deliverable_id: str | None
    """
    Durable join key — minted at the earliest artifact, carried verbatim by all
    downstream artifacts of the same deliverable.  Null during the pre-backfill window.
    D9: nullable, never optional — key MUST be present.
    """
    plan_id: str | None
    """
    Stable id for this specific plan — minted by `coordinator-doc-new --type plan`.
    D9: nullable, never optional — null for plans authored before threading.
    """
    initiative: str | None
    """
    Initiative FK — references state/initiatives/<id>.yaml.
    D9: nullable, never optional — null when no initiative attaches.
    """
    caption: str | None
    """
    Business-legible one-sentence outcome line (CEO-board caption).
    D9: nullable, never optional.
    """
    status_reason: str | None
    """
    Free-text one-liner explaining the lifecycle status.
    D9: nullable, never optional.
    """
    owner: str | None
    """Authoring workstream/EM owner.  D9: nullable, never optional."""
    last_meaningful_activity: IsoDateTime | None
    """
    Derived at emit: ISO-8601 UTC timestamp of the most-recent commit touching
    this deliverable's artifacts.  D9: nullable, never optional.
    """
    workstream_type: WorkstreamType | None
    """
    Workstream type — derived from associated handoff category at emit; null when
    no handoff is linked.  D9: nullable, never optional.
    """
    shipped_sha: str | None
    """
    Derived at emit: verified merge SHA (ancestor of origin/main).
    Null when not yet merged or fetch failed.  D9: nullable, never optional.
    """
    deliverable_status: DeliverableStatus | None
    """
    Derived at emit: deliverable lifecycle status.
    Named `deliverable_status` to avoid collision with PlanSummary.status (PlanStatus).
    D9: nullable, never optional.
    """
    review_verified: bool | None
    """
    Derived at emit: `coordinator_core.ops.emit.deliverable_status.plan_review_verified()`
    applied to this record — True iff `review_verified_by` (C6a attest field) is present.
    An ordering predicate layered on top of `status`, not a `PlanStatus` enum member (hard
    constraint 1) and not itself a raw frontmatter field. D9: nullable, never optional.

    Field-shape sanctioned by the pre-existing `deliverable_status` derived-predicate field
    above; SemVer classification of this addition (MINOR/PATCH warn vs. MAJOR/DR-234 hold)
    is recorded at C7's dispatch site (docs/plans/2026-08-20-the-rungs-get-writers.md § C7,
    AC16), not here.

    Wired 2026-08-20 in `ops/emit/sections/plans.py::collect`, which populates it from
    `plan_review_verified(fm)` at collect time -- the predicate reads `review_verified_by`
    off frontmatter, so it needs none of the cross-record state the enrich pass exists for.
    Until that wiring landed the field was schema-required and never emitted, so every plan
    record failed a strict consumer's required-key check from 3.13.0 onward.
    """
    content_hash: ContentHash | None = None
    """
    R5 content-hash change-signal (optional; sibling of provenance). Omitted by
    makima for records with no resolvable single source file (rolled-up aggregates,
    empty-path computed records). Version-neutral optional — absent on all existing
    records. Spec: producer-contract § 3.3.
    """
