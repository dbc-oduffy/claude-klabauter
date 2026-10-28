"""coordinator_core.hooks.sizing_scout_deny -- the main loop's hand dispatch of the sizing scout is
denied unless its brief carries READONLY_MARKER: naming the live run and its stage when this session
has one, else rerouting to Skill(coordinator:sizing).

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

import json
from pathlib import Path
from typing import Any, Mapping, Optional

from coordinator_core.hooks._envelope import deny

GUARD_NAME = "submit-engine-ask"
SIZING_SCOUT_TYPES = frozenset({"coordinator:sizing-scout", "sizing-scout"})
READONLY_MARKER = "sizing-scout-mode: read-only-measurement"
_RUN_STAGE_DIRS = ("state/warp", "scratch/warp")
ARGS_SHAPE = 'args: {ask: "<words>"} | {sizing: "<path>"} | {asks: [{ask, source?}]}, brief: "<text>" | brief_file'


def _readonly_marked(payload: Mapping[str, Any]) -> bool:
    """True when the brief carries READONLY_MARKER as a whole line of its own."""
    tool_input = payload.get("tool_input")
    prompt = tool_input.get("prompt") if isinstance(tool_input, Mapping) else None
    if not isinstance(prompt, str):
        return False
    return any(line.strip() == READONLY_MARKER for line in prompt.splitlines())


def _run_stage(root: Path, run_id: str) -> Optional[str]:
    """The stage in the run's ``run-stage.json``, else None (unsafe id, absent or malformed file)."""
    if not run_id or run_id in (".", "..") or run_id != Path(run_id).name:
        return None
    for base in _RUN_STAGE_DIRS:
        try:
            data = json.loads((root / base / run_id / "run-stage.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        stage = data.get("stage") if isinstance(data, dict) else None
        if isinstance(stage, str) and stage:
            return stage
    return None


def sizing_scout_deny(payload: Mapping[str, Any], subagent_type: str) -> Optional[dict]:
    """Deny the main loop's hand dispatch of the sizing scout; a live run is named when one exists.

    A brief carrying READONLY_MARKER is admitted. ``subagent_type`` is the caller's already-resolved
    type (``resolve_subagent_type``).
    """
    if payload.get("tool_name") != "Agent" or payload.get("agent_id"):
        return None
    if subagent_type not in SIZING_SCOUT_TYPES:
        return None
    if _readonly_marked(payload):
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
        stage = _run_stage(root, live) if root is not None else None
        state = f"in stage {stage}" if stage else "live"
        reason = (
            f"Run {live} is {state}; its completion is your one wake. End the turn. "
            f"A read-only measurement brief carries the line `{READONLY_MARKER}`."
        )
    else:
        reason = "Sizing runs in the ask workflow: Skill(coordinator:sizing) fires it."
    return apply_guard_level(GUARD_NAME, deny("PreToolUse", reason))
