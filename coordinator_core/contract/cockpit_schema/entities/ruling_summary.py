"""
RulingSummary — one APM ruling record for the cockpit (CONTRACT_VERSION 4.16.0).
Served through `records.query`, not a SnapshotEnvelope key.

One shape for both ruling variants: a fence-less ruling yields every optional key null.
The `from` wire key is carried by the alias `from_` (`from` is a Python keyword).
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from coordinator_core.contract.cockpit_schema.common import IsoDateTime


class RulingSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    path: str
    observed_at: IsoDateTime
    title: str | None
    from_: str | None = Field(alias="from")
    created: str | None
    source: str | None
    decision: str | None
    pm_must_see: bool | str | None
