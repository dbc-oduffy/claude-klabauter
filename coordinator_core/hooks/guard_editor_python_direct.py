"""coordinator_core.hooks.guard_editor_python_direct — PreToolUse (any tool) op.

Warns the main loop when it calls an editor-Python tool, or launches editor Python from Bash,
directly; the call belongs to the route table's `editor_python.preferred_agent_type`.

Fires when `tool_name` is in `editor_python.tool_names`, or when `tool_name == "Bash"` and a
simple command of `tool_input.command` matches a compiled `editor_python.bash_patterns` regex.
The command goes through `_command_tokenizer.resolve_command_positions` (heredoc bodies and
comments dropped, wrappers peeled); each non-UNRESOLVED command is matched as its joined tokens,
never the raw string. The MCP leg returns an advisory at `warn` and a deny through
`apply_guard_level` at `strict`; the Bash leg is an advisory at every level. Subagent calls
(truthy `agent_id`) are never touched.

Fail-open: no table, no `editor_python`, no repo root, a malformed table, or any internal error
returns `{}`.

Forwarder contract. `evaluate(payload, env=None, repo_root=None) -> dict` is the one body behind
the registered op and the seam a thin external hook script forwards to. The op handler delegates
here. No subprocess, no git.
"""

from __future__ import annotations

from typing import Mapping, Optional

from coordinator_core._hook_envelope import allow_advisory, deny, no_advisory, payload_of
from coordinator_core.ipc import register_op
from coordinator_core.machine_profile import apply_guard_level

GUARD_NAME = "guard-editor-python-direct"


def _message(trigger: str, preferred: str) -> str:
    return (
        f"{trigger} is editor-Python work, which belongs to {preferred}. "
        f"Dispatch {preferred} with the outcome."
    )


def _bash_hit(command: str, editor) -> bool:
    """True when a resolved simple command (executable first) matches a bash_pattern.

    UNRESOLVED entries are skipped and the raw string is never searched.
    """
    from coordinator_core.bash_guards import _command_tokenizer as tok

    for rc in tok.resolve_command_positions(command, preserve_windows_backslashes=True):
        if rc.confidence == tok.ResolutionConfidence.UNRESOLVED:
            continue
        text = " ".join(rc.tokens)
        if any(p.search(text) for p in editor.bash_patterns):
            return True
    return False


def evaluate(
    payload: dict,
    env: "Optional[Mapping[str, str]]" = None,
    repo_root=None,
) -> dict:
    """Single-call forwarder seam — see module docstring "Forwarder contract"."""
    try:
        return _evaluate(dict(payload_of(payload)), repo_root)
    except Exception:
        return no_advisory()


def _evaluate(params: dict, repo_root) -> dict:
    if params.get("agent_id"):
        return no_advisory()
    tool_name = params.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name:
        return no_advisory()

    from coordinator_core.ops.dispatch_emit import agent_route

    root = repo_root if repo_root else agent_route.repo_root_of(params.get("cwd"))
    if not root:
        return no_advisory()
    table = agent_route.load_route_table(root)
    editor = table.editor_python if table is not None else None
    if editor is None:
        return no_advisory()

    trigger = None
    if tool_name in editor.tool_names:
        trigger = tool_name
    elif tool_name == "Bash":
        tool_input = params.get("tool_input")
        command = tool_input.get("command") if isinstance(tool_input, Mapping) else None
        if isinstance(command, str) and editor.bash_patterns and _bash_hit(command, editor):
            # Warn-only at every `level`: recall is ~78%, so a deny (which apply_guard_level
            # would keep on an author box at strict) would block on a classifier known to miss.
            return allow_advisory(
                "PreToolUse", _message("This Bash launch", editor.preferred_agent_type)
            )
    if trigger is None:
        return no_advisory()

    message = _message(trigger, editor.preferred_agent_type)
    if editor.level == "strict":
        return apply_guard_level(GUARD_NAME, deny("PreToolUse", message)) or no_advisory()
    return allow_advisory("PreToolUse", message)


@register_op("hooks.guard_editor_python_direct")
def _handler(params: dict, repo_root=None) -> dict:
    payload = payload_of(params)
    env = payload.get("env")
    return evaluate(payload, env if isinstance(env, Mapping) else {}, repo_root)
