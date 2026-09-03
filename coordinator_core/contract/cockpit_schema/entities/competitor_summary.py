"""
CompetitorSummary — example-market-data-repo competitor relationship fact (Level-2).
Pydantic port of DoE `coordinator/cockpit-contract/src/entities/competitor-summary.ts`
(Zod source).

One of two entities added in the v2.16.0 widen for cockpit's example-market-data-repo
ingest path. Per-repo (D25 case-1-by-shape): carries repo + provenance, no literal
`scope` field (DD-2). Grounded in D26 entity_anchor — a competitor fact with no
fleet repo sets provenance.repo:"" and carries identity via
provenance.entity_anchor {kind:'competitor_uid', value:<uid>}.

Nullable fields follow D9 (present-as-null, never optional). coordinator_root_path
is nullable present-as-null (DD-1 AMEND: carried not omitted — machine-blind = key
present carrying null). `extra="forbid"` per DD-7. competitor_uid non-null whenever
provenance.entity_anchor.kind === 'competitor_uid' per DD-8 (see model_validator) —
one-directional presence check; value-equality is deliberately NOT enforced.

Spec backlink: DoE-claude:pln-cockpit-contract-widen-competi-0d708f
Spec backlink: coordinator/docs/wiki/cockpit-contract-entity-addition-protocol.md
Spec backlink: cross-repo/inbox/2026-07-14-example-cockpit-repo-em-market-intel-cockpit-contract-widen.md
Spec backlink: DoE-claude:pln-bash-to-naked-python-engine-mi-c09292 § T4e
"""
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
    # Connector key — nullable present-as-null (DD-1): null = not machine-bound.
    coordinator_root_path: str | None
    # Natural join key (cockpit keys on (repo, competitor_uid, segment)).
    # Present-as-null; non-null whenever provenance.entity_anchor.kind ===
    # 'competitor_uid' (DD-8).
    competitor_uid: str | None
    competitor_id: str
    """Display slug, denormalized — NOT a join key."""
    name: str
    """Human-readable competitor name."""
    segment: CompetitorSegment
    """Required-present, no pydantic default (present-always discipline)."""
    observed_at: IsoDateTime
    provenance: ProvenanceEnvelope

    # Nullable fields (D9 present-as-null).
    category: CompetitorCategory | None
    status: CompetitorStatus | None
    note: str | None
    """Free-text analyst note."""
    display_order: float | None
    """Display ordering hint; ascending, nullable when unranked."""

    @model_validator(mode="after")
    def _check_competitor_uid_presence(self) -> "CompetitorSummary":
        # DD-8: top-level competitor_uid must be non-null whenever
        # provenance.entity_anchor.kind === 'competitor_uid' (one-directional
        # presence check; value-equality between the two is deliberately NOT
        # enforced — that would exceed the ratified DD-8 ruling).
        anchor = self.provenance.entity_anchor
        if anchor is not None and anchor.kind == "competitor_uid" and self.competitor_uid is None:
            raise ValueError(
                "competitor_uid must be non-null when "
                "provenance.entity_anchor.kind === 'competitor_uid' (DD-8)"
            )
        return self
