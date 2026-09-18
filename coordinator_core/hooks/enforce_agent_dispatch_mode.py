"""coordinator_core.hooks.enforce_agent_dispatch_mode — PreToolUse(Agent) op:
the sole `updatedInput` emitter on the Agent matcher.

Arrival note (W4-C8, docs/plans/2026-09-18-doe-holds-no-scripts.md): ported
from DoE-claude `coordinator/hooks/scripts/enforce-agent-dispatch-mode.py`.
That script's own docstring records catering (sidecar provisioning,
contract-block injection, role framing) as RETIRED from this leg
(2026-08-21) onto `SubagentStart`; what remains is PreToolUse-shaped:

    Concern I — teammate-name path-segment refusal (checked first; wins
        outright over every other concern).
    Concern A — mode elevation: raise a dispatched child's permission mode
        to match an autonomous parent's, never lower. Absent child mode is
        treated as `acceptEdits`. Gated by the `COORDINATOR_AGENT_MODE_OK`
        env escape hatch — that hatch short-circuits Concern A's
        computation ONLY, never Concerns E/F/G below.
    Concern E — worktree-isolation strip
        (`support.worktree_isolation_strip.compute_strip`).
    Concern F — named-dispatch (`name` key) strip
        (`support.named_dispatch_strip.compute_named_dispatch_result`) — a
        "deny" result wins outright over every later concern.
    Concern G — foreground-dispatch reroute
        (`support.foreground_dispatch_strip.compute_foreground_reroute`) —
        a "deny" result wins outright over every later concern, same
        precedence tier as Concern F's own.
    Concern H — plan-path record (`support.plan_path_bridge`), a pure side
        effect outside the emit-gate: never reaches `updatedInput` or the
        decision, so it neither joins nor widens it.

Single-emitter invariant, UNCHANGED FROM SOURCE: exactly one envelope is
ever built and returned — a "deny" (Concern I's, F's, or G's own
fail-closed leg) and an "allow"+`updatedInput` (every other concern,
merged onto ONE `tool_input` copy) are mutually exclusive outcomes of the
SAME decision, never two independent emission sites. Precedence, in order:
Concern I deny > Concern F deny > Concern G deny > combined emit-gate
(mode-elevation-needed OR worktree-stripped OR named-stripped OR
foreground-rerouted) > silent pass.

Op contract: `params` is the flat PreToolUse payload dict (`tool_input`,
`permission_mode`, `session_id`, `cwd`, …). Returns `deny(...)`,
`rewrite_input(...)`, or `no_advisory()` — never a bare "allow" without
`updatedInput` (this op has nothing to say when no concern fires).

Fail-open discipline, UNCHANGED FROM SOURCE: every concern below degrades
to "nothing to add" on any internal failure — it never blocks or denies the
spawn on its own account. Concerns F and G's own fail-CLOSED legs are the
sole exceptions, and those are the guard's own genuine decision, not a
computation failure.

Negative-spec:
    Does NOT provision a run-report sidecar, inject `contract_blocks`, or
    apply role framing — that whole family is `hooks.cater_subagent_start`'s
    job now (SubagentStart, not this event).
    Does NOT re-derive any of the four `support.*` computations inline —
    each is called verbatim from its own already-landed module (W4-C3/W4-C4
    arrivals), the same modules `guard_named_dispatch_tool_restriction`
    (this row's own sibling op) and `nudge_foreground_agent_dispatch`
    (already landed) call for their own standalone doors.
    Does NOT re-open the single-emitter fold — a fifth concern, or a second
    `updatedInput` emission site, is out of scope for this port.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C8
"""

from __future__ import annotations

import os
import re
from typing import Any, Optional

from coordinator_core.hooks._envelope import deny, no_advisory, rewrite_input
from coordinator_core.hooks.support.foreground_dispatch_strip import (
    compute_foreground_reroute,
)
from coordinator_core.hooks.support.message_envelope import compose, render
from coordinator_core.hooks.support.named_dispatch_strip import (
    compute_named_dispatch_result,
)
from coordinator_core.hooks.support.plan_path_bridge import (
    extract_plan_path,
    record_plan_path,
)
from coordinator_core.hooks.support.worktree_isolation_strip import compute_strip
from coordinator_core.ipc import register_op

# --- Concern I: teammate-name path-segment refusal. See module docstring. ---
_TEAMMATE_NAME_PATH_UNSAFE_RE = re.compile(r"[\\/]")

# --- Autonomy rank table (least -> most) ---
# plan=0 < default=manual=1 < acceptEdits=2 < auto=3 < dontAsk=4 < bypassPermissions=5
_MODE_RANK = {
    "plan": 0,
    "default": 1,
    "manual": 1,
    "acceptEdits": 2,
    "auto": 3,
    "dontAsk": 4,
    "bypassPermissions": 5,
}


