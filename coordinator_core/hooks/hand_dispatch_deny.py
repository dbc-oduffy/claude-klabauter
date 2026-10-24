"""coordinator_core.hooks.hand_dispatch_deny -- the main loop's hand dispatch of a workflow-owned
planner (plan-author, roadmap-planner) is refused; a sizing-scout dispatch is delegated to its leaf.

Purpose: leaf of ``hooks.block_unenumerated_agent_type.check``. A workflow ``agent()`` call never
reaches PreToolUse(Agent), so a top-level hit on a planner type is hand-rolled by construction.
Invariant: no module-scope import beyond stdlib, ``hooks._envelope`` and
``hooks.sizing_scout_deny`` (``test_fleet_mode_reader_set.py`` counts every import edge into the
guard closure). Trap: the ``/mise-prep`` carve-out token is agent-writable, so it must never be
named in the reason text.

Order is load-bearing: tool, agent_id and the type are checked before the prompt is read, so no
other Agent dispatch pays anything.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional

from coordinator_core.hooks import sizing_scout_deny as _scout
from coordinator_core.hooks._envelope import deny

GUARD_NAME = "hand-dispatch-planner"
PLAN_AUTHOR_TYPES = frozenset({"coordinator:plan-author", "plan-author"})
PLANNER_TYPES = PLAN_AUTHOR_TYPES | frozenset({"coordinator:roadmap-planner", "roadmap-planner"})
_MISE_PREP_TOKEN = re.compile(r"(?<![\w-])mise-prep-gate(?![\w-])")
_REASON = (
    "Planning starts at sizing: Skill(coordinator:sizing) fires the ask workflow, "
    "then Skill(coordinator:plan) plans what it routes."
)


def hand_dispatch_deny(payload: Mapping[str, Any], subagent_type: str) -> Optional[dict]:
    """None unless a top-level Agent dispatch of a scout or planner type.

    A scout type returns ``sizing_scout_deny.sizing_scout_deny`` (called through the module so
    monkeypatches keep reaching it). A plan-author whose prompt names ``mise-prep-gate`` passes;
    any other planner type is denied through ``apply_guard_level``.
    """
    if payload.get("tool_name") != "Agent" or payload.get("agent_id"):
        return None
    if subagent_type in _scout.SIZING_SCOUT_TYPES:
        return _scout.sizing_scout_deny(payload, subagent_type)
    if subagent_type not in PLANNER_TYPES:
        return None
    if subagent_type in PLAN_AUTHOR_TYPES:
        tool_input = payload.get("tool_input")
        prompt = tool_input.get("prompt") if isinstance(tool_input, Mapping) else None
        if isinstance(prompt, str) and _MISE_PREP_TOKEN.search(prompt):
            return None
    from coordinator_core.machine_profile import apply_guard_level

    return apply_guard_level(GUARD_NAME, deny("PreToolUse", _REASON))
