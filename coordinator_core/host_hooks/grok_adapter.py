"""grok PreToolUse adapter: Claude-shaped payload -> HostEvent -> engine event, verdict -> grok answer.

Stdlib only. Payload and edit-tool contract: coordinator-content-repo `coordinator/hooks/scripts/grok-session-context.py`;
shell tool name and answer shape are build defaults recorded in
`docs/research/grok-unimatrix/2026-11-13-grok-hook-surface/synthesis.md`.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from coordinator_core.host_hooks.codex_adapter import verdict_parts
from coordinator_core.host_hooks.contract import HOST_GROK, HostEvent, Verdict, neutral_fallback

#: Pinned equal to `warm.hook_http.SERVED_EVENTS` by test; not imported (hook_http is not cold-start cheap).
SERVED_EVENTS = frozenset({"PreToolUse"})

_SHELL_TOOLS = frozenset({"Bash", "bash", "shell", "run_command", "run_shell_command"})
_COMMAND_KEYS = ("command", "cmd")

#: grok edit tools -> the Claude-shaped write-guard tool names `tool_policy.evaluate` routes to `write_guards.engine`.
_EDIT_TOOLS = {
    "Edit": "Edit",
    "edit_file": "Edit",
    "MultiEdit": "MultiEdit",
    "Write": "Write",
    "write_file": "Write",
    "NotebookEdit": "NotebookEdit",
}
#: Arg keys a grok edit tool may carry its target path under; the first non-empty one becomes `file_path`.
_PATH_KEYS = ("file_path", "path", "Path")


class GrokAdapter:
    """Adapter for grok command hooks (JSON on stdin, JSON on stdout)."""

    host = HOST_GROK

    def adapt_event(self, payload: Mapping[str, Any], event: Optional[str] = None) -> HostEvent:
        raw = payload.get("tool_input")
        args = raw if isinstance(raw, Mapping) else {}
        name = str(payload.get("tool_name") or "")
        if name in _SHELL_TOOLS:
            tool_name = "Bash"
            tool_input: Dict[str, Any] = dict(args)
            tool_input["command"] = next((str(args[k]) for k in _COMMAND_KEYS if args.get(k)), "")
        elif name in _EDIT_TOOLS:
            tool_name = _EDIT_TOOLS[name]
            tool_input = dict(args)
            tool_input["file_path"] = next((str(args[k]) for k in _PATH_KEYS if args.get(k)), "")
        else:
            tool_name = name
            tool_input = dict(args)
        return HostEvent(
            host=HOST_GROK,
            event=str(event or payload.get("hook_event_name") or ""),
            session_id=str(payload.get("session_id") or ""),
            tool_name=tool_name,
            tool_input=tool_input,
            cwd=str(payload.get("cwd") or ""),
        )

    def engine_event(self, host_event: HostEvent) -> Optional[Dict[str, Any]]:
        """The Claude-shaped event `hook_http.build_request` frames, or None when the event is unserved."""
        if host_event.event not in SERVED_EVENTS:
            return None
        return {
            "host": HOST_GROK,
            "hook_event_name": host_event.event,
            "session_id": host_event.session_id or None,
            "cwd": host_event.cwd or None,
            "tool_name": host_event.tool_name,
            "tool_input": dict(host_event.tool_input),
        }

    def render(self, host_event: HostEvent, verdict: Verdict) -> Dict[str, Any]:
        """Claude-shaped `hookSpecificOutput` for deny, ask and an allow/ask carrying `additionalContext`; else `{}`."""
        if host_event.event not in SERVED_EVENTS:
            return neutral_fallback(HOST_GROK)
        decision, reason = verdict_parts(verdict)
        context = _advisory_text(verdict)
        if decision == "deny":
            return _decision(host_event, "deny", reason or "denied by coordinator guard")
        if decision in ("ask", "force_ask"):
            return _decision(host_event, "ask", reason or "coordinator: confirm", context)
        if decision == "allow" and context:
            return _decision(host_event, "allow", reason, context)
        return neutral_fallback(HOST_GROK)


def _advisory_text(verdict: Optional[Mapping[str, Any]]) -> str:
    """The `additionalContext` of a flat or `hookSpecificOutput`-nested verdict, or ""."""
    if not isinstance(verdict, Mapping):
        return ""
    nested = verdict.get("hookSpecificOutput")
    inner = nested.get("additionalContext") if isinstance(nested, Mapping) else None
    context = inner or verdict.get("additionalContext")
    return context if isinstance(context, str) else ""


def _decision(host_event: HostEvent, decision: str, reason: str, context: str = "") -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "hookEventName": host_event.event,
        "permissionDecision": decision,
    }
    if reason:
        out["permissionDecisionReason"] = reason
    if context:
        out["additionalContext"] = context
    return {"hookSpecificOutput": out}
