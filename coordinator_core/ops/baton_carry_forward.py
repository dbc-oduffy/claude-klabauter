"""
coordinator_core.ops.baton_carry_forward -- append to the live baton's
``carry_forward`` list.

WHAT THIS IS FOR. Compaction is lossy in a way a forward-thinking artifact is
not. A session that can see a compaction wave coming has, until now, had
exactly one way to make its awareness durable: author a handoff. On a cloud box
that route is not merely expensive, it is unavailable -- there is no ``/clear``,
and passing a baton means a PR merge, a new session and a re-point. Sessions
there ride compaction by design, so the ceremony built for the attended case
becomes a nag with no available remedy.

This op is the cheap alternative: append what your post-compaction self will
need to the baton that ALREADY EXISTS for this session, minted at its birth.
No new artifact, no commit, no ceremony.

DISCOVERABILITY IS THE POINT, NOT A NICETY. A session very often does not know
it has a baton -- it was minted on its behalf, possibly hundreds of turns ago,
and nothing has mentioned it since. An affordance nobody remembers is not an
affordance, so the return value always names ``baton_path`` and the running
``count``, and the context-pressure advisory names this op at the moment it is
needed rather than assuming the session recalls it exists.

SCOPE, STATED PLAINLY. The baton lives at
``<git-common-dir>/coordinator-sessions/<sid>/baton.json`` -- inside ``.git/``,
never pushed. A note therefore survives COMPACTION (same VM, same session) but
NOT the reclamation of a cloud VM. That is the right scope for its purpose:
the reader it is written for is this session, after the wave. A note that must
outlive the session belongs in a committed artifact, and this op is not that.

NEGATIVE SPEC:
  - Does NOT create the session directory (``store.merge_baton`` never does;
    a missing directory no-ops with an advisory rather than minting one).
  - Does NOT promote, close, or otherwise advance the baton's lifecycle -- a
    note is an addition to a live journal, not a transition of it.
  - Does NOT deduplicate semantically. Byte-identical notes collapse (the
    store's dedup-extend); two different phrasings of one idea both persist,
    which is the correct bias for a lossy-compaction hedge.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.session_context import resolve_current_session_id
from coordinator_core.session_baton import store

_LOG = logging.getLogger(__name__)

#: Guards against a caller pasting an entire transcript into the journal. The
#: baton is read on every `UserPromptSubmit` by DoE's announce hook, so its size
#: is on a hot path; a note that needs more than this is a document, not a note.
MAX_NOTE_CHARS = 2000


def append_note(note: str, session_id: Optional[str] = None,
                cwd: Optional[str] = None) -> dict:
    """Append ``note`` to the live baton's ``carry_forward`` list.

    Returns ``{ok, count, baton_path, session_id, reason}``. Never raises:
    this is an advisory affordance on the context-pressure path, and a record
    write that blocks its caller is worse than a note that did not land.
    """
    empty = {"ok": False, "count": 0, "baton_path": None,
             "session_id": session_id, "reason": None}

    if not isinstance(note, str) or not note.strip():
        return dict(empty, reason="note must be a non-empty string")
    note = note.strip()
    if len(note) > MAX_NOTE_CHARS:
        return dict(empty, reason=f"note exceeds {MAX_NOTE_CHARS} chars "
                                  f"({len(note)}); write a document instead")

    sid = session_id or resolve_current_session_id(cwd)
    if not sid:
        return dict(empty, reason="no resolvable session id")

    try:
        merged = store.merge_baton(sid, cwd, carry_forward=[note])
    except Exception:  # pragma: no cover - store is itself fail-open
        _LOG.warning("baton.carry_forward: merge failed for %s", sid, exc_info=True)
        return dict(empty, session_id=sid, reason="baton merge failed")

    if merged is None:
        return dict(empty, session_id=sid,
                    reason="no session directory; note not recorded")

    path = store.baton_path(sid, cwd)
    return {
        "ok": True,
        "count": len(merged.get("carry_forward") or []),
        "baton_path": str(path) if path else None,
        "session_id": sid,
        "reason": None,
    }


def read_notes(session_id: Optional[str] = None,
               cwd: Optional[str] = None) -> dict:
    """The other half of the affordance: a session resuming after compaction
    has to be able to READ what its pre-compaction self left, or the write leg
    was pointless."""
    sid = session_id or resolve_current_session_id(cwd)
    if not sid:
        return {"ok": False, "notes": [], "baton_path": None, "session_id": None}
    record = store.read_baton(sid, cwd)
    path = store.baton_path(sid, cwd)
    return {
        "ok": True,
        "notes": list(record.get("carry_forward") or []),
        "baton_path": str(path) if path else None,
        "session_id": sid,
    }


@register_op("baton.carry_forward")
def _carry_forward(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC ``baton.carry_forward`` handler.

    Params: ``note`` (str, required), ``session_id`` (str, optional).
    """
    return append_note(
        params.get("note", ""),
        session_id=params.get("session_id"),
        cwd=str(repo_root) if repo_root else None,
    )


@register_op("baton.carry_forward_read")
def _carry_forward_read(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC ``baton.carry_forward_read`` handler. Params: ``session_id``
    (str, optional)."""
    return read_notes(
        session_id=params.get("session_id"),
        cwd=str(repo_root) if repo_root else None,
    )
