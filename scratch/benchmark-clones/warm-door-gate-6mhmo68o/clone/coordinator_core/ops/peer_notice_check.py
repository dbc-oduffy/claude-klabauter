"""
coordinator_core.ops.peer_notice_check — same-repo peer-contention notice reader
(``peer_notice.check`` op).

Purpose: read-only listing of unread notices addressed to a session, written by
``coordinator_core.ops.peer_notice_send``. Companion read side of the disk-backed
peer-notice fallback channel -- see ``docs/reference/peer-notice-channel.md`` and
``peer_notice_send``'s module docstring for the full "why" (harness cross-session
messaging is remote-gated and unforceable; this is the same-repo fallback).

COMPUTE_ONLY — reads ``state/peer-notices/<session_id>/*.json`` and returns the
parsed list; never writes, deletes, or moves a notice file. Marking a notice
"delivered" (moving it out of the unread set) is the DELIVERY seam's job
(``coordinator_core.write_guards.nudge_peer_notice_unread``), not this op's --
kept separate so a caller can list notices speculatively (e.g. an operator
running a status check) without side-effecting the unread set.

Unread set: every ``*.json`` file directly inside
``state/peer-notices/<session_id>/`` -- a delivered notice is moved into a
sibling ``.delivered/`` subdirectory by the write-guard delivery seam, so it is
excluded here by construction (this function only globs the parent directory,
non-recursively).

Negative-spec:
    - Does NOT mutate any file -- pure read.
    - Does NOT recurse into ``.delivered/`` or any other subdirectory --
      non-recursive glob only.
    - Does NOT raise when the target directory is absent (no notices ever sent
      to this session) -- returns an empty list, same as every other
      graceful-absent read op in this package (``goals_match``,
      ``initiatives_serve``).
    - A malformed/unparseable notice file is skipped (quarantined), not raised
      -- mirrors ``goals_match._collect_goals``'s quarantine discipline.

Spec backlink: see ``coordinator_core.ops.peer_notice_send`` module docstring.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops._path_guard import safe_id
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.ops.session_context import resolve_current_session_id

#: Sentinel sort key for a missing/malformed ``created_at`` -- sorts before
#: every real timestamp (oldest), so such a record still surfaces rather than
#: being silently starved to the tail by a ``_MAX_NOTICES``-bounded caller.
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _sort_key(record: dict) -> datetime:
    """Parse ``record["created_at"]`` to an aware ``datetime`` for sorting.

    Sorts on the parsed INSTANT, not the raw string -- a lexicographic string
    sort is only correct when every writer uses one fixed offset (Review:
    code-reviewer, P3 UTC-dependency finding). A naive result is normalized
    to UTC so a future non-UTC writer still orders correctly instead of
    silently misordering; anything that fails to parse (missing, non-string,
    malformed) falls back to ``_EPOCH`` rather than raising -- one bad record
    must not take out the whole read path.
    """
    raw = record.get("created_at")
    if isinstance(raw, str) and raw:
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return _EPOCH
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    return _EPOCH


def _notices_dir(worktree_root: Path, session_id: str) -> Path:
    return worktree_root / "state" / "peer-notices" / session_id


def list_unread_notices(worktree_root: Path, session_id: str) -> List[dict]:
    """Return every unread notice addressed to ``session_id``, oldest-``created_at``-first.

    Shared core reused by both the ``peer_notice.check`` op handler below and
    ``coordinator_core.write_guards.nudge_peer_notice_unread`` (which must NOT
    reimplement this scan a second time).

    Ordering is on the record's own ``created_at`` field, NOT the filename --
    ``notice_id`` is a ``uuid.uuid4().hex`` (``peer_notice_send.py``), which is
    random and carries no chronological signal. Sorting by filename previously
    made "oldest first" a docstring claim the code could not deliver, and with
    ``_MAX_NOTICES`` bounding what a caller ever sees, a backlog past that
    bound would silently surface an arbitrary subset rather than the oldest
    (Review: code-reviewer, P2 false-ordering finding).

    Sorting is on a PARSED ``datetime``, not the raw ``created_at`` string --
    a lexicographic string sort is only equivalent to sorting by instant when
    every value shares one offset, which held only by accident of
    ``peer_notice_send`` being the sole writer (Review: code-reviewer, P3
    UTC-dependency finding). ``_sort_key`` parses via
    ``datetime.fromisoformat`` and normalizes a naive result to UTC, so a
    future writer emitting a local/naive timestamp still sorts correctly
    relative to the rest rather than silently misordering. A record whose
    ``created_at`` is missing, non-string, or fails to parse sorts FIRST
    (oldest) via a sentinel epoch rather than raising -- consistent with this
    function's existing quarantine-not-raise discipline for a malformed file,
    and it still surfaces rather than being silently starved to the tail by a
    bounded caller.
    """
    target_dir = _notices_dir(worktree_root, session_id)
    if not target_dir.is_dir():
        return []

    try:
        entries = list(target_dir.glob("*.json"))
    except OSError:
        return []

    notices = []
    for entry in entries:
        try:
            text = entry.read_text(encoding="utf-8")
            record = json.loads(text)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(record, dict):
            continue
        record = dict(record)
        record["_path"] = entry
        notices.append(record)

    notices.sort(key=_sort_key)
    return notices


@register_op("peer_notice.check")
def _peer_notice_check(params: Dict[str, Any], repo_root: Optional[Path] = None) -> dict:
    """``peer_notice.check`` handler.

    Params:
        session_id — optional. Defaults to the resolver's own current-session-id
                     chain (``coordinator_core.ops.session_context.
                     resolve_current_session_id``) when omitted, i.e. "what is
                     addressed to ME". A caller-supplied value must be a safe
                     single path segment (``ops._path_guard.safe_id``) -- it is
                     interpolated directly into ``state/peer-notices/<id>/``,
                     so an unsafe value (e.g. containing ``..`` or a path
                     separator) is a ``ValueError``, not resolved.

    Returns:
        {"notices": [{notice_id, from_session_id, target_session_id,
                       artifact_path, enclosing_function, message, created_at,
                       messaging_available_at_send}, ...]}

        Each returned dict omits the internal ``_path`` field used by callers
        that need the on-disk location (this op's own return value is JSON-RPC
        wire data, never a ``Path`` object).
    """
    worktree_root = main_worktree_root(repo_root) if repo_root else Path.cwd()

    session_id = (params.get("session_id") or "").strip() or None
    if session_id is not None and not safe_id(session_id):
        # Validated at param-parsing, before interpolation into a directory
        # name -- see peer_notice_send's identical rationale (Review:
        # code-reviewer, P1 path-traversal finding). Fail loud: this repo's
        # op convention is never a silent no-op write/read.
        raise ValueError(
            f"peer_notice.check: session_id {session_id!r} is not a safe id "
            "(alphanumerics, '.', '_', '-' only; no path separators)"
        )
    if not session_id:
        session_id = resolve_current_session_id(worktree_root)
    if not session_id:
        return {"notices": []}

    notices = list_unread_notices(worktree_root, session_id)
    wire_notices = [{k: v for k, v in n.items() if k != "_path"} for n in notices]
    return {"notices": wire_notices}
