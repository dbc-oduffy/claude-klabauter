"""coordinator_core.hooks.guard_ue_specialist_advisory -- PreToolUse (Agent) op.

Advisory-only: a direct Agent dispatch of a non-example-game-repo `subagent_type` whose brief matches a work
class of the installed example-game-repo-control route table, in a UE repo, gets a context line naming
`route to example-game-repo-control:<specialist>`. It never denies and never rewrites `updatedInput`.

Forwarder contract. `evaluate(payload, env=None, repo_root=None) -> dict` is the one body behind the
registered op and the seam a thin external hook script forwards to. It returns `{}` (no advisory) or
a PreToolUse `additionalContext` envelope. A non-Agent tool, a non-UE repo, no installed example-game-repo
manifest, a example-game-repo/game-dev `subagent_type`, a specialist missing from the installed roster and
any internal error all return `{}`. No subprocess; the table and roster come from agent_route's
memoised `_plugin_table`.
"""

from __future__ import annotations

from typing import Mapping, Optional

from coordinator_core._hook_envelope import context_only, no_advisory, payload_of
from coordinator_core.hooks.guard_agent_route_admission import _path_tokens
from coordinator_core.ipc import register_op

EXEMPT_PREFIXES = ("example-game-repo-control:", "game-dev:")


def _message(subagent_type: str, class_id: str, specialist: str) -> str:
    return f"UE work class {class_id} sent to {subagent_type}: route to {specialist}."


def evaluate(
    payload: dict,
    env: "Optional[Mapping[str, str]]" = None,
    repo_root=None,
) -> dict:
    """Single-call forwarder seam -- see module docstring."""
    try:
        return _evaluate(dict(payload_of(payload)), repo_root)
    except Exception:
        return no_advisory()


def _evaluate(params: dict, repo_root) -> dict:
    if params.get("tool_name") != "Agent":
        return no_advisory()
    tool_input = params.get("tool_input")
    if not isinstance(tool_input, Mapping):
        return no_advisory()
    prompt = tool_input.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        return no_advisory()

    from coordinator_core.hooks.block_unenumerated_agent_type import resolve_subagent_type

    subagent_type = resolve_subagent_type(dict(tool_input))
    if subagent_type.startswith(EXEMPT_PREFIXES):
        return no_advisory()

    from coordinator_core.ops.dispatch_emit import agent_route

    root = repo_root if repo_root else agent_route.repo_root_of(params.get("cwd"))
    if root is None:
        return no_advisory()
    table, roster, _note = agent_route._plugin_table(root)
    if table is None or not roster:
        return no_advisory()

    cls = agent_route.match_work_class(
        table, writes=_path_tokens(prompt), title="", body=prompt
    )
    if cls is None or cls.agent_type not in roster:
        return no_advisory()
    return context_only("PreToolUse", _message(subagent_type, cls.id, cls.agent_type))


@register_op("hooks.guard_ue_specialist_advisory")
def _handler(params: dict, repo_root=None) -> dict:
    payload = payload_of(params)
    env = payload.get("env")
    return evaluate(payload, env if isinstance(env, Mapping) else {}, repo_root)
