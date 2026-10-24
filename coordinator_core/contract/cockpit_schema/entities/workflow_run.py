"""
WorkflowRun — one dispatch-emit run's progress, projected from its
`run-stage.json` (CONTRACT_VERSION 4.16.0).

`status` is derived from `stage`: landed -> closed, halted -> halted, else live.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from coordinator_core.contract.cockpit_schema.common import IsoDateTime

WorkflowRunStage = Literal["minted", "staged", "halted", "landed"]
WorkflowRunStatus = Literal["live", "halted", "closed"]


class WorkflowRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    stage: WorkflowRunStage
    status: WorkflowRunStatus
    sizing_rel: str | None
    session_id: str | None
    started_at: IsoDateTime
    last_activity_at: IsoDateTime
    steps_planned: int | None
    """StageManifest row count, set at `staged`."""
    outcome: str | None
