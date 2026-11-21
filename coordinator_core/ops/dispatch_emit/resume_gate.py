"""Resume gate: the checks a refire of a halted run passes before it resumes that run, and the takeover log.

A run that is cancelled, superseded or closed is refused; a run whose driver session (run-stage.json
`session_id`) is not the caller's is refused unless the caller names a takeover reason. A run with no
recorded driver is never refused. Marker and record reads only, no spawn.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from coordinator_core.ops.dispatch_emit import halted_run
from coordinator_core.ops.dispatch_emit.ask_contract import read_run_stage
from coordinator_core.ops.dispatch_emit.halted_hold_release import supersession_reason

TAKEOVER_LOG = "takeover.jsonl"

REFUSE_CANCELLED = "resume-cancelled"
REFUSE_SUPERSEDED = "resume-superseded"
REFUSE_CLOSED = "resume-closed"
REFUSE_FOREIGN_DRIVER = "resume-foreign-driver"


class ResumeRefused(ValueError):
    """A resume the gate refuses; `reason` is one of the `REFUSE_*` codes, `str(exc)` the agent-facing text."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def takeover_reason(args: object) -> Optional[str]:
    """The non-empty `takeover.reason` text of a refire's `args`, else None."""
    take = args.get("takeover") if isinstance(args, dict) else None
    text = take.get("reason") if isinstance(take, dict) else None
    return text.strip() if isinstance(text, str) and text.strip() else None


def check_resume(root: Path, run_id: str, caller_session: Optional[str], takeover: Optional[str]) -> Optional[str]:
    """Refuse a resume of `run_id` or return the prior driver session when the caller takes the run over.

    Raises `ResumeRefused` for a cancelled, superseded or closed run, and for a driver other than
    `caller_session` when `takeover` is empty. Returns the prior driver session id only for an allowed
    takeover (the caller then calls `record_takeover`); None otherwise."""
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    rd = record_dir(Path(root), run_id)
    marker = halted_run._read(rd) or {}
    fresh = "Fire the sizing fresh (`args.fresh: true`)."
    if marker.get("status") == halted_run.STATUS_HALTED and marker.get("halted") == "cancelled":
        raise ResumeRefused(REFUSE_CANCELLED, f"Run {run_id} was cancelled. {fresh}")
    if marker.get("status") == halted_run.STATUS_CLOSED:
        raise ResumeRefused(REFUSE_CLOSED, f"Run {run_id} is closed. {fresh}")
    if marker.get("status") == halted_run.STATUS_SUPERSEDED or supersession_reason(Path(root), run_id):
        raise ResumeRefused(REFUSE_SUPERSEDED, f"Run {run_id} is superseded. {fresh}")
    driver = (read_run_stage(Path(root), run_id) or {}).get("session_id")
    if not isinstance(driver, str) or not driver or not caller_session or driver == caller_session:
        return None
    if takeover:
        return driver
    raise ResumeRefused(
        REFUSE_FOREIGN_DRIVER,
        f"Run {run_id} is driven by session {driver}. Pass `args.takeover: {{reason: \"<why>\"}}` to take it over, "
        "or fire the sizing fresh (`args.fresh: true`).",
    )


def record_takeover(rd: Path, from_session: str, to_session: str, reason: str) -> None:
    """Append `{at, from_session, to_session, reason}` to the run dir's takeover log."""
    row = {
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "from_session": from_session, "to_session": to_session, "reason": reason,
    }
    with open(Path(rd) / TAKEOVER_LOG, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def gate_resume(root: Path, run_id: str, caller_session: Optional[str], args: object, sizing_rel: Optional[str]) -> None:
    """The one gate every resume of `run_id` under its existing id passes: `check_resume`, then, on a takeover,
    the log row and a live escrowed run token for the caller. `args` is the refire's `args` or the op's params
    (`takeover.reason` read from either). Raises `ResumeRefused`; a log or token write failure never blocks."""
    reason = takeover_reason(args)
    prior = check_resume(root, run_id, caller_session, reason)
    if not (prior and reason and caller_session):
        return
    from coordinator_core.ops.dispatch_emit import run_token
    from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT, record_dir

    try:
        record_takeover(record_dir(Path(root), run_id), prior, caller_session, reason)
        if run_token.escrowed(Path(root), run_id) is None:
            token = run_token.mint(
                Path(root), run_id, f"{RUN_DIR_ROOT}/{run_id}.workflow.mjs",
                session_id=caller_session, sizing_rel=sizing_rel, carry_prior=True,
            )
            run_token.escrow(Path(root), run_id, token)
    except (OSError, ValueError):
        pass
