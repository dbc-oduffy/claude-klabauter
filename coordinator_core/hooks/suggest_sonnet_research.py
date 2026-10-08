
from __future__ import annotations

import os

from coordinator_core._hook_envelope import payload_of
from coordinator_core.ipc import register_op
from coordinator_core.hooks._envelope import allow_advisory, no_advisory
from coordinator_core.hooks._payload import field
from coordinator_core.session.identity import resolve_subagent_identity


def _deep_research_plugin_dir() -> str | None:
    try:
        from coordinator_core.resolve_coordinator_clone import resolve_content_root

        clone = resolve_content_root()
    except Exception:
        return None
    if not clone:
        return None
    return os.path.join(clone, "pipelines", "deep-research")

_MSG_WITH_PLUGIN = (
    "DELEGATION REQUIRED: web research as Opus. Use instead:\n"
    "  /coordinator:research\n"
    "  emit-dispatch-workflow --research --ask \"<question>\"\n"
    "  emit-dispatch-workflow --from-sizing <path>\n"
    "  Agent subagent_type='Explore'\n"
    "  Agent subagent_type='coordinator:enricher'\n"
    "Direct web calls: single URL/fact only, never a generic search Agent."
)

_MSG_WITHOUT_PLUGIN = (
    "DELEGATION REQUIRED: web research as Opus. Use instead:\n"
    "  install deep-research plugin, then /coordinator:research\n"
    "  emit-dispatch-workflow --research --ask \"<question>\"\n"
    "  emit-dispatch-workflow --from-sizing <path>\n"
    "  Agent subagent_type='Explore'\n"
    "  Agent subagent_type='coordinator:enricher'\n"
    "Direct web calls: single URL/fact only, never a generic search Agent."
)


def _research_plugins() -> bool:
    """Whether the deep-research plugin directory is present."""
    plugin_dir = _deep_research_plugin_dir()
    return plugin_dir is not None and os.path.isdir(plugin_dir)


@register_op("hooks.suggest_sonnet_research")
async def _handler(params: dict, repo_root=None) -> dict:
    params = payload_of(params)
    import asyncio

    agent_id = field(params, "agent_id")
    session_id = field(params, "session_id")
    if resolve_subagent_identity(agent_id, session_id):
        return no_advisory()

    plugin_present = await asyncio.to_thread(_research_plugins)
    msg = _MSG_WITH_PLUGIN if plugin_present else _MSG_WITHOUT_PLUGIN
    return allow_advisory("PreToolUse", msg)
