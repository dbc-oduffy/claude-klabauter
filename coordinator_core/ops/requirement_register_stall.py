"""The `requirement_register.stall_report` op, kept apart from `requirement_register` so that
module's import closure stays free of `coordinator_core.ipc`."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.requirement_register import (
    STALL_JP_ID,
    _parse_day,
    stall_judgment_point,
    stall_report,
)


@register_op("requirement_register.stall_report")
def _stall_report_op(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC ``requirement_register.stall_report``: the stall lines and the ceremony
    judgment point (``None`` when nothing stalled). Params: ``today`` (YYYY-MM-DD, optional),
    ``jp_id`` (the calling ceremony's judgment-point id, default ``STALL_JP_ID``)."""
    if repo_root is None:
        raise ValueError("requirement_register.stall_report requires a repo root")
    raw = params.get("today")
    today = _parse_day(raw) if raw is not None else date.today()
    if today is None:
        raise ValueError(f"today must be YYYY-MM-DD: {raw!r}")
    report = stall_report(Path(repo_root), today)
    return {
        "stalled": bool(report),
        "stale_rows": report.stale_rows,
        "stuck_plans": report.stuck_plans,
        "unclaimed_rows": report.unclaimed_rows,
        "judgment_point": stall_judgment_point(report, id=str(params.get("jp_id") or STALL_JP_ID)),
    }
