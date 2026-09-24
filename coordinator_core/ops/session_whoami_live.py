"""
coordinator_core.ops.session_whoami_live — JSON-RPC "session.whoami_live".

Purpose: answer "who am I, and is that id live" in one in-process call,
composing `coordinator_core.session.core.resolve_session_id` and
`coordinator_core.session.liveness.session_live` — zero spawns, well under
the 500ms brightline.

Self-registration: importing this module calls
register_op("session.whoami_live", ...) as a side-effect. Registered in
`coordinator_core/ops/__init__.py`'s `_EAGER_OP_MODULES` and
`coordinator_core/ops/_registry_map.py`'s `OP_MODULE_MAP`, classified
COMPUTE_ONLY in `coordinator_core/authz/classification.py`, and scoped
"none" in `coordinator_core/op_scopes.py` — same resolution story as
`session.resolve_address` (file-for-file precedent, per this op's spec row).

Spec: docs/plans/2026-09-22-inbox-blitz-bundled-xs-s-fixes-2026-09-11.md
(item 54, T54).

Negative-spec:
    - Does NOT reuse the `coordinator_whoami` name — that is a retired
      installer package name, not this op's.
    - Does NOT live in `session/core.py` — `session/liveness.py` imports
      `core`, so `core` importing `liveness` back would be circular. This
      module composes both from the outside instead.
    - Does NOT re-derive liveness itself (no direct `stable_pid_alive`/
      registry read) — it delegates to `liveness.session_live`, the one
      shared liveness key, so this op cannot diverge from every other
      liveness consumer on what "live" means.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.session import core, liveness


@register_op("session.whoami_live")
def _session_whoami_live(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "session.whoami_live" handler.

    Params: none consumed.

    Returns:
        {"session_id": str, "live": bool}

    `session_id` is `core.resolve_session_id()`'s return verbatim — empty
    string when unresolvable, never an exception (see that function's
    docstring). `live` is `False` for an unresolvable (empty) session id,
    without calling `liveness.session_live` on an empty string (that
    function already returns False on empty `sid`, but this avoids relying
    on that as the reason).
    """
    cwd = str(repo_root) if repo_root else None
    session_id = core.resolve_session_id(cwd=cwd)
    live = bool(session_id) and liveness.session_live(session_id, cwd=cwd)
    return {"session_id": session_id, "live": live}
