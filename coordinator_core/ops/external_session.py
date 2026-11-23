"""external_session.handle: the Codex lifecycle bridge's work, served by the warm engine.

The caller supplies everything process-local: its environment and its own pid. The engine's
environment and process ancestry describe the server, never the Codex host.
"""
from coordinator_core.ipc import register_op, CallerFacingValidationError

EVENTS = ("SessionStart", "SessionEnd", "SubagentStart", "UserPromptSubmit", "PostToolUse",
          "Stop", "SubagentStop", "PreCompact", "PostCompact")
PARAMS_SCHEMA = {
    "type": "object", "properties": {
        "event": {"type": "string", "enum": list(EVENTS)},
        "payload": {"type": "object", "description": "The hook payload Codex wrote to the bridge's stdin."},
        "env": {"type": "object", "additionalProperties": {"type": "string"},
                "description": "The bridge process environment; the context functions read this, never the engine's."},
        "caller_pid": {"type": "integer", "minimum": 1,
                       "description": "The bridge's own pid; the Codex host is found among its ancestors."},
    }, "required": ["event", "payload", "env", "caller_pid"], "additionalProperties": False,
}


def _observed_launch(caller_pid: int) -> dict:
    """Launch facts of the Codex ancestor of `caller_pid`; empty facts when none is observable."""
    from coordinator_core.session.agent_role import observed_cli_launch
    try:
        import psutil
        for process in psutil.Process(caller_pid).parents():
            if process.name().lower().removesuffix(".exe") == "codex":
                return observed_cli_launch(process.cmdline())
    except Exception:  # noqa: BLE001 -- no observable ancestor means no native-main evidence
        pass
    return observed_cli_launch([])


@register_op("external_session.handle", params_schema=PARAMS_SCHEMA)
def handle(params, repo_root=None) -> dict:
    """Apply one Codex lifecycle event: registry side effect, then the hook output for it.

    SessionEnd unregisters the thread; a top-level SessionStart registers it; SessionStart and
    SubagentStart render session context; every other event goes to the Codex lifecycle
    handler. Returns `{"output": <hook envelope>, "notices": [<stderr lines>]}`; a registry
    failure is a notice, never a lost context result.
    """
    p = params if isinstance(params, dict) else {}
    unknown = set(p) - {"event", "payload", "env", "caller_pid"}
    if unknown:
        raise CallerFacingValidationError(f"external_session.handle: unknown fields {sorted(unknown)}")
    event, payload, env, caller_pid = p.get("event"), p.get("payload"), p.get("env"), p.get("caller_pid")
    if event not in EVENTS:
        raise CallerFacingValidationError(f"external_session.handle: event must be one of {list(EVENTS)}")
    if not isinstance(payload, dict) or not isinstance(env, dict):
        raise CallerFacingValidationError("external_session.handle: payload and env must be objects")
    if isinstance(caller_pid, bool) or not isinstance(caller_pid, int) or caller_pid < 1:
        raise CallerFacingValidationError("external_session.handle: caller_pid must be a positive integer")
    env = {k: v for k, v in env.items() if isinstance(k, str) and isinstance(v, str)}
    payload = {**payload, "hook_event_name": event}

    from coordinator_core.session import external_registry
    notices = []
    try:
        if event == "SessionEnd":
            external_registry.unregister(payload["session_id"], payload=payload)
        elif event == "SessionStart" and not payload.get("agent_id"):
            external_registry.register_current(payload, host_pid=caller_pid, env=env)
    except Exception as exc:  # noqa: BLE001 -- registration is best-effort beside the context result
        notices.append(f"external session registration unavailable: {type(exc).__name__}: {exc}")
    output = {}
    if event in ("SessionStart", "SubagentStart"):
        from coordinator_core.host_hooks.session_context import context
        output = context(payload, env, observed=_observed_launch(caller_pid))
    elif event != "SessionEnd":
        from coordinator_core.host_hooks.codex_lifecycle import handle as lifecycle
        output = lifecycle(payload, env)
    return {"output": output, "notices": notices}
