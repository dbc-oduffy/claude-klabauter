"""
FinancialMetricSummary — example-market-data-repo financial fact (Level-2 aggregate),
sourced from example-market-data-repo's sc1-06 EDGAR XBRL quantitative-claim output.
Pydantic port of coordinator-claude `coordinator/cockpit-contract/src/entities/financial-metric-summary.ts`
(Zod source), emitted as a third top-level peer array `financial_metrics[]`.

v2.21.0 addition (D31 bilateral: emitter-ownership transfer makes this widen
bilateral by construction — coordinator-claude ratifies the shape, claude-klabauter's emitter produces
the bytes). This is an entity-anchored financial fact (a company, via
provenance.entity_anchor): `repo` may legitimately be `""` with an
entity_anchor set, mirroring the ProvenanceEnvelope entity-first carve-out
CompetitorSummary/IntelligenceSignal already use. Natural key store-side is
`(repo, claim_id, content_key)`. `coordinator_root_path` is nullable
present-as-null (D9), never omitted. `content_key` is nullable-present on the
hosted contract — producer-side non-null enforcement stays in market-intel
(cgs-03); identical situation to IntelligenceSignal.content_key.
`extra="forbid"` per DD-7.

Spec backlink: cross-repo/inbox/2026-07-21-claude-central-em-financial-metric-summary-entity-emitter.md
Spec backlink: coordinator/docs/wiki/cockpit-contract-entity-addition-protocol.md
Source of record: example-market-data-repo's FinancialMetricRecord
(market_intel/contract/schema/financial_metric.schema.json)
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..common import IsoDateTime
from ..provenance import ProvenanceEnvelope


class FinancialMetricSummary(BaseModel):
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
    # Connector key — nullable present-as-null (D9): null = not machine-bound.
    coordinator_root_path: str | None
    claim_id: str
    """example-market-data-repo producer-local surrogate id. Part of the natural key (repo, claim_id, content_key)."""
    content_key: str | None = Field(
        description=(
            "sha256 hex of the backing ledger claim's canonical body — the "
            "projection-render join anchor (example-retrieval-repo fleet identity). "
            "Nullable-present on the hosted contract; example-market-data-repo "
            "enforces non-null producer-side per cgs-03."
        )
    )
    taxonomy: str
    """XBRL taxonomy the tag belongs to (e.g. us-gaap)."""
    tag: str
    """XBRL concept tag (e.g. Revenues)."""
    unit: str
    """Measurement unit (e.g. USD, shares)."""
    period_type: str
    """OPEN STRING period type (e.g. instant | duration)."""
    fiscal_period: str
    """Fiscal period label (e.g. FY2025Q3)."""
    period_end: str
    """Period end date (ISO calendar date string)."""
    value: float
    """The reported quantitative value."""
    observed_at: IsoDateTime
    provenance: ProvenanceEnvelope
    comparable: bool
    """Whether this value is comparable across periods without adjustment."""
    class_partition: str
    """OPEN STRING partition class for grouping/display."""

    # Nullable fields (D9 present-as-null).
    decimals_or_scale: int | None
    """XBRL decimals/scale attribute, when reported."""
    comparability_reason: str | None
    """Free-text reason when `comparable` is false or qualified."""
    confidence: float | None
    """Scalar point-estimate confidence for the extracted value."""
    confidence_interval: tuple[float, float] | None
    """Confidence band as an ordered [lo, hi] numeric tuple around `confidence`. Present-as-null."""
    measurement_basis: str | None
    """OPEN STRING measurement provenance (e.g. structured_source | llm_extracted | human_verified)."""
