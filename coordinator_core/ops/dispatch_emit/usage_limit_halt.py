"""Engine-side `halted.json` for an ask run whose legs died on the account usage limit.

The `halt-commit` agent leg cannot run once the limit hits, so the engine writes the marker
itself. Detection ignores whether the reset window is still live: a refire normally arrives after
the reset, and that is exactly when the claim must release. Journal and transcript tails only.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from coordinator_core.ops.dispatch_emit import halted_run
from coordinator_core.ops.dispatch_emit.halt_refire import (
    HALT_USAGE_LIMIT,
    OP_WORKFLOW_RESUME,
    is_refire_next_action,
    refire_next_action,
)
from coordinator_core.quota_limits import is_rate_limit_error, rejected_resets_at
from coordinator_core.workflow_watch import stall


def _first_limit_leg(journal_path: str, *, require_all: bool = False) -> Optional[stall.UsageLimitHalt]:
    """The first unsettled leg whose transcript ends in a 429 rate_limit turn, reset past or not.

    With `require_all`, None unless every unsettled leg ends on a 429: a leg still working keeps the run live.
    """
    run_dir = os.path.dirname(journal_path)
    run_id = os.path.basename(run_dir)
    first: Optional[stall.UsageLimitHalt] = None
    for agent_id, label in stall._unsettled(stall._tail_text(journal_path, stall.JOURNAL_TAIL_BYTES)):
        hit = None
        path = stall._transcript_path(run_dir, agent_id)
        if path is not None:
            lines = [ln for ln in stall._tail_text(path, stall.TRANSCRIPT_TAIL_BYTES).split("\n") if ln.strip()]
            resets_at = rejected_resets_at(lines[-1]) if lines and is_rate_limit_error(lines[-1]) else None
            if resets_at is not None:
                hit = stall.UsageLimitHalt(run_id, agent_id, label, resets_at)
        if hit is None:
            if require_all:
                return None
            continue
        first = first or hit
    return first


def _next_action(hit: stall.UsageLimitHalt, sizing_rel: Optional[str], script_path: Optional[str]) -> Optional[dict]:
    if not sizing_rel:
        return None
    call = hit.resume_instruction(script_path if script_path else "<scriptPath>")
    action = refire_next_action(
        call=call, run_id=hit.run_id, sizing=sizing_rel, halted=HALT_USAGE_LIMIT,
        op=OP_WORKFLOW_RESUME, input={"kind": "wait"},
    )
    return action if is_refire_next_action(action) else None


def halt_run_on_usage_limit(
    record_dir: Path,
    journal_path: str,
    *,
    sizing_rel: Optional[str],
    script_path: Optional[str],
    now: float,
    require_all_legs: bool = False,
) -> Optional[Path]:
    """Write `halted: usage_limit` into `record_dir` when an unsettled leg of the run ends on a 429.

    Returns the marker path, or None when no leg qualifies or any marker (any status, or an
    unreadable one) already exists; an existing marker is never rewritten. `now` is unused by
    detection and kept for the callers' shared signature. `require_all_legs` halts only when every
    unsettled leg ended on the limit (the liveness read); the watcher, which saw a live limit, leaves it off.
    """
    del now
    try:
        if halted_run.read_strict(Path(record_dir)) is not None:
            return None
    except ValueError:
        return None
    hit = _first_limit_leg(journal_path, require_all=require_all_legs)
    if hit is None:
        return None
    return halted_run.write_halt_marker(
        Path(record_dir),
        halted=HALT_USAGE_LIMIT,
        sizing_rel=sizing_rel,
        reason=f"{hit.agent_label} {hit.resets_at_iso}",
        next_action=_next_action(hit, sizing_rel, script_path),
    )
