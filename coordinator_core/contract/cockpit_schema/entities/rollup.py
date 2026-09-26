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

    model_config = ConfigDict(extra="forbid")

    # ISO-8601 UTC — latest observed_at across all input facts.
    max_observed_at: IsoDateTime
    max_commit_sha: str
    source_count: int


FactWindowKind = Literal["iso-week", "day"]


class FactWindow(BaseModel):

    model_config = ConfigDict(extra="forbid")

    kind: FactWindowKind
    # Inclusive bounds, ISO date YYYY-MM-DD, of the window actually used to select this row's facts.
    start: str
    end: str


class RollupNarrative(BaseModel):

    model_config = ConfigDict(extra="forbid")

    text: str
    generated_by: str
    generated_at: IsoDateTime
    input_watermark: RollupWatermark


class _DayDeterministicFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chains_completed: int
    tshirt_counts: dict[str, int]
    opus_dispatches: int
    commits: int


class DayRollup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grain: Literal["chain", "day"]
    # ISO date YYYY-MM-DD.
    period: str
    repo: str = Field(description=_REPO_DESCRIPTION)
    coordinator_root_path: str
    deterministic_facts: _DayDeterministicFacts
    narrative: RollupNarrative | None
    input_watermark: RollupWatermark
    freshness: Freshness
    provenance: ProvenanceEnvelope
    content_hash: ContentHash | None = None
    fact_window: FactWindow | None = None


class _WeekDeterministicFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chains_completed: int
    tshirt_counts: dict[str, int]
    opus_dispatches: int
    commits: int
    reviews_conducted: int
    verdicts: dict[str, int]


class WeekRollup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grain: Literal["week"]
    period: str
    repo: str = Field(description=_REPO_DESCRIPTION)
    coordinator_root_path: str
    deterministic_facts: _WeekDeterministicFacts
    narrative: RollupNarrative | None
    input_watermark: RollupWatermark
    freshness: Freshness
    provenance: ProvenanceEnvelope
    content_hash: ContentHash | None = None
    fact_window: FactWindow | None = None
