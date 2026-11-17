"""agy PreToolUse adapter: camelCase protojson payload -> HostEvent -> engine event, verdict -> agy decision.

Stdlib only. Host shapes: coordinator-content-repo 856b1d1f46, `docs/research/2026-11-04-peer-hook-surfaces/agy.md`.
agy fails closed on `{}`, so every non-deny answer is an explicit decision.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from coordinator_core.host_hooks.codex_adapter import ASK_DECISIONS, ask_denies, verdict_parts
from coordinator_core.host_hooks.contract import HOST_AGY, HostEvent, Verdict, neutral_fallback

#: Pinned equal to `warm.hook_http.SERVED_EVENTS` by test; not imported (hook_http is not cold-start cheap).
SERVED_EVENTS = frozenset({"PreToolUse"})

# agy's shell tool reaches the Claude-shaped bash guards as `Bash`; its `CommandLine` arg is the command.
_SHELL_TOOL = "run_command"

#: agy edit tools -> the Claude-shaped write-guard tool names `tool_policy.evaluate` routes to `write_guards.engine`.
_EDIT_TOOLS = {
    "write_file": "Write",
    "write_to_file": "Write",
    "edit_file": "Edit",
    "replace_file_content": "Edit",
    "sed_file": "Edit",
    "multi_replace_file_content": "MultiEdit",
    "notebook_edit": "NotebookEdit",
}
#: Lowercase arg keys an agy edit tool may carry its target path under (matched case-insensitively, so `Path`
#: from 1.5.9 write_file/edit_file counts); the first non-empty one becomes `file_path`.
_PATH_KEYS = ("targetfile", "absolutepath", "filepath", "file_path", "path")
#: Payload keys naming the calling subagent (protojson `agentId`, or snake_case); the engine event carries it
#: as `agent_id`, which the shim's cold chain and the guards read as "subagent". An EM payload carries neither.
_AGENT_ID_KEYS = ("agentId", "agent_id")


class AgyAdapter:
    """Adapter for agy command hooks (JSON on stdin, JSON on stdout)."""

    host = HOST_AGY

    def adapt_event(self, payload: Mapping[str, Any], event: Optional[str] = None) -> HostEvent:
        """`event` is the registered event when the shim knows it; agy payloads carry no event name,
        so absent that, a payload with a `toolCall` and no `error` is a PreToolUse."""
        call = payload.get("toolCall")
        call = call if isinstance(call, Mapping) else {}
        args = call.get("args")
        args = args if isinstance(args, Mapping) else {}
        name = str(call.get("name") or "")
        if name == _SHELL_TOOL:
            tool_name = "Bash"
            tool_input: Dict[str, Any] = {"command": str(args.get("CommandLine") or "")}
        elif name in _EDIT_TOOLS:
            tool_name = _EDIT_TOOLS[name]
            tool_input = dict(args)
            lowered = {str(k).lower(): v for k, v in args.items()}
            tool_input["file_path"] = next((str(lowered[k]) for k in _PATH_KEYS if lowered.get(k)), "")
        else:
            tool_name = name
            tool_input = dict(args)
        resolved = event or payload.get("hookEventName") or payload.get("hook_event_name")
        if not resolved and call and "error" not in payload:
            resolved = "PreToolUse"
        workspaces = payload.get("workspacePaths")
        cwd = str(workspaces[0]) if isinstance(workspaces, (list, tuple)) and workspaces else ""
        if not cwd and isinstance(payload.get("cwd"), str):
            cwd = payload["cwd"]
        agent_id = next((payload[k] for k in _AGENT_ID_KEYS if isinstance(payload.get(k), str) and payload[k]), "")
        return HostEvent(
            host=HOST_AGY,
            event=str(resolved or ""),
            session_id=str(payload.get("conversationId") or ""),
            tool_name=tool_name,
            tool_input=tool_input,
            cwd=cwd,
            metadata={"agent_id": agent_id} if agent_id else {},
        )

    def engine_event(self, host_event: HostEvent) -> Optional[Dict[str, Any]]:
        """The Claude-shaped event `hook_http.build_request` frames, or None when the event is unserved."""
        if host_event.event not in SERVED_EVENTS:
            return None
        return {
            **dict(host_event.metadata),
            "hook_event_name": host_event.event,
            "session_id": host_event.session_id or None,
            "cwd": host_event.cwd or None,
            "tool_name": host_event.tool_name,
            "tool_input": dict(host_event.tool_input),
        }

    def render(self, host_event: HostEvent, verdict: Verdict) -> Dict[str, Any]:
        """agy response: deny; an ask or advisory as an allow whose text is stashed for the next PreInvocation; unserved events get the `ask` fallback."""
        if host_event.event not in SERVED_EVENTS:
            return neutral_fallback(HOST_AGY)
        decision, reason = verdict_parts(verdict)
        if decision == "deny":
            return {"decision": "deny", "reason": reason or "denied by coordinator guard"}
        if decision in ASK_DECISIONS:
            if ask_denies(verdict):
                return {"decision": "deny", "reason": reason or "denied by coordinator guard"}
            return _model_visible(host_event.session_id, reason or "coordinator: confirm")
        advisory = _advisory_text(verdict)
        if advisory:
            return _model_visible(host_event.session_id, advisory)
        return {"decision": "allow"}


_CONV_RE = re.compile(r"^[A-Za-z0-9_-]+$")
#: Undrained advisories kept per conversation; past it the stash refuses so a missing PreInvocation drain cannot grow disk without bound.
_STASH_CAP = 64


def advisory_dir() -> Path:
    """Where PreToolUse advisories wait for the next PreInvocation; `unimatrix-agy-hook-pretool.py` drains the same path."""
    env = os.environ.get("COORDINATOR_SETTINGS_HOME")
    return (Path(env) if env else Path.home() / ".coordinator-claude-settings") / "unimatrix" / "agy-advisories"


def stash_advisory(conversation: str, text: str) -> bool:
    """Queue `text` for the conversation's next PreInvocation `injectSteps`; False when it cannot be queued."""
    if not _CONV_RE.match(conversation or "") or not text:
        return False
    try:
        folder = advisory_dir() / conversation
        folder.mkdir(parents=True, exist_ok=True)
        if sum(1 for _ in folder.iterdir()) >= _STASH_CAP:
            return False
        (folder / f"{time.time_ns()}-{os.getpid()}.txt").write_text(text, encoding="utf-8")
    except OSError:
        return False
    return True


def _model_visible(conversation: str, text: str) -> Dict[str, Any]:
    """An explicit allow whose advisory reaches the model at the next PreInvocation.

    agy documents no `injectSteps` on a PreToolUse answer (hooks.md section 1), and renders `ask` as a
    prompt that `-p` auto-approves, so the text is stashed and a PreInvocation hook injects it. `reason`
    repeats the text so the shim's delivery stamp (`mark_if_delivered`, which walks mappings only) sees the nag.
    """
    stash_advisory(conversation, text)
    return {"decision": "allow", "reason": text}


def _advisory_text(verdict: Optional[Mapping[str, Any]]) -> str:
    """The `additionalContext` of a flat or `hookSpecificOutput`-nested non-deny verdict, or ""."""
    if not isinstance(verdict, Mapping):
        return ""
    nested = verdict.get("hookSpecificOutput")
    context = (nested.get("additionalContext") if isinstance(nested, Mapping) else None) or verdict.get("additionalContext")
    return context.strip() if isinstance(context, str) else ""
