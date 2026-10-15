"""
coordinator_core.ops.watcher_stand_down -- JSON-RPC "workstream_complete.stand_down_watchers".

Purpose: the closing /workstream-complete step stops this session's coordinator watchers
(group-em-watch, group-em-memory-governor), verifies by process-table re-snapshot that none
survives, and stands a Group EM holder down, returning the ended-notice recipients and text.

Negative-spec:
    - Never raises: a bad param is `{"error": ...}`.
    - Never spawns, never sends the notice; the caller relays the SendMessage.
    - Never kills a process no session key attributes to this session.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.workstream_complete.watcher_stand_down import stand_down_watchers
from coordinator_core.workstream_complete.watcher_stand_down_contract import OP_NAME


@register_op(OP_NAME)
def _stand_down_watchers(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "workstream_complete.stand_down_watchers" handler.

    Params:
        session_id (str) -- required; the closing session.
        repo_root (str, optional) -- the repo whose Group EM nomination to stand down; defaults to
            the caller's `os.getcwd()`, matching `groupem.standing`.

    Returns:
        {stopped, survivors, unattributed, expires_on_its_own, stand_down, notice, warnings}
        or {"error": str} on an invalid param.
    """
    p = params if isinstance(params, dict) else {}
    sid = p.get("session_id")
    if not isinstance(sid, str) or not sid:
        return {"error": "session_id (non-empty str) is required"}
    override = p.get("repo_root")
    if override is not None and (not isinstance(override, str) or not override):
        return {"error": "repo_root must be a non-empty str when given"}
    target = override if override else str(Path.cwd())
    try:
        return stand_down_watchers(sid, target)
    except Exception as exc:  # noqa: BLE001 -- degrade-never-raise
        return {"error": f"{type(exc).__name__}: {exc}"}
