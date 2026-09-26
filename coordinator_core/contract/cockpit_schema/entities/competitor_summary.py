from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..common import IsoDateTime
from ..provenance import ProvenanceEnvelope

CompetitorSegment = Literal["mobile", "console_pc", "web", "all", "other"]
"""Market segment. Required, non-null; 'all' is the catch-all value."""

CompetitorCategory = Literal["competitor", "peer", "aspirational_target", "first_party"]
"""Relationship class of the competitor to us. 'first_party' is a Example-Fleet-owned
product or parent company carried in the same registry (DR-192, DoE-claude) —
the alternative was category:null, which discards the stance."""

CompetitorStatus = Literal["reference", "incumbent", "analyzed", "gap_closed"]
"""Analysis lifecycle state of the competitor."""


class CompetitorSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo: str = Field(
        description=(
            "Owner-qualified repo identity: '<owner>/<repo>'. Owner carries the "
            "producing repo's own casing (producer-authoritative — there is no "
            "independent casing authority); no '.git' suffix. Consumers must not "
            "assume pre-normalization: normalizing to lower(owner)/lower(repo) for "
            "join, dedup, or storage keying is conformant. See DR-022. This "
            "owner-qualified string is the canonical cross-entity join anchor. "
            'Or "" for an entity-first (no-repo) fact anchored via '
            "provenance.entity_anchor."
        )
    )
    coordinator_root_path: str | None
    competitor_uid: str | None
    competitor_id: str
    """Display slug, denormalized — NOT a join key."""
    name: str
    """Human-readable competitor name."""
    segment: CompetitorSegment
    """Required-present, no pydantic default (present-always discipline)."""
    observed_at: IsoDateTime
    provenance: ProvenanceEnvelope

    category: CompetitorCategory | None
    status: CompetitorStatus | None
    note: str | None
    """Free-text analyst note."""
    display_order: float | None
    """Display ordering hint; ascending, nullable when unranked."""

    @model_validator(mode="after")
    def _check_competitor_uid_presence(self) -> "CompetitorSummary":
        anchor = self.provenance.entity_anchor
        if anchor is not None and anchor.kind == "competitor_uid" and self.competitor_uid is None:
            raise ValueError(
                "competitor_uid must be non-null when "
                "provenance.entity_anchor.kind === 'competitor_uid' (DD-8)"
            )
        return self
