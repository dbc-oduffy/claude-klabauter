"""Feeds each product-path write of a Bash command to the XS lane budget verdict.

The Edit/Write hook is the only other producer of a lane projection; this guard asks the same
verdict (`_em_write_budget.shell_write_budget`) so a heredoc, `tee`, `sed -i`, `cp` or
interpreter-body write is counted or denied exactly as an Edit would be. A literal `cat`/`tee`
heredoc body is projected in memory; any other shape is "not projectable" and is denied as
unmeasurable where a lane governs the session. No subprocess: root resolution is a walk and the
lane measure reads blobs in process. Fail-open on a crash; the Edit/Write path stays the hard gate.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

_PREFILTER_TOKENS = (">", "tee", "<<", "-c", "sed", "cp", "mv")
_NOT_PROJECTABLE = "shell write not projectable"
_GUARD_NAME = "xs-lane-shell-write"
_EDIT_ROUTE = "one plain Edit per file"


def _record(envelope: Dict[str, Any]) -> Any:
    from coordinator_core.bash_guards._rewrite_support import _alt_record
    from coordinator_core.guard_messages.alternative_record import AdaptableOffer

    out = envelope.get("hookSpecificOutput") or {}
    deny = out.get("permissionDecision") == "deny"
    text = out.get("permissionDecisionReason") or out.get("additionalContext") or ""
    return _alt_record(
        "deny" if deny else "advisory",
        _GUARD_NAME,
        text,
        (AdaptableOffer(template=_EDIT_ROUTE, what_to_adapt="the change"),),
        em_text=text if _EDIT_ROUTE in text else None,
    )


def check(cmd: str, session_id: str, cwd: str, payload: Optional[Dict[str, Any]] = None) -> Any:
    """The first deny among the command's product write targets (an `AlternativeRecord`), else
    the first advisory context the verdict returned, else None. Each admitted target joins the
    lane record through the shared verdict -- at PreToolUse, so a later guard's deny of the
    command leaves the path counted, and a deny on a later target leaves earlier ones counted."""
    if not cmd or not any(tok in cmd for tok in _PREFILTER_TOKENS):
        return None
    try:
        return _check(cmd, session_id, cwd, payload or {})
    except Exception:  # noqa: BLE001 -- fail-open: the Edit/Write hook is the hard gate
        return None


def _check(cmd: str, session_id: str, cwd: str, payload: Dict[str, Any]) -> Any:
    from coordinator_core.bash_guards._shell_write_projection import shell_writes
    from coordinator_core.hooks._em_write_budget import shell_write_budget
    from coordinator_core.hooks.nudge_unrouted_sizing import _repo_root
    from coordinator_core.session import xs_lane_budget as xlb

    if "agent_id" in payload or not session_id:
        return None
    if cwd and not payload.get("cwd"):
        payload = dict(payload, cwd=cwd)
    root = _repo_root(payload)
    if not root:
        return None
    advisory = None
    for target in shell_writes(cmd, cwd or payload.get("cwd") or "", root):
        projected = None
        if target.body is not None:
            projected = xlb.project_shell_write(target.abs_path, target.body, target.append)
        envelope = shell_write_budget(
            payload,
            session_id,
            target.abs_path,
            projected,
            None if projected is not None else _NOT_PROJECTABLE,
        )
        if envelope is None:
            continue
        if (envelope.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny":
            return _record(envelope)
        if advisory is None:
            advisory = envelope
    return _record(advisory) if advisory is not None else None
