"""repo.induct contract: params schema and the step record every induction step returns."""
from __future__ import annotations

from typing import Literal, TypedDict

PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "root": {"type": "string"},
        "repo_key": {"type": "string", "pattern": r"^repos\.[a-z0-9_]+$"},
        "project_type": {"type": "string"},
    },
    "required": ["root", "repo_key", "project_type"],
    "additionalProperties": False,
}

StepStatus = Literal["done", "skipped", "failed"]


class StepRecord(TypedDict):
    step: str
    status: StepStatus
    detail: str
    changed: list[str]


def make_step(
    step: str,
    status: StepStatus,
    detail: str = "",
    changed: list[str] | None = None,
) -> StepRecord:
    """Build a StepRecord; `changed` lists the paths the step wrote."""
    return {"step": step, "status": status, "detail": detail, "changed": list(changed or [])}
