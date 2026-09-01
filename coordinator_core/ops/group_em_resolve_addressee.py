"""
coordinator_core.ops.group_em_resolve_addressee — JSON-RPC
"groupem.resolve_addressee".

Purpose: expose `coordinator_core.group_em.send_pass.resolve_addressee`
through the op registry, mirroring `coordinator_core.ops.session_peer_roster`'s
own thin registration convention exactly. Re-resolving a peer's live name
before sending is already correct in-process Python; during an hour-long
box-wide Bash denial on 2026-09-01 a crown could not run it at all — the
capability was never missing, only the door was.

Self-registration: importing this module calls
register_op("groupem.resolve_addressee", _groupem_resolve_addressee) as a
side-effect. Registered in `coordinator_core/ops/__init__.py`'s registration
list, `coordinator_core/ops/_registry_map.py`'s `OP_MODULE_MAP`,
`coordinator_core/op_scopes.py` (scope "none" — same resolution story as
`groupem.enter`), and `coordinator_core/authz/classification.py` (read-only —
`send_pass.resolve_addressee` only re-reads the live peer registry via
`peer_roster.build_roster` and writes nothing).

Spec backlink: `state/dispatch-briefs/2026-09-01-the-crowns-standing-surfaces-report-themselves/C7.md`.

Negative-spec:
    - Never falls back to the bare `peer_session_id` when the underlying
      function returns `None` — `None` is a REFUSAL per
      `send_pass.resolve_addressee`'s own docstring (no usable name today),
      and this veneer passes that refusal through unmodified rather than
      inventing a fallback address.
    - Never accepts a `build_roster` override from `params` — that
      parameter is a test-injection seam on the underlying function, not a
      wire-level capability; a caller reaching this op always gets the live
      registry.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.group_em import send_pass as group_em_send_pass


@register_op("groupem.resolve_addressee")
def _groupem_resolve_addressee(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "groupem.resolve_addressee" handler.

    Params:
        repo_root (str, optional) -- the repo whose live peer registry is
            consulted. Defaults to the CALLING process's own `os.getcwd()`
            when omitted, matching `groupem.enter`'s own convention.
        peer_session_id (str, required) -- the session id to resolve.

    Returns:
        {"name": str | None}

    `name` is exactly `send_pass.resolve_addressee`'s own return value —
    `None` means REFUSAL (session id not in today's roster, no usable
    `name` on the row it is in, or the roster read itself failed); see that
    function's own docstring. No new meaning is layered on top of it here.

    Scope "none" (op_scopes.py): the engine-injected `repo_root` kwarg is
    never resolved/injected for a "none"-scoped op — the `repo_root` WIRE
    PARAM above (read from `params`) is the only way a caller narrows the
    target, matching `groupem.enter`'s own convention exactly.
    """
    p = params if isinstance(params, dict) else {}

    override = p.get("repo_root")
    target_root = override if isinstance(override, str) and override else str(Path.cwd())

    name = group_em_send_pass.resolve_addressee(target_root, p["peer_session_id"])

    return {"name": name}
