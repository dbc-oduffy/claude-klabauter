"""
backlog_history time-series block — pydantic port of DoE
`coordinator/cockpit-contract/src/entities/backlog-history.ts` (Zod source).
opticon's producer-emitted backlog-trend charts; a nested singular object on
SnapshotEnvelope.

Spec backlink: DoE-claude:pln-land-backloghistory-contract-b-7d3f40
Spec backlink: DoE-claude:pln-bash-to-naked-python-engine-mi-c09292 § T4e
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from coordinator_core.contract.cockpit_schema.common import IsoDate, IsoDateTime
from coordinator_core.contract.cockpit_schema.provenance import ProvenanceEnvelope

_MAX_SAFE_INTEGER = 9007199254740991


class DailyPoint(BaseModel):
    """Single day's backlog counts for one repo — the leaf node of the series."""

    model_config = ConfigDict(extra="forbid")

    date: IsoDate
    bug: int = Field(ge=0, le=_MAX_SAFE_INTEGER)
    improvement: int = Field(ge=0, le=_MAX_SAFE_INTEGER)
    lessons: int = Field(ge=0, le=_MAX_SAFE_INTEGER)


class RepoSeries(BaseModel):
    """Per-repo time series — an ordered array of DailyPoints for one repository."""

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
            "Opticon strips the owner prefix at render."
        ),
    )
    points: list[DailyPoint]


class BacklogHistory(BaseModel):
    """The complete backlog-trend block emitted onto SnapshotEnvelope."""

    model_config = ConfigDict(extra="forbid")

    # ISO-8601 UTC timestamp when this block was generated; present-as-null (D9).
    # Full timestamp (better staleness signal; matches provenance.observed_at);
    # opticon truncates to date for display.
    generated_at: IsoDateTime | None
    # Per-repo backlog time series; required present-but-empty array (D19 pattern) —
    # emitter emits [] not omit.
    series: list[RepoSeries]
    # Block-level required non-null provenance; keeps opticon provenance_fk NOT NULL
    # satisfied.
    provenance: ProvenanceEnvelope
