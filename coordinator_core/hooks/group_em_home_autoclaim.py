"""coordinator_core.hooks.group_em_home_autoclaim — SessionStart claim of the Group EM
on a boot inside the group-em home.

A session whose cwd lies in the resolved group-em home claims through the in-process
`groupem.enter` op with `entry_evidence="home-start"` (the explicit path is
`group-em-enter --home-start`). The claim stays pending until the session's own transcript
start resolves it, per `nomination._home_start_evidence`.

Invariants:
  - The session id is the payload's, never ambient env.
  - No supersede/incumbent-override signal is ever passed; a refusal (live incumbent,
    refuted transcript) is reported in additionalContext, never swallowed.
  - Outside the home, or on a `compact` restart, the hook is silent and writes nothing.
  - Any failure degrades to silence; the hook never blocks a boot.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core._hook_envelope import payload_of
from coordinator_core.group_em import human_entry
from coordinator_core.hooks._envelope import context_only, no_advisory
from coordinator_core.hooks.group_em_autofire import render_additional_context
from coordinator_core.ipc import register_op
from coordinator_core.warm.entry_seam import reentrant_dispatch

_SKIP_SOURCES = frozenset({"compact"})


def _render(entered: dict) -> str:
    """The claimed result renders as the autofire digest; a refusal or failed leg names itself."""
    standing = entered.get("standing") or {}
    if entered.get("refusal") or entered.get("nomination_error"):
        reason = entered.get("nomination_error") or standing.get("message") or "nomination refused"
        return (
            "## Group EM home-start claim: NOT CLAIMED\n\n"
            f"{reason}\n\n"
            "This session holds no Group EM standing. Run /group-em to claim by typed entry."
        )
    return render_additional_context(entered)


def compute_context(payload: dict) -> Optional[str]:
    if payload.get("source") in _SKIP_SOURCES:
        return None

    session_id = payload.get("session_id")
    cwd = payload.get("cwd")
    if not (isinstance(session_id, str) and session_id and isinstance(cwd, str) and cwd):
        return None

    if human_entry._in_home(cwd) is not True:
        return None

    try:
        entered = reentrant_dispatch(
            "groupem.enter",
            {
                "repo_root": cwd,
                "caller_session_id": session_id,
                "entry_evidence": human_entry.HOME_START,
            },
            repo_root=Path(cwd),
        )
    except Exception:  # includes OpUnavailableError
        return None

    if not isinstance(entered, dict) or not entered:
        return None
    return _render(entered)


@register_op("hooks.group_em_home_autoclaim")
def _handler(params: dict, repo_root=None) -> dict:
    params = payload_of(params)
    try:
        additional_context = compute_context(params)
    except Exception:
        additional_context = None

    if not additional_context:
        return no_advisory()
    return context_only("SessionStart", additional_context)
