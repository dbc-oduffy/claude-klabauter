"""coordinator_core.hooks.sizing_scout_deny -- the main loop's hand dispatch of the sizing scout is
always denied: naming the live run when this session has one, else the Workflow ask submission.

Purpose: leaf of ``hooks.block_unenumerated_agent_type.check`` (the engine leg DoE's
PreToolUse(Agent) fan-in already runs). Invariant: this module never imports
``hooks.submit_engine_ask`` or anything under ``ops.dispatch_emit`` -- the guard modules import
``block_unenumerated_agent_type`` widely, and that import edge must not drag the dispatch emitter
(and through it ``sizing_assemble`` -> the fleet record) into every guard's closure
(``coordinator_core/tests/test_fleet_mode_reader_set.py``). Trap: the walk counts function-local
imports, so a lazy import does not cut the edge.

Order is load-bearing: tool, agent_id and the type are checked before any file is read, so no
other Agent dispatch pays a launch-record directory listing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

from coordinator_core.hooks._envelope import deny
from coordinator_core.warp_fire import SUBMIT_WORKFLOW_NAME

GUARD_NAME = "submit-engine-ask"
SIZING_SCOUT_TYPES = frozenset({"coordinator:sizing-scout", "sizing-scout"})
ARGS_SHAPE = 'args: {ask: "<words>"} | {sizing: "<path>"} | {asks: [{ask, source?}]}, brief: "<text>" | brief_file'


def sizing_scout_deny(payload: Mapping[str, Any], subagent_type: str) -> Optional[dict]:
    """Deny the main loop's hand dispatch of the sizing scout; a live run is named when one exists.

    ``subagent_type`` is the caller's already-resolved type (``resolve_subagent_type``).
    """
    if payload.get("tool_name") != "Agent" or payload.get("agent_id"):
        return None
    if subagent_type not in SIZING_SCOUT_TYPES:
        return None
    live = None
    session_id = payload.get("session_id")
    root = None
    if isinstance(session_id, str) and session_id:
        from coordinator_core.git.repo_root import show_toplevel

        cwd = payload.get("cwd")
        top = show_toplevel(cwd if isinstance(cwd, str) and cwd else None)
        root = Path(top) if top else None
    if root is not None:
        from coordinator_core import warp_live

        transcript = payload.get("transcript_path")
        live = warp_live.live_run_for_session(
            root, session_id, transcript_path=transcript if isinstance(transcript, str) else None
        )
    from coordinator_core.machine_profile import apply_guard_level

    if live:
        reason = f"Run {live} is already sizing; its completion is your one wake. End the turn."
    else:
        reason = f"Sizing runs in the ask workflow. Use Workflow({{name: \"{SUBMIT_WORKFLOW_NAME}\", {ARGS_SHAPE}}})."
    return apply_guard_level(GUARD_NAME, deny("PreToolUse", reason))

