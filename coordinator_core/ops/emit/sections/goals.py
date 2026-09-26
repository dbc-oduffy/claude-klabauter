
from __future__ import annotations

import logging

from coordinator_core.goals.wire_read import read_and_collapse
from coordinator_core.ops.emit.context import EmitContext

_LOG = logging.getLogger(__name__)


class GoalsStateRootUnreadable(Exception):
    pass


def collect(ctx: EmitContext) -> tuple[list[dict], list[dict]]:
    central_state_root = ctx.central_state_root
    result = read_and_collapse(central_state_root, default_repo=ctx.repo_name)

    if result.unreadable_error is not None:
        _LOG.warning(
            "goals: cannot scan central_state_root %s — %s; goals_current would "
            "otherwise wrongly report zero goals for an unscannable root",
            central_state_root,
            result.unreadable_error,
        )
        raise GoalsStateRootUnreadable(
            f"{central_state_root}: {result.unreadable_error}"
        ) from result.unreadable_error

    records: list[dict] = []
    for row in result.rows:
        record, log_path = row.record, row.shard_path
        period = record.get("period", "")
        derivation = "parsed" if period == "week" else "rolled_up"

        emitted = {
            "goal_id": record.get("goal_id") or row.goal_id,
            "repo": record.get("repo", ctx.repo_name),
            "coordinator_root_path": record.get("coordinator_root_path", "."),
            "period": period,
            "period_value": record.get("period_value", ""),
            "declared_by_machine": record.get("declared_by_machine", "unknown"),
            "declared_at": record.get("declared_at", ctx.observed_at),
            "text": record.get("text", ""),
            "status": record.get("status", "active"),
            "parent_goal_id": record.get("parent_goal_id"),
            "provenance": ctx.provenance(
                "coordinator_artifact",
                path=f"state/{log_path.name}",
                derivation=derivation,
            ),
        }

        # NOT `.nullable()` (present-as-null) — this is a DELIBERATE EXCEPTION to the D9
        if "weekly_perceptible" in record:
            emitted["weekly_perceptible"] = record["weekly_perceptible"]

        key_results_status_raw = record.get("key_results_status")
        if isinstance(key_results_status_raw, list) and key_results_status_raw:
            kr_status = [
                {
                    "id": kr.get("id", ""),
                    "text": kr.get("text", ""),
                    "kind": kr.get("kind", ""),
                    "status": kr.get("status", ""),
                }
                for kr in key_results_status_raw
                if isinstance(kr, dict)
            ]
            if kr_status:
                emitted["key_results_status"] = kr_status

        records.append(emitted)

    return records, []
