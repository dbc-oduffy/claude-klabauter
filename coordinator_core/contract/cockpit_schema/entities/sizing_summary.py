"""
SizingSummary — one `state/sizings/*.yaml` record projected for the cockpit
(CONTRACT_VERSION 4.16.0). Nullable slots are present-as-null, never absent.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

SizingResolution = Literal["unresolved", "planned", "closed"]


class SizingSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    intent: str
    route: str
    tshirt: str
    status: str
    xl_exit: str | None
    created: str
    exit_criterion_accepted: bool
    linked_plan: str | None
    """The sizing's `plan` foreign key; null until a plan is linked."""
    resolution: SizingResolution
