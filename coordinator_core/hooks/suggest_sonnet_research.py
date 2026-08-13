"""
coordinator_core.hooks.suggest_sonnet_research — PreToolUse advisory hook op.

Purpose: Nudges the EM to delegate web research to dedicated skills/agents rather
than running ad-hoc research as Opus. Pure advisory (allow + additionalContext) —
never blocks. Suppressed when fired from a subagent context — either a bare-hex
unnamed agent or a named teammate, as resolved by the shared
`resolve_subagent_identity` primitive — to avoid re-emitting the "delegate to a
scout" advisory at researcher-altitude. An agent_id that resolves to nothing
(malformed, absent, or unrecognised shape) is NOT suppressed.

The deep-research plugin conditional message is preserved faithfully from
Port of: suggest-sonnet-research.sh (example-doctrine-repo 3a561713, 2026-07-22). If the plugin
directory is absent, a condensed install-prompt variant is used instead.

Negative-spec:
    Do NOT block or deny — this hook is advisory only. Subagent suppression is the
    only path to no_advisory(); all other fires return allow_advisory().

Spec backlink: docs/plans/2026-07-04-pcore-04-advisory-hook-ops-claude-klabauter-engine.md § C2
"""

from __future__ import annotations

import os

from coordinator_core.ipc import register_op
from coordinator_core.hooks._envelope import allow_advisory, no_advisory
from coordinator_core.hooks._payload import field
from coordinator_core.session.identity import resolve_subagent_identity


def _deep_research_plugin_dir() -> str | None:
    """Resolve the bundled deep-research plugin dir under the coordinator content root.

    Delegates to the native in-process peer of `resolve-coordinator-clone
    --for-content` (`coordinator_core.resolve_coordinator_clone.resolve_content_root`)
    rather than spawning the CLI. The prior implementation subprocessed a single
    hardcoded `~/.claude/bin/resolve-coordinator-clone` rung, which the settings-home
    migration retired: that path no longer exists on a migrated machine, every
    resolution failed, and the hook silently degraded to the "plugin absent" advisory
    variant on every WebFetch/WebSearch. It was also a per-fire process spawn on a
    PreToolUse hot path — the same defect class already fixed in
    `resolve_coordinator_clone._registry_live_path`.

    Returns None if the content root can't be resolved — resolution failure degrades to
    "plugin absent", it never raises.
    """
    try:
        from coordinator_core.resolve_coordinator_clone import resolve_content_root

        clone = resolve_content_root()
    except Exception:
        return None
    if not clone:
        return None
    return os.path.join(clone, "pipelines", "deep-research")

# Advisory text when the deep-research plugin is installed.
_MSG_WITH_PLUGIN = (
    "DELEGATION REQUIRED: web research as Opus. EM orchestrates, researchers execute:\n"
    "- Internet → /coordinator:research --mode=web <topic>\n"
    "- Codebase/repo → /coordinator:research --mode=repo <path> [--deepest]\n"
    "- Structured batch → /coordinator:research --mode=structured <spec-path>\n"
    "- Quick exploration → Agent subagent_type='Explore'\n"
    "- Spec enrichment → Agent subagent_type='coordinator:enricher'\n"
    "- YouTube/podcast/audio → /notebooklm-research\n"
    "Direct web calls only for a specific URL or a single mid-conversation fact.\n"
    "Do NOT spin up a generic Agent(prompt='go search for...') — discards guardrails."
)

# Advisory text when the deep-research plugin is NOT installed.
_MSG_WITHOUT_PLUGIN = (
    "DELEGATION REQUIRED: web research as Opus. EM orchestrates, researchers execute:\n"
    "- Any research → install the deep-research plugin, then "
    "/coordinator:research --mode={web,repo,structured}\n"
    "- Quick exploration → Agent subagent_type='Explore'\n"
    "- Spec enrichment → Agent subagent_type='coordinator:enricher'\n"
    "Direct web calls only for a specific URL (one fetch) or a single mid-conversation fact.\n"
    "Do NOT spin up a generic Agent(prompt='go search for...') — discards tested guardrails."
)


def _has_deep_research_plugin() -> bool:
    """Return True iff the deep-research plugin directory exists on disk."""
    plugin_dir = _deep_research_plugin_dir()
    if plugin_dir is None:
        return False
    return os.path.isdir(plugin_dir)


@register_op("hooks.suggest_sonnet_research")
async def _handler(params: dict, repo_root=None) -> dict:
    """PreToolUse advisory hook: nudge the EM to delegate web research.

    Suppression: when agent_id resolves via `resolve_subagent_identity` — either
    a bare-hex unnamed agent or a named teammate — the caller is already a
    delegated researcher; emitting the advisory at subagent-altitude is
    doctrine-incorrect and noisy. Return no_advisory() immediately. An agent_id
    that fails to resolve (malformed, absent, unrecognised shape) does NOT
    suppress — the advisory still fires.

    Otherwise: check whether the deep-research plugin is installed (blocking I/O
    wrapped in asyncio.to_thread) and return allow_advisory with the appropriate
    message variant.
    """
    # asyncio deferred to first use here (not module scope) — this is the only function
    # in the module touching the asyncio namespace at runtime; a module-scope
    # `import asyncio` dragged asyncio.base_events (~5ms) into every eager op/hook
    # import even for callers that never dispatch this PreToolUse hook. Spec:
    # docs/plans/2026-07-24-canonical-resolution-engine.md task W0-1.
    import asyncio

    # Subagent suppression — agent_id resolves (bare-hex unnamed agent, or named
    # teammate `a<name>-<16hex>` + session_id) via the shared, fail-closed
    # `resolve_subagent_identity`; suppress advisory at subagent altitude.
    # Review: code-reviewer — A-F4: restore format guard; `present()` alone would
    # suppress on any non-empty agent_id string, including malformed values. That
    # fail-closed property now comes from `resolve_subagent_identity`'s branch
    # (c), which returns "" for garbage, short hex, uppercase hex, and malformed
    # named-teammate shapes, rather than a local bare-hex-only regex.
    agent_id = field(params, "agent_id")
    session_id = field(params, "session_id")
    if resolve_subagent_identity(agent_id, session_id):
        return no_advisory()

    plugin_present = await asyncio.to_thread(_has_deep_research_plugin)
    msg = _MSG_WITH_PLUGIN if plugin_present else _MSG_WITHOUT_PLUGIN
    return allow_advisory("PreToolUse", msg)
