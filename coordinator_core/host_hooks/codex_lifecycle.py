"""Native lifecycle/advisory ports; no Claude transcript format is assumed.

Turn end never unregisters a thread. Compaction notices use the documented UI
channel; only SessionStart(source=compact) restores model-visible role context.
"""
from __future__ import annotations

from coordinator_core.session.agent_role import role_for_caller

EVENTS = frozenset({"UserPromptSubmit", "PostToolUse", "Stop", "SubagentStop", "PreCompact", "PostCompact"})


def _notice(text):
    return {"systemMessage":text} if text else {}


def handle(payload, env, *, plugin_root=None):
    event = payload.get("hook_event_name")
    if event not in EVENTS:
        return {}
    if event in ("Stop", "SubagentStop", "PostCompact"):
        try:
            from coordinator_core.codex_status.hook import handle as refresh_status
            refresh_status(payload, event)
        except Exception:
            pass  # Status is advisory, never a reason to lose the other hook output.
    role = "worker" if payload.get("agent_id") else role_for_caller(env, payload.get("session_id"))
    if event == "Stop":
        if role != "em" or payload.get("stop_hook_active"):
            return {}
        text = payload.get("last_assistant_message")
        if not isinstance(text, str) or not text.strip():
            return {}
        from coordinator_core.hooks.guard_turn_intent import _handler
        # Native text is stable hook input; Claude transcript parsing is not.
        safe = {key:payload[key] for key in ("session_id", "cwd", "last_assistant_message") if key in payload}
        verdict = _handler(safe)
        nested = verdict.get("hookSpecificOutput", {}) if isinstance(verdict, dict) else {}
        reason = nested.get("permissionDecisionReason") or verdict.get("reason")
        if (nested.get("permissionDecision") == "deny" or verdict.get("decision") == "block") and reason:
            return {"decision":"block", "reason":reason}
        return _notice(verdict.get("systemMessage") or nested.get("additionalContext", ""))
    if event in ("PreCompact", "PostCompact"):
        from coordinator_core.hooks.context_pressure_precompact import _STEERING_RULES
        return _notice(_STEERING_RULES if event == "PreCompact" else
                       "Compaction completed. Role and Unreal retrieval context is restored by the native SessionStart compact hook. " + _STEERING_RULES)
    if event in ("UserPromptSubmit", "PostToolUse"):
        from coordinator_core.host_hooks.session_context import load_adapter
        adapter = load_adapter(plugin_root)
        text = adapter.event_advisory(payload, role)
        if not text:
            return {}
        return {"hookSpecificOutput":{"hookEventName":event, "additionalContext":text}}
    return {}
