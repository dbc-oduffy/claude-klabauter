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
    id: str
    label: str
    provenance: ProvenanceEnvelope


    owner: str | None
    status: InitiativeStatus | None
    description: str | None
    content_hash: ContentHash | None = None
    goals: list[Goal] | None = None