def _mode_rank(mode: str) -> int:
    return _MODE_RANK.get(mode, -1)


def _teammate_name_deny_message(name: str) -> Optional[str]:
    match = _TEAMMATE_NAME_PATH_UNSAFE_RE.search(name)
    if not match:
        return None
    offending_char = match.group(0)
    prose = (
        "[named-dispatch guard] denied: `name` contains {char!r}, illegal "
        "in a path segment -- it becomes the teammate's canonical id and "
        "sidecar path. Retry using only letters, digits, `.`, `_`, `@`, or "
        "`-` (e.g. \"feature-auth-review\")."
    ).format(char=offending_char)
    return render(compose(prose))


@register_op("hooks.enforce_agent_dispatch_mode")
async def _handler(params: dict, repo_root=None) -> dict:
    """PreToolUse(Agent) op: the sole `updatedInput` emitter for mode
    elevation, worktree-isolation stripping, named-dispatch stripping, and
    foreground-dispatch rerouting. See module docstring for concern order.
    """
    data: Any = params if isinstance(params, dict) else {}

    tool_input = data.get("tool_input")
    tool_input_dict = tool_input if isinstance(tool_input, dict) else {}

    # Concern I — computed unconditionally, before anything else; wins
    # outright over every other concern.
    teammate_name_deny_message: Optional[str] = None
    _name_value = tool_input_dict.get("name")
    if isinstance(_name_value, str):
        try:
            teammate_name_deny_message = _teammate_name_deny_message(_name_value)
        except Exception:
            teammate_name_deny_message = None

    # Escape hatch: short-circuits Concern A's computation ONLY.
    mode_ok_escape = bool(os.environ.get("COORDINATOR_AGENT_MODE_OK"))

    parent_mode = data.get("permission_mode") or ""
    child_mode = tool_input_dict.get("mode") or ""

    need_mode_elevation = False
    if not mode_ok_escape and parent_mode:
        parent_rank = _mode_rank(parent_mode)
        child_effective = child_mode or "acceptEdits"
        child_rank = _mode_rank(child_effective)
        if parent_rank >= 0 and child_rank >= 0 and parent_rank >= 3 and child_rank < parent_rank:
            need_mode_elevation = True

    # Concern E — computed unconditionally; never gated by the mode escape
    # hatch.
    try:
        worktree_strip_result = compute_strip(tool_input_dict)
    except Exception:
        worktree_strip_result = None

    # Concern F — computed unconditionally; a "deny" result wins outright.
    try:
        named_dispatch_result = compute_named_dispatch_result(tool_input_dict)
    except Exception:
        named_dispatch_result = None

    # Concern G — computed unconditionally; a "deny" result wins outright,
    # same precedence tier as Concern F's own.
    try:
        foreground_result = compute_foreground_reroute(
            tool_input_dict.get("run_in_background"),
            data.get("session_id"),
            tool_input_dict,
            data.get("cwd"),
        )
    except Exception:
        foreground_result = None

    # Concern H — pure side effect, deliberately outside the emit-gate.
    try:
        record_plan_path(
            str(data.get("session_id") or ""),
            str(tool_input_dict.get("subagent_type") or ""),
            extract_plan_path(str(tool_input_dict.get("prompt") or "")) or "",
            data.get("cwd"),
        )
    except Exception:
        pass

    if teammate_name_deny_message is not None:
        return deny("PreToolUse", teammate_name_deny_message)

    if named_dispatch_result is not None and named_dispatch_result[0] == "deny":
        _, _, deny_message = named_dispatch_result
        return deny("PreToolUse", deny_message)

    if foreground_result is not None and foreground_result[0] == "deny":
        _, _, deny_message = foreground_result
        return deny("PreToolUse", deny_message)

    if not (
        need_mode_elevation
        or worktree_strip_result is not None
        or named_dispatch_result is not None
        or foreground_result is not None
    ):
        return no_advisory()

    if not isinstance(tool_input, dict):
        return no_advisory()

    merged = dict(tool_input)
    if need_mode_elevation:
        merged["mode"] = parent_mode

    additional_context_parts: list[str] = []
    if worktree_strip_result is not None:
        _, worktree_note = worktree_strip_result
        merged.pop("isolation", None)
        additional_context_parts.append(worktree_note)
    if named_dispatch_result is not None:
        _, _, name_offer = named_dispatch_result
        merged.pop("name", None)
        additional_context_parts.append(name_offer)
    if foreground_result is not None and foreground_result[0] == "reroute":
        merged["run_in_background"] = foreground_result[1]
        additional_context_parts.append(foreground_result[2])

    context = "\n\n".join(additional_context_parts) if additional_context_parts else ""
    return rewrite_input("PreToolUse", merged, context)
