"""Codex lifecycle context bridge to doctrine-owned audience and Unreal notices."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from coordinator_core.session.agent_role import classify, native_launch


def load_adapter(plugin_root=None):
    if plugin_root is None:
        from coordinator_core.content_root import read_content_root
        value = read_content_root()
        if not value:
            raise ValueError("coordinator content root unavailable")
        plugin_root = Path(value)
    root = Path(plugin_root)
    path = root / "hooks" / "scripts" / "codex-session-context.py"
    if not path.is_file():
        raise ValueError("published doctrine lacks Codex session context adapter")
    spec = importlib.util.spec_from_file_location("coordinator_codex_session_context", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def context(payload, env, *, plugin_root=None, observed=None, probe=None):
    """Build one native hook envelope without changing registration or parent role."""
    role = classify(payload, env, **(native_launch() if observed is None else observed))
    adapter = load_adapter(plugin_root)
    route = None
    if adapter.is_unreal(payload):
        if probe is None:
            from coordinator_core.host_hooks.ue_route import probe_example_retrieval_repo
            probe = probe_example_retrieval_repo
        route = probe()
    text = adapter.render_context(payload, role, route)
    event = payload.get("hook_event_name")
    if event not in ("SessionStart", "SubagentStart"):
        return {}
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
