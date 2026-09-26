from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from coordinator_core.contract.cockpit_schema.common import IsoDate, IsoDateTime
from coordinator_core.contract.cockpit_schema.provenance import ProvenanceEnvelope

_MAX_SAFE_INTEGER = 9007199254740991


class DailyPoint(BaseModel):

    model_config = ConfigDict(extra="forbid")

    date: IsoDate
    bug: int = Field(ge=0, le=_MAX_SAFE_INTEGER)
    improvement: int = Field(ge=0, le=_MAX_SAFE_INTEGER)
    lessons: int = Field(ge=0, le=_MAX_SAFE_INTEGER)


class RepoSeries(BaseModel):

    model_config = ConfigDict(extra="forbid")

    repo: str = Field(
        min_length=1,
        description=(
            "Owner-qualified repo identity: '<owner>/<repo>'. Owner carries the "
            "producing repo's own casing (producer-authoritative — there is no "
            "independent casing authority); no '.git' suffix. Consumers must not "
            "assume pre-normalization: normalizing to lower(owner)/lower(repo) for "
            "join, dedup, or storage keying is conformant. See DR-022. This "
            "owner-qualified string is the canonical cross-entity join anchor. "
            "Cockpit strips the owner prefix at render."
        ),
    )
    points: list[DailyPoint]


class BacklogHistory(BaseModel):

    model_config = ConfigDict(extra="forbid")

    # ISO-8601 UTC timestamp when this block was generated; present-as-null (D9).
    generated_at: IsoDateTime | None
    series: list[RepoSeries]
    provenance: ProvenanceEnvelope
