"""coordinator_core.hooks.guard_agent_route_admission -- PreToolUse (Agent) op.

Denies an Agent dispatch whose resolved `subagent_type` lacks the `required_tools` of the work
class the brief matches under the repo's `.claude/route-table.json`, naming the class's
`agent_type`. The deny is a refusal, never an `updatedInput` rewrite: `enforce_agent_dispatch_mode`
is the sole updatedInput emitter on the Agent matcher. A class's `triage_agent_type` is admitted
without a range check: the triage hop assesses and need not hold the tools.

Forwarder contract. `evaluate(payload, env=None, repo_root=None) -> dict` is the one body behind the
registered op and the seam a thin external hook script forwards to. It returns `{}` (admit) or the
PreToolUse deny envelope after `apply_guard_level`. No table, a non-Agent tool, a malformed table
and any internal error all fail open to `{}`. No subprocess.
"""

from __future__ import annotations

import re
from typing import Mapping, Optional

from coordinator_core._hook_envelope import deny, no_advisory, payload_of
from coordinator_core.ipc import register_op
from coordinator_core.machine_profile import apply_guard_level

GUARD_NAME = "guard-agent-route-admission"
ROUTE_TABLE_RELPATH = ".claude/route-table.json"

_TOKEN_SPLIT_RE = re.compile(r"""[\s'"`<>()\[\]{},;]+""")


def _path_tokens(prompt: str) -> list[str]:
    out = []
    for tok in _TOKEN_SPLIT_RE.split(prompt):
        tok = tok.strip(":.")
        if tok and ("/" in tok or "\\" in tok or "." in tok):
            out.append(tok)
    return out


def _reason(subagent_type: str, cls, redirect: bool) -> str:
    """The route-table names the class's agent and tools; the message points there, never echoes them."""
    tail = "Dispatch its agent_type instead." if redirect else "Dispatch its agent_type."
    return (
        f"{subagent_type} lacks the required_tools of work class {cls.id} "
        f"({ROUTE_TABLE_RELPATH}). {tail}"
    )


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
    from coordinator_core.ops.dispatch_emit import agent_route

    root = repo_root if repo_root else agent_route.repo_root_of(params.get("cwd"))
    if root is None:
        return no_advisory()
    table = agent_route.load_route_table(root)
    if table is None:
        return no_advisory()

    cls = agent_route.match_work_class(
        table, writes=_path_tokens(prompt), title="", body=prompt
    )
    if cls is None or not cls.required_tools:
        return no_advisory()

    subagent_type = resolve_subagent_type(dict(tool_input))
    if subagent_type == cls.triage_agent_type:
        return no_advisory()
    verdict = agent_route.admit(subagent_type, cls, table)
    if verdict.verdict == "admit":
        return no_advisory()

    reason = _reason(subagent_type, cls, verdict.verdict == "redirect")
    return apply_guard_level(GUARD_NAME, deny("PreToolUse", reason)) or no_advisory()


@register_op("hooks.guard_agent_route_admission")
def _handler(params: dict, repo_root=None) -> dict:
    payload = payload_of(params)
    env = payload.get("env")
    return evaluate(payload, env if isinstance(env, Mapping) else {}, repo_root)
