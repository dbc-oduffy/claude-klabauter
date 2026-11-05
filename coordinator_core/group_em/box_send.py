"""Cross-box send: one verified JSONL line into a registered box's inbox, and the read half.

Channel: ``<box root>/state/group-em/box/inbox.jsonl``, one line per message
``{ts, from_box, to_session, text}`` plus ``from_session`` when the sender names itself. ``send`` resolves the target root through
``box_registry.box_root`` and appends with ``admission_post._append_verified``; delivery means
present at the inbox path, not read by the session. ``read_inbox`` runs on the target box.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from coordinator_core._settings_home import settings_home
from coordinator_core.group_em import box_registry, remote_identity
from coordinator_core.group_em.admission_post import _append_verified

MAX_TEXT = 8192


def inbox_path(home: Optional[Path] = None) -> Path:
    """The inbox file of the box rooted at settings-home ``home`` (default: this box)."""
    base = Path(home) if home is not None else settings_home()
    return base / "state" / "group-em" / "box" / "inbox.jsonl"


def send(
    box_id: str,
    to_session: str,
    text: str,
    directory: Optional[Path] = None,
    now: Optional[float] = None,
    from_session: Optional[str] = None,
    from_box: Optional[str] = None,
) -> dict:
    """Append one line addressed to ``to_session`` into box ``box_id``'s inbox; return a reply.

    ``directory`` is the primary's ``state/group-em`` dir holding the registry. Reply:
    ``{sent: False, reason}`` with ``unknown_box``, ``bad_session`` (empty ``to_session``),
    ``write_failed:*``, ``short_write`` or ``not_found_at_path``; else ``{sent: True, box_id, path}``.
    ``text`` is truncated to ``MAX_TEXT`` characters. Stamps ``from_box`` from this box's identity
    and ``from_session`` when given. ``from_box`` overrides the stamp: the caller must have
    authenticated it (``box_key.verify``); this function trusts it.
    """
    root = box_registry.box_root(box_id, directory)
    if root is None:
        return {"sent": False, "reason": "unknown_box"}
    if not isinstance(to_session, str) or not to_session:
        return {"sent": False, "reason": "bad_session"}
    line = {
        "ts": time.time() if now is None else now,
        "from_box": from_box
        or box_registry.local_box_id(
            Path(directory).parent.parent if directory is not None else None
        ),
        "to_session": to_session,
        "text": str(text)[:MAX_TEXT],
    }
    if isinstance(from_session, str) and from_session:
        line["from_session"] = from_session
    path = inbox_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"sent": False, "reason": f"write_failed:{type(exc).__name__}:{exc}"}
    data = (json.dumps(line, separators=(",", ":")) + "\n").encode("utf-8")
    reason = _append_verified(path, data)
    if reason is not None:
        return {"sent": False, "reason": reason}
    return {"sent": True, "box_id": box_id, "path": str(path)}


def read_inbox(session_id: str, home: Optional[Path] = None) -> list[dict]:
    """The inbox lines addressed to ``session_id`` on the box at ``home``, oldest first.

    Malformed lines are skipped; a missing inbox reads as empty. Each line carries ``from`` in
    this box's namespace when it has a ``from_session`` (``remote_identity.rewrite_from``).
    """
    local_id = box_registry.local_box_id(home)
    try:
        with open(inbox_path(home), "r", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        return []
    out: list[dict] = []
    for ln in raw.splitlines():
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("to_session") == session_id:
            out.append(remote_identity.rewrite_from(rec, local_id))
    return out
