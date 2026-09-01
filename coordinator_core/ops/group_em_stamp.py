"""
coordinator_core.ops.group_em_stamp — JSON-RPC "groupem.stamp".

Purpose: expose `coordinator_core.group_em.watch_heartbeat.stamp` through the
op registry, mirroring `coordinator_core.ops.session_peer_roster`'s own thin
registration convention exactly. The Group EM's watch-heartbeat stamp used to
be reachable only as an in-process Python call; during an hour-long box-wide
Bash denial on 2026-09-01 a crown holding the watch could not stamp a
declination through any door at all — the capability was already correct
in-process Python, only the door was missing. This op is that door, nothing
reimplemented.

Self-registration: importing this module calls
register_op("groupem.stamp", _groupem_stamp) as a side-effect. Registered in
`coordinator_core/ops/__init__.py`'s registration list,
`coordinator_core/ops/_registry_map.py`'s `OP_MODULE_MAP`,
`coordinator_core/op_scopes.py` (scope "none" — same resolution story as
`groupem.enter`: the watch record this writes lives under a repo-scoped path
passed verbatim as `repo_root`, but the op itself never resolves or injects
one, matching every other "none"-scoped op on this surface), and
`coordinator_core/authz/classification.py` (MUTATING — this op writes the
heartbeat record via `watch_heartbeat.write_atomic`, unlike its two sibling
ops in this same task which are read-only).

Spec backlink: `state/dispatch-briefs/2026-09-01-the-crowns-standing-surfaces-report-themselves/C7.md`.

Negative-spec:
    - Never catches or degrades `watch_heartbeat.stamp`'s own exceptions
      (missing `writer_session_id`, an unrecognized `tick_source`) — those
      are input-validation failures the caller must see and fix, not legs
      of a composed op with a degrade-never-raise contract (contrast
      `groupem.enter`, which composes four independent legs and degrades
      each; this op wraps exactly one function and passes its behavior
      through unmodified).
    - Never supplies a default `interval_seconds` — the caller's own tick
      cadence is not this veneer's to guess.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from coordinator_core.ipc import register_op
from coordinator_core.group_em import read_pass as group_em_read_pass
from coordinator_core.group_em import watch_heartbeat


@register_op("groupem.stamp")
def _groupem_stamp(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "groupem.stamp" handler.

    Params:
        repo_root (str, optional) -- the repo whose watch record is
            stamped. Defaults to the CALLING process's own `os.getcwd()`
            when omitted, matching `groupem.enter`'s own convention.
        holder_session_id (str, optional) -- defaults to
            `read_pass.caller_session_id()` (the `CLAUDE_CODE_SESSION_ID`
            env var) when omitted.
        declinations (list, optional) -- this tick's declination rows;
            defaults to `[]`.
        interval_seconds (number, required).
        subscribed_peers (int, optional) -- defaults to `1`.
        now_epoch (number, optional).
        tick_source (str, optional) -- defaults to
            `watch_heartbeat.TICK_SOURCE`.
        holder_name (str, optional).
        writer_session_id (str, optional) -- defaults to
            `read_pass.caller_session_id()` when omitted, matching
            `holder_session_id`'s own default.

    Returns:
        {"stamped": bool}

    `stamped` is exactly `watch_heartbeat.stamp`'s own return value —
    `False` on a fresh-and-foreign decline (record left untouched, see that
    function's own docstring), `True` on a written record. No new meaning
    is layered on top of it here.

    Scope "none" (op_scopes.py): the engine-injected `repo_root` kwarg is
    never resolved/injected for a "none"-scoped op — the `repo_root` WIRE
    PARAM above (read from `params`) is the only way a caller narrows the
    target, matching `groupem.enter`'s own convention exactly.
    """
    p = params if isinstance(params, dict) else {}

    override = p.get("repo_root")
    target_root = override if isinstance(override, str) and override else str(Path.cwd())

    sid_override = p.get("holder_session_id")
    holder_session_id: Optional[str] = (
        sid_override if isinstance(sid_override, str) and sid_override else None
    )
    if holder_session_id is None:
        holder_session_id = group_em_read_pass.caller_session_id()

    if not holder_session_id:
        raise ValueError(
            "holder_session_id is unresolvable -- no `holder_session_id` param and no "
            "CLAUDE_CODE_SESSION_ID in the environment. Stamping anyway would write a "
            "crown row that names no crown, which is the assert-more-than-you-know defect "
            "this record exists to close."
        )

    resolved_caller = group_em_read_pass.caller_session_id()
    writer_override = p.get("writer_session_id")
    writer_session_id: Optional[str] = (
        writer_override if isinstance(writer_override, str) and writer_override else None
    )
    if writer_session_id is None:
        writer_session_id = resolved_caller
    elif resolved_caller and writer_session_id != resolved_caller:
        raise ValueError(
            f"writer_session_id {writer_session_id!r} disagrees with this caller's resolved "
            f"identity {resolved_caller!r}. `writer_session_id` is what `is_fresh_and_foreign` "
            "compares to decide whether to decline, and what `_writer_identity` compares to "
            "decide whether to persist a `prior_*` trace -- so a writer naming itself as "
            "someone else bypasses the decline AND suppresses the trace in one move, which is "
            "the silent destruction this record exists to prevent. A guard authenticated by "
            "the party it guards is not a guard. Omit the param to be identified, or correct it."
        )

    declinations = p.get("declinations")
    declinations = declinations if isinstance(declinations, list) else []

    kwargs: dict[str, Any] = {
        "subscribed_peers": p.get("subscribed_peers", 1),
        "now_epoch": p.get("now_epoch"),
        "tick_source": p.get("tick_source", watch_heartbeat.TICK_SOURCE),
        "holder_name": p.get("holder_name"),
        "writer_session_id": writer_session_id,
    }

    stamped = watch_heartbeat.stamp(
        target_root,
        holder_session_id,
        declinations,
        p["interval_seconds"],
        **kwargs,
    )

    return {"stamped": stamped}
