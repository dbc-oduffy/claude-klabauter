"""Native Codex tool policy routing, reusing shared engine guard bodies.

No Claude permission/model rewrites are applied to native tool arguments.
Unknown local tools remain neutral, rather than pretending a shell policy covers
their semantics. Hosted/specialized tools absent from PreToolUse are out of scope.
"""
from __future__ import annotations

import json
from typing import Any

from coordinator_core.host_hooks.codex_tools import patch_events


def _merge(outputs: list[Any], warnings: list[str]) -> dict[str, Any]:
    contexts = list(warnings)
    for output in outputs:
        if isinstance(output, list):
            nested = _merge(output, [])
            output = nested
        if not isinstance(output, dict):
            continue
        hso = output.get("hookSpecificOutput")
        hso = hso if isinstance(hso, dict) else output
        decision = hso.get("permissionDecision") or hso.get("decision")
        if decision == "deny":
            return {"permissionDecision": "deny", "permissionDecisionReason":
                    hso.get("permissionDecisionReason") or hso.get("reason") or "denied by coordinator guard"}
        for source, key in ((hso, "additionalContext"), (output, "systemMessage")):
            value = source.get(key)
            if isinstance(value, str) and value and value not in contexts:
                contexts.append(value)
    return {"additionalContext": "\n".join(contexts)} if contexts else {}


def evaluate(payload: dict[str, Any], *, policy_file: str | None = None,
             resolution_class: str | None = None) -> dict[str, Any]:
    """Evaluate a caller-owned payload without reading daemon identity/environment."""
    tool = payload.get("tool_name") or ""
    if tool in {"Bash", "PowerShell", "exec_command", "shell"}:
        from coordinator_core.bash_guards.dispatch import evaluate_payload_json
        return _merge([evaluate_payload_json(json.dumps(payload), policy_file=policy_file,
                                            resolution_class=resolution_class)], [])
    if tool == "apply_patch":
        from coordinator_core.write_guards.engine import evaluate as write_evaluate
        events, warnings = patch_events(payload)
        return _merge([write_evaluate(event, cwd=payload.get("cwd"), aggregate=True)
                       for event in events], warnings)
    if tool in {"Write", "Edit", "MultiEdit", "NotebookEdit"}:
        from coordinator_core.write_guards.engine import evaluate as write_evaluate
        return _merge([write_evaluate(payload, cwd=payload.get("cwd"), aggregate=True)], [])
    if tool in {"spawn_agent", "Agent"}:
        from coordinator_core.hooks.guard_brief_prescribed_method import evaluate as brief_evaluate
        args = payload.get("tool_input") or {}
        # Native message is a brief; native agent_type is a Codex profile, not
        # a Claude roster/model/mode. Only the portable brief rule is reused.
        message = args.get("message") if isinstance(args, dict) else None
        event = dict(payload, tool_name="Agent", tool_input={"prompt": message or ""})
        return _merge([brief_evaluate(event, env=payload.get("env"))], [])
    if isinstance(tool, str) and tool.startswith("mcp__"):
        from coordinator_core.bash_guards import block_perforce_submit
        record = block_perforce_submit.check_mcp(payload)
        if record:
            from coordinator_core.host_hooks.backstop import _reason
            return {"permissionDecision": "deny", "permissionDecisionReason":
                    _reason(record, "block-perforce-submit")}
        from coordinator_core.hooks.guard_editor_python_direct import evaluate as editor_evaluate
        return _merge([editor_evaluate(payload, env=payload.get("env"))], [])
    return {}
