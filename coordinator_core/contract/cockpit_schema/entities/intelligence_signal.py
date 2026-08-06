"""
IntelligenceSignal — example-market-data-repo signal fact (Level-2 aggregate).
Pydantic port of example-doctrine-repo `coordinator/cockpit-contract/src/entities/intelligence-signal.ts`
(Zod source).

Second of two v2.16.0 example-market-data-repo emission entities. Per-repo (D25
case-1-by-shape): repo + provenance, no literal `scope` field (DD-2). Nullable
fields follow D9 (present-as-null). `bucket` is an OPEN STRING (DD-4 / DR-301 /
D26 §2) — a plain str, NOT a closed enum, so a new bucket never forces a
re-vendor. `extra="forbid"` per DD-7. coordinator_root_path nullable present-as-null.

v2.18.0 widen: 7 additive nullable descriptor/join fields (content_key,
claim_id, coverage_scope, sampling_basis, market_scope, confidence,
measurement_basis) reconciling example-market-data-repo's producer join/coverage
ask with cockpit's descriptor ask into one coherent union (D29).

v2.19.0 widen: types the `confidence_interval` field deferred at D29/v2.18.0
now that its wire shape is confirmed by example-market-data-repo + cockpit (D30).

Spec backlink: docs/plans/2026-07-14-cockpit-contract-widen-market-intel-entities.md
Spec backlink: coordinator/docs/wiki/cockpit-contract-entity-addition-protocol.md
Spec backlink: cross-repo/inbox/2026-07-14-example-cockpit-repo-em-market-intel-cockpit-contract-widen.md
Spec backlink: cross-repo/inbox/2026-07-14-claude-klabauter-em-intelligence-signal-widen-conflict.md
Spec backlink: cross-repo/inbox/2026-07-14-example-market-data-repo-em-doe-intelligence-signal-confidence-interval-type-confirmed.md
Spec backlink: DECISIONS.md D29, D30
Spec backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § T4e
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..common import IsoDateTime
from ..provenance import ProvenanceEnvelope


class _ObservedWindow(BaseModel):
    """Observation window this signal aggregates over. Anonymous nested shape (inlined, no $ref)."""

    # Review: code-reviewer — populate_by_name=True so the `from`/`from_`
    # reserved-word alias round-trips via kwargs, matching roadmap_dag_edge.py's
    # identical pattern.
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: IsoDateTime = Field(alias="from")
    to: IsoDateTime


class IntelligenceSignal(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    repo: str = Field(
        description=(
            "Owner-qualified repo identity: '<owner>/<repo>'. Owner carries the "
            "producing repo's own casing (producer-authoritative — there is no "
            "independent casing authority); no '.git' suffix. Consumers must not "
            "assume pre-normalization: normalizing to lower(owner)/lower(repo) for "
            "join, dedup, or storage keying is conformant. See DR-022. This "
            "owner-qualified string is the canonical cross-entity join anchor. "
            'Or "" for an entity-first fact (see provenance.entity_anchor).'
        )
    )
    # Connector key — nullable present-as-null: null = not machine-bound.
    coordinator_root_path: str | None
    signal_id: str
    """Stable id for this signal."""
    bucket: str
    """OPEN STRING bucket (DD-4 / DR-301): load-bearing but unbounded; no parse-time enum."""
    label: str
    """Human-readable signal label."""
    observed_at: IsoDateTime
    provenance: ProvenanceEnvelope

    # Nullable fields (D9 present-as-null).
    mention_count: float | None
    """Count of mentions over the observed_window."""
    sentiment_score: float | None
    """Sentiment score, range convention -1..1 (negative..positive)."""
    source_refs: list[str] | None
    """Supporting reference ids/urls for this signal."""
    observed_window: _ObservedWindow | None
    """Observation window this signal aggregates over."""
    display_order: float | None
    """Display ordering hint; ascending, nullable when unranked."""
    content_key: str | None = Field(
        description=(
            "sha256 hex of the backing ledger claim's canonical body — the "
            "projection-render join anchor (example-retrieval-repo fleet identity). "
            "Nullable on the shared contract; example-market-data-repo enforces "
            "non-null producer-side per cgs-03. Bare scalar join key (join on "
            "equality) — deliberately NOT the structured ContentHash provenance shape."
        )
    )
    claim_id: str | None = Field(
        description=(
            "Informational: example-retrieval-repo producer-local surrogate id. NOT a "
            "fleet-stable join key — join on content_key, never on this."
        )
    )
    coverage_scope: list[str] | None = Field(
        description=(
            "Reachable surfaces actually sampled for this signal, as a set "
            "(e.g. ['youtube','press']) — the coverage-bias qualifier for "
            "sentiment_score. Array, not a flattened string: consumers do "
            "surface-membership queries."
        )
    )
    sampling_basis: str | None = Field(
        description=(
            "Engagement-weighted / not-a-population-estimate qualifier for "
            "the coverage-bias objectivity spine."
        )
    )
    market_scope: str | None = Field(
        description=(
            "OPEN STRING market-scope descriptor (e.g. subject_repo | "
            "market_category | peer). This is a signal-level descriptor and is "
            "explicitly NOT the D25/EmissionScope `scope` discriminant — it "
            "MUST be excluded from any future corpus-wide scope-conformance "
            "mechanism (see DD-2 latent-gap note)."
        )
    )
    confidence: float | None = Field(
        description="Scalar point-estimate confidence for the signal."
    )
    confidence_interval: tuple[float, float] | None = Field(
        description=(
            "Confidence band as an ordered [lo, hi] numeric tuple in [0,1] "
            "around the scalar `confidence` point-estimate — NOT an interval "
            "around sentiment_score. Present-as-null. Wire shape confirmed by "
            "example-market-data-repo + cockpit 2026-07-14 (see D30)."
        )
    )
    measurement_basis: str | None = Field(
        description=(
            "OPEN STRING measurement provenance (e.g. structured_source | "
            "llm_extracted | human_verified | judge_panel — documented, not "
            "frozen; per docs/decisions/2026-07-14-reference-only-citation-"
            "emission-surface.md)."
        )
    )
