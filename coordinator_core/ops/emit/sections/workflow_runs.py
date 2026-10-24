"""Parity oracle: none (net-new section; covered by tests/test_sizings_and_workflow_runs_sections.py).

Section porter — WorkflowRun (envelope key: ``workflow_runs``).

One row per run record directory under ``state/warp/`` (or, for an in-flight run, ``scratch/warp/``) that carries ``run-stage.json`` and is
either non-terminal (``minted``/``staged``) or active within the last 7 days. ``sizing_rel``
comes from the run's ``run.json``; a run without ``run-stage.json`` (legacy) is skipped.
``outcome`` is always null. Emits nothing while the vendored schema pin lacks
``workflow-run.schema.json``, so emit never fails validation before the pin is re-vendored.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import ValidationError

from coordinator_core.contract.cockpit_schema.entities.workflow_run import WorkflowRun
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, RUN_DIR_ROOT, RUN_STAGE_FILE
from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.validate import _entity_schema_path

ENTITY = "workflow-run"
_RECENT = timedelta(days=7)
_LIVE_STAGES = frozenset({"minted", "staged"})
_STATUS_BY_STAGE = {"landed": "closed", "halted": "halted"}


def _read_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def collect(ctx: EmitContext) -> tuple[list[dict], list[dict]]:
    if not _entity_schema_path(ENTITY).exists():
        return [], []
    root = ctx.subprocess_root if ctx.subprocess_root is not None else ctx.repo_root
    now = _parse_ts(ctx.observed_at) or datetime.now(timezone.utc)
    run_dirs: dict[str, Path] = {}
    # An in-flight run's record sits only under scratch/warp (ask_contract.record_dir); state wins.
    for rel in (RECORD_DIR_ROOT, RUN_DIR_ROOT):
        try:
            for e in os.scandir(Path(root) / rel):
                if e.is_dir():
                    run_dirs.setdefault(e.name, Path(e.path))
        except OSError:
            continue

    records: list[dict] = []
    malformed: list[dict] = []
    for run_id in sorted(run_dirs):
        run_dir = run_dirs[run_id]
        stage_rec = _read_json(str(run_dir / RUN_STAGE_FILE))
        if stage_rec is None:
            continue
        stage = stage_rec.get("stage")
        last = _parse_ts(stage_rec.get("last_activity_at"))
        if stage not in _LIVE_STAGES and (last is None or now - last > _RECENT):
            continue
        run_rec = _read_json(str(run_dir / "run.json")) or {}
        sizing_rel = run_rec.get("sizing_rel")
        row = {
            "run_id": run_id,
            "stage": stage,
            "status": _STATUS_BY_STAGE.get(stage, "live"),
            "sizing_rel": sizing_rel if isinstance(sizing_rel, str) and sizing_rel else None,
            "session_id": stage_rec.get("session_id"),
            "started_at": stage_rec.get("started_at"),
            "last_activity_at": stage_rec.get("last_activity_at"),
            "steps_planned": stage_rec.get("steps_planned"),
            "outcome": None,
        }
        try:
            WorkflowRun.model_validate(row)
        except ValidationError as exc:
            malformed.append(
                {
                    "path": (run_dir / RUN_STAGE_FILE).relative_to(root).as_posix(),
                    "reason": f"fails WorkflowRun: {exc.error_count()} violation(s)",
                    "frontmatter_keys": sorted(stage_rec.keys()),
                }
            )
            continue
        records.append(row)
    return records, malformed
