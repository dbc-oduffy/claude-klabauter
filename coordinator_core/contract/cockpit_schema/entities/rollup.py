"""
DayRollup and WeekRollup — deterministic aggregation is the SSOT; narrative is
a regenerable VIEW over the deterministic numbers (the Data Science Reviewer P1-D4).
Pydantic port of DoE `coordinator/cockpit-contract/src/entities/rollup.ts`
(Zod source).

Rule: deterministic aggregation (GROUP-BY over the completion-log `chain` key)
is reproducible and authoritative. The narrative is regenerable and MUST cite
its input watermark; it is never a substitute for the deterministic counts —
hence `deterministic_facts` is non-nullable and `narrative` is nullable.

Dedupe grain is the completion-log `chain` key: the three overlapping sources
(completion log, week-changelog daily blocks, daily-summaries — the last
already synthesised from the completion log) dedupe to this grain to avoid
double-counting in week rollups. tc-3 emission owns the dedupe; this contract
pins the shape.

Spec backlink: DoE-claude:pln-bash-to-naked-python-engine-mi-c09292 § T4e
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from coordinator_core.contract.cockpit_schema.common import IsoDateTime
from coordinator_core.contract.cockpit_schema.provenance import ContentHash, ProvenanceEnvelope

Freshness = Literal["current", "stale"]

_REPO_DESCRIPTION = (
    "Owner-qualified repo identity: '<owner>/<repo>'. Owner carries the "
    "producing repo's own casing (producer-authoritative — there is no "
    "independent casing authority); no '.git' suffix. Consumers must not "
    "assume pre-normalization: normalizing to lower(owner)/lower(repo) for "
    "join, dedup, or storage keying is conformant. See DR-022. This "
    "owner-qualified string is the canonical cross-entity join anchor. Empty "
    "string '' is the cross-repo-aggregate sentinel."
)


class RollupWatermark(BaseModel):
    """Watermark of the inputs a narrative was generated from — makes "stale as of when" falsifiable."""

    model_config = ConfigDict(extra="forbid")

    # ISO-8601 UTC — latest observed_at across all input facts.
    max_observed_at: IsoDateTime
    # Latest commit SHA in the period.
    max_commit_sha: str
    # Number of distinct source records consumed.
    source_count: int


#: `rolling-30d` is deliberately NOT a member. The pre-fix rolling window predates this field
#: entirely, so no emitter can ever produce a row carrying it — a row from that era has no
#: `fact_window` at all, and ABSENCE is the discriminator. A member nothing can emit reads to a
#: consumer as a case worth branching on, and that branch would be dead on arrival. Add it back
#: only alongside an emitter that actually produces it.
FactWindowKind = Literal["iso-week", "day"]


class FactWindow(BaseModel):
    """The window a rollup row's facts were computed over — states what the row DID, never what
    produced it (no emitter-version stamp; see the plan's rejection of that alternative).

    ABSENT means the row was emitted before this field existed: window unknown, and MUST be
    read as "do not label" — never defaulted to any kind, in particular never inferred from
    `max_observed_at` or any other wall-clock/deployment signal. During fleet rollout most rows
    come from engines without this field; giving it a default would silently relabel every
    stale row as week-scoped and reintroduce, at higher confidence, the exact defect this field
    fixes. Spec: docs/plans/2026-09-04-rollup-rows-name-their-own-fact-window.md.

    Carries both a semantic (`kind`) so a consumer can branch without parsing dates, and the
    actual inclusive bounds used, so a future window change is self-describing without minting
    a new `kind` value.
    """

    model_config = ConfigDict(extra="forbid")

    kind: FactWindowKind
    # Inclusive bounds, ISO date YYYY-MM-DD, of the window actually used to select this row's facts.
    start: str
    end: str


class RollupNarrative(BaseModel):
    """Regenerable narrative over the deterministic rollup; cites its input watermark."""

    model_config = ConfigDict(extra="forbid")

    text: str
    # Model/agent slug.
    generated_by: str
    generated_at: IsoDateTime
    input_watermark: RollupWatermark


class _DayDeterministicFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chains_completed: int
    # {"XS": N, "S": N, …}.
    tshirt_counts: dict[str, int]
    opus_dispatches: int
    commits: int


class DayRollup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grain: Literal["chain", "day"]
    # ISO date YYYY-MM-DD.
    period: str
    # "" for cross-repo aggregate.
    repo: str = Field(description=_REPO_DESCRIPTION)
    coordinator_root_path: str
    deterministic_facts: _DayDeterministicFacts
    # null if not yet generated; never a substitute for deterministic_facts.
    narrative: RollupNarrative | None
    input_watermark: RollupWatermark
    freshness: Freshness
    provenance: ProvenanceEnvelope
    # R5 content-hash change-signal (optional; sibling of provenance). Omitted by
    # claude-klabauter for records with no resolvable single source file (rolled-up
    # aggregates, empty-path computed records). Version-neutral optional —
    # absent on all existing records. Spec: producer-contract § 3.3.
    content_hash: ContentHash | None = None
    # The window this row's facts were computed over. Version-neutral optional —
    # absent on all rows emitted before this field existed; absence MUST be read
    # as "window unknown", never defaulted. See FactWindow's own docstring.
    fact_window: FactWindow | None = None


class _WeekDeterministicFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chains_completed: int
    tshirt_counts: dict[str, int]
    opus_dispatches: int
    commits: int
    reviews_conducted: int
    # {"ok": N, "warn": N, "blocked": N}.
    verdicts: dict[str, int]


class WeekRollup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grain: Literal["week"]
    # ISO week YYYY-Www.
    period: str
    # "" for cross-repo aggregate.
    repo: str = Field(description=_REPO_DESCRIPTION)
    coordinator_root_path: str
    deterministic_facts: _WeekDeterministicFacts
    narrative: RollupNarrative | None
    input_watermark: RollupWatermark
    freshness: Freshness
    provenance: ProvenanceEnvelope
    # R5 content-hash change-signal (optional; sibling of provenance). Omitted by
    # claude-klabauter for records with no resolvable single source file (rolled-up
    # aggregates, empty-path computed records). Version-neutral optional —
    # absent on all existing records. Spec: producer-contract § 3.3.
    content_hash: ContentHash | None = None
    # The window this row's facts were computed over. Version-neutral optional —
    # absent on all rows emitted before this field existed; absence MUST be read
    # as "window unknown", never defaulted. See FactWindow's own docstring.
    fact_window: FactWindow | None = None
