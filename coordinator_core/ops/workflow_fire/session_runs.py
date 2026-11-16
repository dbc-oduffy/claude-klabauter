"""Open runs a session fired: fire records whose run is neither landed nor halted."""
from __future__ import annotations

from pathlib import Path

_TERMINAL_STAGES = frozenset({"landed", "halted"})


def open_runs_for_session(repo_root: Path, session_id: str) -> list[dict]:
    """Fires `session_id` submitted whose run has not reached `landed` or `halted`.

    Each dict: `{fire_id, run_id, stage, fire_state}`; `stage` is None when the run has no readable
    run-stage (an absent stage counts as open). A record with no `run_id` is open only while its
    fire has not exited. No git spawn, no sleep.

    Known limit: only headless-executor fire records carry `caller_session_id`; the claude-backend
    record does not, so this sees headless fires only -- the path every codex/agy/grok EM fire takes.
    """
    from coordinator_core.ops.dispatch_emit.ask_contract import read_run_stage
    from coordinator_core.ops.workflow_fire import fire

    if not session_id:
        return []
    open_runs: list[dict] = []
    for record in fire._list_records(fire._registry_dir(str(repo_root))):
        if record.get("caller_session_id") != session_id:
            continue
        run_id = record.get("run_id") or None
        fire_state = record.get("state")
        stage_name = None
        if run_id:
            stage = read_run_stage(Path(repo_root), run_id)
            value = stage.get("stage") if stage else None
            stage_name = value if isinstance(value, str) else None
            if stage_name in _TERMINAL_STAGES:
                continue
        elif fire_state == "exited":
            continue
        open_runs.append({"fire_id": record.get("fire_id"), "run_id": run_id,
                          "stage": stage_name, "fire_state": fire_state})
    return open_runs
