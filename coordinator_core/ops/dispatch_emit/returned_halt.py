"""Engine-side `halted.json` for a finished ask run whose script returned `halted_by` unrecorded.

The script's halt-record commit is token-gated and skipped on some exits, so the run finishes with
`halted_by` returned and the record dir still at stage `minted`. The watcher that sees the finished
run lands the marker itself, as `usage_limit_halt` does. File reads and one atomic write; no spawn.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from coordinator_core.ops.dispatch_emit import halted_run
from coordinator_core.ops.dispatch_emit.ask_contract import HALT_BLOCKING_FIELD, HALT_BLOCKING_ITEMS_FIELD, HALT_PLAN_FIELD


def _str(value: object) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _persist_owed(record_dir: Path, halted_by: dict) -> None:
    """Persist `record_dir/manifest.json` `owed[]` through `write_owed_legs` (idempotent; absent or
    unreadable manifest and a write failure are tolerated). Runs before the marker gates so a halt
    already recorded by the script still leaves its owed legs."""
    try:
        manifest = json.loads((record_dir / "manifest.json").read_text(encoding="utf-8"))
        owed = [row for row in manifest.get("owed") or [] if isinstance(row, dict)]
        if not owed:
            return
        from coordinator_core.ops.dispatch_emit.owed_leg_queue import write_owed_legs

        plan = _str(halted_by.get(HALT_PLAN_FIELD)) or _str(manifest.get("plan")) or ""
        write_owed_legs(record_dir.parents[2], record_dir.name, plan, owed)
    except Exception:
        return


def land_returned_halt(record_dir: Path, result: dict, *, launched_at: float) -> Optional[Path]:
    """Write `halted.json` via `halted_run.write_halt_marker` when `result` carries a `halted_by` dict,
    `halt_commit` is not a committed reply, and the record dir's `halted.json` is absent or older than
    `launched_at`; return the marker path, else None. Never raises."""
    try:
        if not isinstance(result, dict):
            return None
        halted_by = result.get("halted_by")
        halted = _str(halted_by.get("halted")) if isinstance(halted_by, dict) else None
        if halted is None:
            return None
        _persist_owed(Path(record_dir), halted_by)
        commit = result.get("halt_commit")
        if isinstance(commit, dict) and commit.get("committed") is True:
            return None
        marker = Path(record_dir) / halted_run.HALTED_FILE
        if marker.exists() and marker.stat().st_mtime >= launched_at:
            return None
        blocking = halted_by.get(HALT_BLOCKING_FIELD)
        items = halted_by.get(HALT_BLOCKING_ITEMS_FIELD)
        return halted_run.write_halt_marker(
            Path(record_dir),
            halted=halted,
            sizing_rel=_str(result.get("sizing")) or _str(halted_by.get("sizing")),
            plan_rel=_str(halted_by.get(HALT_PLAN_FIELD)),
            blocking=tuple(blocking) if isinstance(blocking, list) else None,
            blocking_items=items if isinstance(items, list) else None,
            run_base_sha=_str(halted_by.get(halted_run.HALT_RUN_BASE_FIELD)),
            reason=_str(halted_by.get("reason")),
            next_action=halted_by.get("next_action") if isinstance(halted_by.get("next_action"), dict) else None,
        )
    except Exception:
        return None
