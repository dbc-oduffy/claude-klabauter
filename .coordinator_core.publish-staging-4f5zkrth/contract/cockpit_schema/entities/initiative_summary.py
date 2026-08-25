"""
InitiativeSummary — lightweight parent entity for work identity. Pydantic
port of DoE `coordinator/cockpit-contract/src/entities/initiative-summary.ts`
(Zod source).

An initiative is the named business bet / strategic theme that one or more
deliverables advance. It is the FK target of the `initiative` field on
HandoffSummary, PlanSummary, and RoadmapSummary. On-disk records live at
`state/initiatives/<id>.yaml` (per-repo; central-seam-routed in the meta-repo).

A roadmap is ONE KIND of initiative that attaches to it; a cross-repo bet with
no formal roadmap is also a first-class initiative. This entity does NOT build
the roadmap DAG/roll-up — that is the roadmap-first-class-tracking spinoff.

`repo` and `coordinator_root_path` are connector-injected (D4), consistent
with all other summary entities. Nullable fields follow D9 (present-as-null,
not optional).

Spec backlink: pln-fleet-deliverable-spine-identity-and-facets-2b331c § D2.
Spec backlink: DoE-claude:pln-bash-to-naked-python-engine-mi-c09292 § T4e
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from coordinator_core.contract.cockpit_schema.entities.goal import Goal
from coordinator_core.contract.cockpit_schema.provenance import ContentHash, ProvenanceEnvelope

InitiativeStatus = Literal["active", "paused", "shipped", "abandoned"]
"""
Initiative lifecycle — canonical-4 set.
active=in flight, paused=temporarily blocked, shipped=done, abandoned=cancelled.
"""


class InitiativeSummary(BaseModel):
    """The named business bet / strategic theme entity."""

    model_config = ConfigDict(extra="forbid")

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
    # Stable kebab-case id for this initiative, e.g. "ini-delphi-os-bet".
    # This is the FK value carried by `initiative:` on other artifact summaries.
    id: str
    # Human-readable initiative label (short name shown on the CEO board).
    label: str
    provenance: ProvenanceEnvelope

    # Nullable fields (D9 present-as-null).

    # Owning team or person; null if not declared.
    owner: str | None
    # Current lifecycle state of the initiative; null if not yet set.
    status: InitiativeStatus | None
    # Business-legible one-sentence description of the initiative's outcome.
    # D9: nullable, never optional.
    description: str | None
    # R5 content-hash change-signal (optional; sibling of provenance). Omitted
    # by makima for records with no resolvable single source file (rolled-up
    # aggregates, empty-path computed records). Version-neutral optional —
    # absent on all existing records. Spec: producer-contract § 3.3.
    content_hash: ContentHash | None = None
    # Goal refs for the goals→initiatives→status ladder (opticon buildLadder
    # reads it defensively). Version-neutral optional — absent on all
    # existing records (2.13.0, D24). "Defensively" means opticon handles an
    # absent/empty array, NOT per-item malformed-goal tolerance: a nested
    # Goal parse failure fails the whole InitiativeSummary record into the
    # malformed bucket (all-or-nothing).
    goals: list[Goal] | None = None
