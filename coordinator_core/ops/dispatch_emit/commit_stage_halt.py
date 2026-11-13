"""Engine-side `halted.json` for an ask run whose commit stage was cancelled or failed.

A cancelled or failed `commit` / `halt-commit` agent leg ends the run with no committed reply and no
`halted_by`, so no marker lands and the halted-close ops refuse the run as never having run. The
watcher that sees the run end lands the marker itself. Journal tail read and one atomic write.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from coordinator_core.ops.dispatch_emit import halted_run
from coordinator_core.ops.dispatch_emit import ask_contract
from coordinator_core.ops.dispatch_emit.ask_contract import HALT_REASON_MAX_CHARS
from coordinator_core.ops.dispatch_emit.halt_remedy_class import remedy_or_halt
from coordinator_core.workflow_watch import stall

HALT_COMMIT_STAGE = "commit_stage"
HALT_UNRECORDED_END = "run-ended-unrecorded"
#: The run's commit-stage agent labels: the terminal `commit` and the `halt-commit` record commit.
COMMIT_STAGE_LABELS = frozenset({"commit", "halt-commit"})
_CLOSED_STATUSES = frozenset({halted_run.STATUS_LANDED, halted_run.STATUS_CLOSED, halted_run.STATUS_SUPERSEDED})


def _committed(result: object) -> bool:
    """True when a leg's `result` (dict, or JSON text of one) reports `committed` or `already_landed`."""
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            return False
    return isinstance(result, dict) and (result.get("committed") is True or result.get("already_landed") is True)


def _reason(event: dict) -> Optional[str]:
    for field in ("error", "reason", "message"):
        value = event.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def commit_leg_outcome(journal_text: str) -> Optional[tuple[str, str, Optional[str]]]:
    """(label, state, reason) of the run's last commit-stage leg, else None.

    `state` is `committed`, `unsettled`, `failed` or `uncommitted` (a result with no committed reply).
    The watcher's own stamp line is not a leg event and is ignored.
    """
    legs: dict[str, dict] = {}
    for raw in journal_text.split("\n"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("source") == "workflow_watch":
            continue
        agent_id = event.get("agentId")
        if not isinstance(agent_id, str):
            continue
        key = event.get("key")
        ident = key if isinstance(key, str) else agent_id
        kind = event.get("type")
        if kind == "started":
            label = event.get("label")
            if label in COMMIT_STAGE_LABELS:
                legs.pop(ident, None)
                legs[ident] = {"label": label, "state": "unsettled", "reason": None}
        elif ident in legs:
            if kind == "result":
                legs[ident]["state"] = "committed" if _committed(event.get("result")) else "uncommitted"
            elif kind == "failed":
                legs[ident]["state"] = "failed"
                legs[ident]["reason"] = _reason(event)
    if not legs:
        return None
    last = next(reversed(legs.values()))
    return last["label"], last["state"], last["reason"]


def halt_run_on_commit_stage_end(
    record_dir: Path,
    journal_path: str,
    *,
    terminal_status: str,
    sizing_rel: Optional[str],
    launched_at: float,
) -> Optional[Path]:
    """Write `halted: commit_stage` into `record_dir` when the run ended with its commit leg unsettled,
    failed, or (on a killed, stopped or failed task) answered without a committed reply.

    Returns the marker path, else None. A `halted.json` written since `launched_at`, or one already
    landed, closed or superseded, is left alone. Never raises.
    """
    try:
        outcome = commit_leg_outcome(stall._tail_text(journal_path, stall.JOURNAL_TAIL_BYTES))
        if outcome is None:
            return None
        label, state, leg_reason = outcome
        if state == "committed" or (state == "uncommitted" and terminal_status == "completed"):
            return None
        marker = halted_run.read_strict(Path(record_dir))
        if marker is not None and (
            marker.get("status") in _CLOSED_STATUSES
            or (Path(record_dir) / halted_run.HALTED_FILE).stat().st_mtime >= launched_at
        ):
            return None
        reason = f"commit stage: {label} leg {state}, task {terminal_status}"
        if leg_reason:
            reason = f"{reason}: {leg_reason}"
        return remedy_or_halt(
            HALT_COMMIT_STAGE,
            lambda _n: halted_run.write_halt_marker(
                Path(record_dir), halted=HALT_COMMIT_STAGE, sizing_rel=sizing_rel, reason=reason[:HALT_REASON_MAX_CHARS],
            ),
            lambda _failures: None,
        )
    except Exception:
        return None


def halt_run_on_unrecorded_end(
    record_dir: Path, *, terminal_status: str, sizing_rel: Optional[str]
) -> Optional[Path]:
    """Write `halted: run-ended-unrecorded` into `record_dir` when the ended run left no `halted.json`.

    Two reads. An existing marker of any status is never overwritten, a run that ended `completed` or is staged `landed` is not
    halted. Returns the marker path, else None. Never raises.
    """
    try:
        record_dir = Path(record_dir)
        if terminal_status == "completed":
            return None
        if (record_dir / halted_run.HALTED_FILE).exists():
            return None
        stage = ask_contract._read_json_object(record_dir / ask_contract.RUN_STAGE_FILE)
        if isinstance(stage, dict) and stage.get("stage") == "landed":
            return None
        reason = f"run ended {terminal_status} with no halt record"
        return remedy_or_halt(
            HALT_UNRECORDED_END,
            lambda _n: halted_run.write_halt_marker(
                record_dir, halted=HALT_UNRECORDED_END, sizing_rel=sizing_rel, reason=reason[:HALT_REASON_MAX_CHARS],
            ),
            lambda _failures: None,
        )
    except Exception:
        return None
