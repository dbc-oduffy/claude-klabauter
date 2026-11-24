"""Native lifecycle/advisory ports; no Claude transcript format is assumed.

Turn end never unregisters a thread. Compaction notices use the documented UI
channel; only SessionStart(source=compact) restores model-visible role context.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

EVENTS = frozenset({"UserPromptSubmit", "PostToolUse", "Stop", "SubagentStop", "PreCompact", "PostCompact"})


# The only payload keys codex_status.hook.handle reads. Forwarding just these keeps the stdin
# write far under any pipe buffer, so it never waits on the child interpreter's start.
# Must equal ops.codex_status.REFRESH_PAYLOAD_KEYS (the warm op rejects any other field); this
# module cannot import the op module without paying its import cost on the hook path.
_STATUS_KEYS = ("session_id", "cwd", "transcript_path", "agent_transcript_path")


def _notice(text):
    return {"systemMessage":text} if text else {}


def _detach_kwargs() -> dict:
    # DETACHED_PROCESS alone: no console is ever allocated, so no conhost is spawned (one process
    # fewer than CREATE_NO_WINDOW). Never OR CREATE_NO_WINDOW in; Win32 then ignores it.
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200) | getattr(subprocess, "DETACHED_PROCESS", 0x8)}  # popup-intentional-last-resort
    return {"start_new_session": True}


#: Hard wall for the warm dial, inside the 200ms process budget.
WARM_DIAL_BUDGET_SECS = 0.15
REFRESH_OP = "codex_status.refresh"


def refresh_status_warm(payload: dict, event: str) -> bool:
    """Ask the warm engine to run the status refresh; True only when it acknowledged.

    Never starts a server and never blocks past WARM_DIAL_BUDGET_SECS. A miss is counted by the
    warm client's own cold-fallback row.
    """
    if not isinstance(payload.get("session_id"), str) or not isinstance(payload.get("cwd"), str):
        return False
    request = {"jsonrpc": "2.0", "id": 1, "method": REFRESH_OP,
               "params": {"event": event, **{key: payload[key] for key in _STATUS_KEYS if isinstance(payload.get(key), str)}},
               "_origin_worktree": payload["cwd"]}
    box: dict = {}

    def dial() -> None:
        try:
            from coordinator_core.warm import client
            box["response"] = client.try_warm_dispatch_no_spawn(request, read_deadline_secs=WARM_DIAL_BUDGET_SECS, hook_path=True)
        except Exception:  # noqa: BLE001 -- a dial failure is a miss
            box["response"] = None

    worker = threading.Thread(target=dial, daemon=True)
    worker.start()
    worker.join(WARM_DIAL_BUDGET_SECS)
    response = box.get("response")
    return isinstance(response, dict) and "error" not in response and isinstance(response.get("result"), dict)


def refresh_status(payload: dict, event: str) -> bool:
    """Warm op call first; the detached interpreter only when the warm door did not answer."""
    return refresh_status_warm(payload, event) or refresh_status_detached(payload, event)


def refresh_status_detached(payload: dict, event: str) -> bool:
    """Hand the status refresh (rollout snapshot, app-server title set) to a detached interpreter.

    The hook never imports codex_status or opens the app-server proxy; the child reads the
    payload from stdin. Returns False when the spawn itself failed.
    """
    root = str(Path(__file__).resolve().parents[2])
    env = dict(os.environ)
    env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "coordinator_core.codex_status.hook", "--event", event],
            cwd=root, env=env, stdin=subprocess.PIPE, text=True, encoding="utf-8", stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True, **_detach_kwargs())
        pipe = proc.stdin
        if pipe is None:
            return False
        try:
            pipe.write(json.dumps({key: payload[key] for key in _STATUS_KEYS if key in payload}))
        finally:
            pipe.close()
        return True
    except (OSError, ValueError, TypeError):
        return False


_LAZY = {"external_registry": "coordinator_core.session.external_registry",
         "explicit_worker_marker": "coordinator_core.session.agent_role",
         "role_for_caller": "coordinator_core.session.agent_role"}


def __getattr__(name: str) -> object:
    """Import the role dependencies on first use so the status-refresh path never pays for them."""
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(name)
    import importlib
    module = importlib.import_module(target)
    value = module if name == "external_registry" else getattr(module, name)
    globals()[name] = value
    return value


def _codex_role(env: dict, session_id) -> str:
    """role_for_caller without its Claude-harness rung.

    A Codex payload's session lives only in the external registry; an absent row there would
    otherwise fall through to a scan of every registered harness session file (~0.5s).
    """
    here = sys.modules[__name__]
    if here.explicit_worker_marker(env):
        return "worker"
    row = here.external_registry.lookup(session_id) if isinstance(session_id, str) else None
    if isinstance(row, dict) and row.get("session_id") == session_id:
        return here.role_for_caller(env, session_id)
    return "unknown"


def handle(payload: dict, env: dict, *, plugin_root=None) -> dict:
    event = payload.get("hook_event_name")
    if event not in EVENTS:
        return {}
    if event in ("Stop", "SubagentStop", "PostCompact"):
        refresh_status(payload, event)  # Advisory: a failed spawn never costs the other hook output.
    def role() -> str:
        # The registry lookup is the costly step; only events that act on the role pay for it.
        return "worker" if payload.get("agent_id") else _codex_role(env, payload.get("session_id"))

    if event == "Stop":
        if payload.get("stop_hook_active"):
            return {}
        text = payload.get("last_assistant_message")
        if not isinstance(text, str) or not text.strip() or role() != "em":
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
        text = adapter.event_advisory(payload, role())
        if not text:
            return {}
        return {"hookSpecificOutput":{"hookEventName":event, "additionalContext":text}}
    return {}
