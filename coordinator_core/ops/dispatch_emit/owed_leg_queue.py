"""coordinator_core.ops.dispatch_emit.owed_leg_queue -- persist the legs a halt left owed.

A leg the run withheld for the EM or an operator (`OwedRow`, reason in `EM_OWNED_REASONS`) lives
only in the run reply; `write_owed_legs` keeps it as `state/owed-legs/<run_id>--<id>.yaml` until
discharged. `open_owed_legs` derives discharge, it never records one.

INVARIANT: a leg is discharged when its plan spine row's `disposition` is not `open` (`resolve`,
the operator discharge) OR `plan_tasks_mutate.read_em_discharges` holds its id (`em-discharge`).

TRAP: `em-discharge` writes only `em_discharged` in `<plan-stem>.evidence.yaml` and leaves the row
`open`; checking disposition alone keeps every em-discharged leg owed forever.

A plan that cannot be read leaves its legs open: an unverifiable discharge is not a discharge.
Stdlib + yaml only; no git, no subprocess.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

OWED_LEGS_DIR = Path("state") / "owed-legs"
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(value: str) -> str:
    return _UNSAFE.sub("_", value).strip("_") or "x"


def write_owed_legs(root: Path, run_id: str, plan_path: str, owed: list[dict]) -> list[Path]:
    """Persist each owed row `{id, reason, detail}` as `state/owed-legs/<run_id>--<id>.yaml`.

    Idempotent: a rewrite keeps the file's original `created`. Rows without a string `id` are
    skipped. Returns the written paths.
    """
    directory = Path(root) / OWED_LEGS_DIR
    written: list[Path] = []
    for row in owed:
        row_id = row.get("id")
        if not isinstance(row_id, str) or not row_id:
            continue
        path = directory / f"{_slug(run_id)}--{_slug(row_id)}.yaml"
        created = _existing_created(path) or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        record = {
            "id": row_id,
            "run_id": run_id,
            "plan": plan_path,
            "reason": str(row.get("reason") or ""),
            "detail": str(row.get("detail") or ""),
            "created": created,
        }
        directory.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            yaml.safe_dump(record, handle, sort_keys=False, allow_unicode=True)
        written.append(path)
    return written


def _load_record(path: Path) -> Optional[dict]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if isinstance(loaded, dict) and isinstance(loaded.get("id"), str) and loaded.get("id"):
        return loaded
    return None


def _existing_created(path: Path) -> Optional[str]:
    record = _load_record(path)
    created = record.get("created") if record else None
    return created if isinstance(created, str) and created else None


def _plan_state(root: Path, plan: Any) -> Optional[tuple[dict, dict]]:
    """`({row_id: disposition}, em_discharges)` for `plan`, or None when it cannot be read."""
    if not isinstance(plan, str) or not plan:
        return None
    plan_file = Path(plan)
    if not plan_file.is_absolute():
        plan_file = Path(root) / plan_file
    try:
        source = plan_file.read_text(encoding="utf-8")
    except OSError:
        return None
    from coordinator_core.ops.plan_tasks_render import load_rows

    dispositions: dict[str, str] = {}
    for raw in load_rows(source).rows:
        row_id = raw.get("id")
        disposition = raw.get("disposition")
        if isinstance(row_id, str):
            dispositions[row_id] = disposition if isinstance(disposition, str) and disposition else "open"
    from coordinator_core.ops.plan_tasks_mutate import read_em_discharges

    return dispositions, read_em_discharges(plan_file)


def open_owed_legs(root: Path) -> list[dict]:
    """The persisted owed legs not yet discharged, sorted by file name."""
    directory = Path(root) / OWED_LEGS_DIR
    try:
        files = sorted(directory.glob("*.yaml"))
    except OSError:
        return []
    states: dict[Any, Optional[tuple[dict, dict]]] = {}
    open_rows: list[dict] = []
    for path in files:
        record = _load_record(path)
        if record is None:
            continue
        plan = record.get("plan")
        if plan not in states:
            states[plan] = _plan_state(root, plan)
        state = states[plan]
        if state is not None:
            dispositions, discharges = state
            if dispositions.get(record["id"], "open") != "open" or record["id"] in discharges:
                continue
        open_rows.append(record)
    return open_rows


def render_owed_legs(rows: list[dict]) -> str:
    """One line per row: `OWED <run_id> <id> (<reason>) <plan>`."""
    return "\n".join(
        f"OWED {row.get('run_id')} {row.get('id')} ({row.get('reason')}) {row.get('plan')}" for row in rows
    )
