"""coordinator_core.bash_guards._gem_standing_kill -- the shared verdict the kill
guards consult before denying a Group EM's box-saving kill.

One predicate, no import-time I/O. ``gem_standing`` reads the nomination records
only when called; callers invoke it after their own deny decision so an ordinary
command never pays that read.
"""

from __future__ import annotations

from typing import Any, Mapping


def gem_standing(payload: Mapping[str, Any]) -> bool:
    """True iff the hook payload's session is the live Group EM.

    A payload carrying ``agent_id`` is a subagent of that session, never the
    Group EM. Any error gives False.
    """
    try:
        if payload.get("agent_id"):
            return False
        session_id = payload.get("session_id") or ""
        repo_root = payload.get("cwd") or ""
        if not session_id or not repo_root:
            return False
        from coordinator_core.bash_guards import _gem_cross_repo_commit

        return bool(
            _gem_cross_repo_commit.session_holds_live_gem_standing(
                str(session_id), str(repo_root)
            )
        )
    except Exception:
        return False
