"""coordinator_core.hooks.guard_brief_prescribed_method -- PreToolUse (Agent, Workflow) op.

Lints a dispatch brief against the consumer repo's `brief_lint.prescribed_method_patterns`
(`.claude/route-table.json`, read through `agent_route.load_route_table`): a brief that prescribes
the method ("run ue_build_job.py") takes the choice of method from the routed agent, which is the
agent that assesses the work. The brief is `tool_input.prompt` for Agent and `tool_input.script`
for Workflow; a `scriptPath` is the engine's emitted script and is not linted.

Forwarder contract. `evaluate(payload, env=None, repo_root=None) -> dict` is the one body behind the
registered op and the seam a thin external hook script forwards to. Table level `warn` returns an
allow + additionalContext advisory; `strict` returns a deny resolved through `apply_guard_level`
(guard name `guard-brief-prescribed-method`). No table, no `brief_lint`, no match, or any internal
error returns `{}`. No subprocess.
"""

from __future__ import annotations

from typing import Mapping, Optional

from coordinator_core._hook_envelope import allow_advisory, deny, no_advisory, payload_of
from coordinator_core.ipc import register_op
from coordinator_core.machine_profile import apply_guard_level

GUARD_NAME = "guard-brief-prescribed-method"

_SPAN_MAX = 80


def _brief_of(tool_name: object, tool_input: Mapping) -> Optional[str]:
    key = {"Agent": "prompt", "Workflow": "script"}.get(tool_name)  # type: ignore[arg-type]
    if key is None:
        return None
    if key == "script" and tool_input.get("scriptPath"):
        return None
    brief = tool_input.get(key)
    return brief if isinstance(brief, str) and brief.strip() else None


def _message(span: str) -> str:
    span = " ".join(span.split())
    if len(span) > _SPAN_MAX:
        span = span[: _SPAN_MAX - 3] + "..."
    return (
        f"Brief prescribes a method: '{span}'.\n\n"
        "State the outcome; the routed agent chooses the method."
    )


def evaluate(
    payload: dict,
    env: "Optional[Mapping[str, str]]" = None,
    repo_root=None,
) -> dict:
    """Single-call forwarder seam -- see module docstring "Forwarder contract"."""
    try:
        return _evaluate(dict(payload_of(payload)), repo_root)
    except Exception:
        return no_advisory()


def _evaluate(params: dict, repo_root) -> dict:
    tool_input = params.get("tool_input")
    if not isinstance(tool_input, Mapping):
        return no_advisory()
    brief = _brief_of(params.get("tool_name"), tool_input)
    if brief is None:
        return no_advisory()

    from coordinator_core.ops.dispatch_emit import agent_route

    root = repo_root if repo_root else agent_route.repo_root_of(params.get("cwd"))
    if not root:
        return no_advisory()
    table = agent_route.load_route_table(root)
    if table is None or table.brief_lint is None:
        return no_advisory()

    for pattern in table.brief_lint.patterns:
        found = pattern.search(brief)
        if found is None:
            continue
        text = _message(found.group(0))
        if table.brief_lint.level == "strict":
            return apply_guard_level(GUARD_NAME, deny("PreToolUse", text)) or no_advisory()
        return allow_advisory("PreToolUse", text)
    return no_advisory()


@register_op("hooks.guard_brief_prescribed_method")
def _handler(params: dict, repo_root=None) -> dict:
    payload = payload_of(params)
    env = payload.get("env")
    return evaluate(payload, env if isinstance(env, Mapping) else {}, repo_root)
