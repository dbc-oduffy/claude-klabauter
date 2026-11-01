"""Remote session identity: a session on any box is keyed ``<box_id>:<session_id>``.

Box ids are ``[A-Za-z0-9_-]`` (``box_registry._valid_id``), so the first ``:`` separates
unambiguously. Never keyed by pid or hostname: pids are box-local and change on a mirror swap.
"""

from __future__ import annotations

from typing import Optional

from coordinator_core.group_em import box_registry

SEP = ":"


def peer_key(box_id: str, session_id: str) -> str:
    """``"<box_id>:<session_id>"``; raises ValueError for an unsafe box id or empty session id."""
    if not box_registry._valid_id(box_id):
        raise ValueError(f"invalid box id: {box_id!r}")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("empty session id")
    return box_id + SEP + session_id


def parse_key(key: str, local_id: Optional[str] = None) -> tuple[str, str]:
    """``(box_id, session_id)``; a bare session id (no valid box prefix) parses as the local box."""
    head, sep, tail = key.partition(SEP)
    if sep and tail and box_registry._valid_id(head):
        return head, tail
    return (local_id if local_id is not None else box_registry.local_box_id()), key


def rewrite_from(line: dict, local_id: Optional[str] = None) -> dict:
    """Copy of inbox ``line`` with ``from`` in the receiver's namespace.

    A sender on another box becomes ``peer_key(from_box, from_session)``; a sender on ``local_id``
    stays the bare ``from_session``. A line without a ``from_session`` or with an unsafe
    ``from_box`` gets no ``from`` key. The input is never mutated.
    """
    out = dict(line)
    session = line.get("from_session")
    if not isinstance(session, str) or not session:
        return out
    box = line.get("from_box")
    local = local_id if local_id is not None else box_registry.local_box_id()
    if box is None or box == local:
        out["from"] = session
        return out
    try:
        out["from"] = peer_key(box, session)
    except ValueError:
        pass
    return out
