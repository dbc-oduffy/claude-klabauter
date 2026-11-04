"""Codex PreToolUse adapter: Codex stdin payload -> HostEvent -> engine event, engine verdict -> Codex response.

Stdlib only (the shim imports it on a cold interpreter). Host shapes: coordinator-content-repo 856b1d1f46,
`docs/research/2026-11-04-doe-doctrine-codex/install-mechanics.md` § Hook map.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Tuple

from coordinator_core.host_hooks.contract import HOST_CODEX, HostEvent, Verdict, neutral_fallback

#: Pinned equal to `warm.hook_http.SERVED_EVENTS` by test; not imported (hook_http is not cold-start cheap).
SERVED_EVENTS = frozenset({"PreToolUse"})

# Codex's unified exec tool reaches the Claude-shaped bash guards as `Bash`.
_TOOL_ALIASES = {"exec_command": "Bash", "shell": "Bash"}


def verdict_parts(verdict: Optional[Mapping[str, Any]]) -> Tuple[str, str]:
    """(decision, reason) from a flat or `hookSpecificOutput`-nested verdict; ("", "") when none."""
    if not isinstance(verdict, Mapping):
        return "", ""
    merged: Dict[str, Any] = dict(verdict)
    nested = verdict.get("hookSpecificOutput")
    if isinstance(nested, Mapping):
        merged.update(nested)
    decision = merged.get("permissionDecision") or merged.get("decision") or ""
    reason = merged.get("permissionDecisionReason") or merged.get("reason") or ""
    return str(decision), str(reason)


class CodexAdapter:
    """Adapter for Codex command hooks (JSON on stdin, JSON on stdout)."""

    host = HOST_CODEX

    def adapt_event(self, payload: Mapping[str, Any], event: Optional[str] = None) -> HostEvent:
        tool_input = payload.get("tool_input")
        tool_name = str(payload.get("tool_name") or "")
        return HostEvent(
            host=HOST_CODEX,
            event=str(event or payload.get("hook_event_name") or ""),
            session_id=str(payload.get("session_id") or ""),
            tool_name=_TOOL_ALIASES.get(tool_name, tool_name),
            tool_input=dict(tool_input) if isinstance(tool_input, Mapping) else {},
            cwd=str(payload.get("cwd") or ""),
        )

    def engine_event(self, host_event: HostEvent) -> Optional[Dict[str, Any]]:
        """The Claude-shaped event `hook_http.build_request` frames, or None when the event is unserved."""
        if host_event.event not in SERVED_EVENTS:
            return None
        return {
            "hook_event_name": host_event.event,
            "session_id": host_event.session_id or None,
            "cwd": host_event.cwd or None,
            "tool_name": host_event.tool_name,
            "tool_input": dict(host_event.tool_input),
        }

    def render(self, host_event: HostEvent, verdict: Verdict) -> Dict[str, Any]:
        """Codex response: a deny is `permissionDecision: deny`; everything else is `{}` (continue)."""
        if host_event.event not in SERVED_EVENTS:
            return neutral_fallback(HOST_CODEX)
        decision, reason = verdict_parts(verdict)
        if decision == "deny":
            return {
                "hookSpecificOutput": {
                    "hookEventName": host_event.event,
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason or "denied by coordinator guard",
                }
            }
        return neutral_fallback(HOST_CODEX)
