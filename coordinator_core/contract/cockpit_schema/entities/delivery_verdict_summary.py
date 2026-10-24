"""
DeliveryVerdictSummary — one delivery-verdict record for the cockpit
(CONTRACT_VERSION 4.16.0). Served through `records.query`, not a SnapshotEnvelope key.

Canonical keys are the nested `delivery.verdict` and `criterion.status`.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from coordinator_core.contract.cockpit_schema.common import IsoDateTime


class DeliveryVerdictDelivery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: str
    unbacked: list[dict[str, Any]]
    """Rows as the verifier wrote them (usually `{claim, anchor}`, either value may be null); row keys are not schema-enforced. Empty when none."""


class DeliveryVerdictCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    observation: str | None
    sidecar: str | None


class DeliveryVerdictSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    observed_at: IsoDateTime
    plan_id: str | None
    head_sha: str | None
    recorded_at: str | None
    session_id: str | None
    delivery: DeliveryVerdictDelivery
    criterion: DeliveryVerdictCriterion
    foreign_claims: list[str]
