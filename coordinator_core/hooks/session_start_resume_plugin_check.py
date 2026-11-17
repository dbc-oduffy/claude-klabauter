"""hooks.session_start_resume_plugin_check -- SessionStart advisory: an EM-role session that carries no
coordinator agents was resumed with plain `claude --resume`, which drops the coordinator plugin silently.
Names the `coordinator --resume <id>` relaunch. Advisory only; never denies."""

from __future__ import annotations

import os
from pathlib import Path

from coordinator_core.hooks._envelope import context_only, no_advisory, payload_of
from coordinator_core.ipc import register_op


def coordinator_agents_loaded() -> bool:
    """True when the plugin root this session was launched with carries agent definitions.
    Trap: CLAUDE_PLUGIN_ROOT is set only when the plugin is loaded, so an unset or agent-less root
    is the plain-resume signature; the on-disk install is deliberately not consulted (it survives
    the drop)."""
    raw = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if not raw:
        return False
    try:
        return next(Path(raw).glob("agents/*.md"), None) is not None
    except OSError:
        return False


def _em_role_session(payload: dict, session_id: str) -> bool:
    from coordinator_core.hooks.support import turn_intent
    from coordinator_core.hooks.support.git_common_dir import resolve_git_common_dir

    root = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()
    common = resolve_git_common_dir(str(root))
    return bool(common) and turn_intent.role_marker_present(turn_intent.session_dir(common, session_id))


def build_report(payload: dict) -> "str | None":
    """The relaunch advice, or None when the session is not an EM-role session or has its agents."""
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    if coordinator_agents_loaded() or not _em_role_session(payload, session_id):
        return None
    return (
        "Coordinator plugin missing: this EM-role session has no coordinator agents -- a plain "
        "`claude --resume` drops the plugin. Relaunch with `coordinator --resume "
        f"{session_id}`."
    )


@register_op("hooks.session_start_resume_plugin_check")
def _handler(params: dict, repo_root=None) -> dict:
    try:
        report = build_report(payload_of(params))
    except Exception:
        return no_advisory()
    if not report:
        return no_advisory()
    return context_only("SessionStart", report)
