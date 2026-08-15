"""
coordinator_core.ops.peer_notice_send — same-repo peer-contention notice writer
(``peer_notice.send`` op).

Purpose: disk-backed fallback for cross-session contention notices when the
harness's cross-session messaging inbox is unbound
(``coordinator_core.session.reachability.messaging_available()`` is ``False`` --
the box-wide default; see that module's docstring and
``coordinator_core.session.harness_registry``'s for the full measurement). The
gap this op closes is the FALLBACK, not the transport: ``cross-repo/`` is the
channel between repos, and there was no equivalent inside ONE repo, so two
sessions editing the same function in the shared working tree had nowhere to
tell each other. See ``docs/reference/peer-notice-channel.md``.

This is NOT a replacement for harness peer messaging -- it is the degraded path
for when ``messaging_available()`` reports ``False``, and it RE-CHECKS that flag
at send time rather than assuming permanence: a GrowthBook refresh can flip the
gate ON mid-session (``harness_registry``'s "LATE-BIND path"), so a long-lived
caller must not cache an earlier "unavailable" verdict. The check is advisory
only here -- see Negative-spec below; this op never refuses to write because
messaging became available.

Site: ``state/peer-notices/<target_session_id>/<notice_id>.json`` -- ``state/``
is this repo's authoritative substrate (``tasks/`` is swept scratch, per this
repo's CLAUDE.md), and a notice addressed to a not-yet-started peer session must
survive THIS session's own death, so it cannot live in ``tasks/`` or in-memory.
One file per notice (uuid4-hex ``notice_id``), never an append-only log --
avoids a read/append race between concurrent senders and lets a reader
(``peer_notice.check`` / the write-guard delivery seam) glob the directory
without parsing partial writes.

Addressed, not broadcast: ``target_session_id`` is a session id in this repo's
existing owner-id conventions (``claimed_by``, ``authoring_session``,
``created_by_session``, ``agent_sessions`` entries, ``subagent-share/<id>/``
directory names -- see ``coordinator_core.session.harness_registry`` /
``coordinator_core.session.reachability``, reused here rather than a second
resolver). A notice names the contended artifact by REPO-RELATIVE FILE PATH and,
where known, its ENCLOSING FUNCTION -- never a line number (this repo's
CLAUDE.md: "Cite by enclosing function, not line number" -- a hand-copied line
number is stale-by-default in a shared tree).

``from_session_id`` defaults to ``coordinator_core.ops.session_context.
resolve_current_session_id(worktree_root)`` when the caller omits it -- the same
canonical session-identity chain every other op in this package uses, not a
second one.

Delivery is NOT this op's job: a notice nobody reads is worse than none (it
manufactures a false sense the peer was told). Delivery happens at the seam a
session already walks before mutating -- see
``coordinator_core.write_guards.nudge_peer_notice_unread`` -- this op only
writes the durable record.

MUTATING op: writes coordinator substrate ONLY (``state/peer-notices/``).
Registered as ``peer_notice.send`` in ``ops/__init__.py`` and classified
``OpClass.MUTATING`` in ``authz/classification.py``.

Negative-spec:
    - Does NOT attempt to use, force, or pin harness cross-session messaging --
      that gate is remote and unforceable (``reachability.py`` /
      ``harness_registry.py``). This op is the disk-backed fallback ONLY, and
      ``messaging_available()`` is read purely to STAMP the notice with the
      state of the gate at send time (an audit fact for the reader, "harness
      messaging was up when this was sent -- prefer it next time"), never to
      gate or refuse the write.
    - Does NOT resolve or validate that ``target_session_id`` is currently
      live -- a notice may be read by a session that starts AFTER this call
      returns; that is the whole point of a disk-backed channel. No liveness
      check gates the write.
    - Does NOT overwrite another notice -- each call writes a NEW file
      (uuid4-hex ``notice_id``), so two notices to the same target never
      collide, and a notice is never mutated in place after it is written.
    - Does NOT broadcast -- a falsy/missing ``target_session_id`` is a
      validation error (``ValueError``), never silently fanned out to every
      live session.
    - Does NOT write into rag's workstate_store -- coordinator substrate only
      (dual-write ban, DR-208/DR-236).
    - Does NOT write the notice file in place -- writes a same-directory
      ``.<notice_id>.json.tmp`` then ``Path.replace()``s it onto the final
      name, so a concurrent reader never observes a partial write (the
      ``.tmp`` suffix keeps it outside every ``*.json`` glob in this
      package). Does NOT leave that ``.tmp`` file behind on failure -- a
      partial ``write_text`` or a failed ``replace`` unlinks it (best-effort)
      before re-raising, so a disk-full write doesn't leak a file into a
      directory any session can write to (Review: code-reviewer, P3
      orphaned-tmp-file finding).
    - Does NOT accept an unbounded ``target_session_id`` -- validated against
      ``ops._path_guard.safe_id`` (single path segment, no separators, no
      bare traversal token) BEFORE it is interpolated into a directory name,
      never audited after the join.

Spec backlink: dispatch brief "Build a same-repo peer-contention notice
channel" (2026-08-15); ``coordinator_core/session/harness_registry.py``,
``coordinator_core/session/reachability.py``.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops._path_guard import safe_id
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.ops.session_context import resolve_current_session_id
from coordinator_core.session import harness_registry
from coordinator_core.session.reachability import messaging_available

#: Cap on stored ``message`` length. A notice is advisory text surfaced
#: inline in a PreToolUse ``additionalContext`` block (see
#: ``write_guards.nudge_peer_notice_unread``) -- an unbounded message could
#: blow up that advisory regardless of ``_MAX_NOTICES`` bounding the notice
#: COUNT. Anything past the cap is truncated at send time, not at read time,
#: so every reader (the op and the guard) sees the same already-bounded text
#: rather than re-deciding the cut point independently.
_MAX_MESSAGE_LEN = 2000


def _notices_dir(worktree_root: Path, target_session_id: str) -> Path:
    return worktree_root / "state" / "peer-notices" / target_session_id


@register_op("peer_notice.send")
def _peer_notice_send(params: Dict[str, Any], repo_root: Optional[Path] = None) -> dict:
    """``peer_notice.send`` handler.

    Params:
        target_session_id  — required. The peer session id this notice is
                              addressed to.
        artifact_path       — required. Repo-relative path of the contended
                              file (never an absolute path, never a line
                              number).
        message              — required. Free-text notice body (e.g. "I am
                              editing this function", "your uncommitted hunks
                              here collide with mine"). Truncated to
                              ``_MAX_MESSAGE_LEN`` characters (with a
                              "...[truncated]" marker appended) if longer --
                              this op bounds notice COUNT via nothing here (see
                              the delivery guard's ``_MAX_NOTICES``) and must
                              separately bound per-notice SIZE.
        enclosing_function    — optional. The contended function/method name,
                              when known (never a line number).
        from_session_id      — optional. Defaults to the resolver's own
                              current-session-id chain when omitted.

    Returns:
        {"ok": True, "notice_path": <repo-relative path written>,
         "messaging_available": <bool, gate state at send time>}

    Raises ``ValueError`` on a missing/empty required param -- fail loud, per
    this repo's op conventions (never a silent no-op write).
    """
    target_session_id = (params.get("target_session_id") or "").strip()
    if not target_session_id:
        raise ValueError("peer_notice.send requires a non-empty target_session_id")
    if not safe_id(target_session_id):
        # Validated HERE, at param-parsing, not as a post-join containment
        # check on the resulting path -- target_session_id is interpolated
        # directly into a directory name below (`_notices_dir`), so a value
        # like "../../foo" must be rejected before it is ever joined, not
        # audited after (Review: code-reviewer, P1 path-traversal finding).
        raise ValueError(
            f"peer_notice.send: target_session_id {target_session_id!r} is not a "
            "safe id (alphanumerics, '.', '_', '-' only; no path separators)"
        )

    artifact_path = (params.get("artifact_path") or "").strip()
    if not artifact_path:
        raise ValueError("peer_notice.send requires a non-empty artifact_path")

    message = (params.get("message") or "").strip()
    if not message:
        raise ValueError("peer_notice.send requires a non-empty message")
    if len(message) > _MAX_MESSAGE_LEN:
        message = message[:_MAX_MESSAGE_LEN] + "...[truncated]"

    enclosing_function = (params.get("enclosing_function") or "").strip() or None

    worktree_root = main_worktree_root(repo_root) if repo_root else Path.cwd()

    from_session_id = (params.get("from_session_id") or "").strip() or None
    if not from_session_id:
        from_session_id = resolve_current_session_id(worktree_root)

    # Advisory-only read of the gate state -- never gates the write itself
    # (see module docstring Negative-spec). Failure here must not block the
    # send: a snapshot read error degrades to an unknown gate state, not a
    # write refusal.
    try:
        snapshot = harness_registry.snapshot()
        gate_up = messaging_available(snapshot)
    except Exception:
        gate_up = None

    notice_id = uuid.uuid4().hex
    record = {
        "notice_id": notice_id,
        "from_session_id": from_session_id,
        "target_session_id": target_session_id,
        "artifact_path": artifact_path,
        "enclosing_function": enclosing_function,
        "message": message,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "messaging_available_at_send": gate_up,
    }

    target_dir = _notices_dir(worktree_root, target_session_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    notice_path = target_dir / f"{notice_id}.json"
    # Write-tmp-then-os.replace, same directory: os.replace is atomic on both
    # POSIX and Windows within one filesystem, so a concurrent reader
    # (peer_notice_check.list_unread_notices / the delivery guard) never
    # observes a partially-written file -- this module already reasons about
    # avoiding partial-write parsing via one-file-per-notice (module
    # docstring); a bare write_text left that reasoning half-applied (Review:
    # code-reviewer, P3 un-atomic-write finding).
    tmp_path = target_dir / f".{notice_id}.json.tmp"
    try:
        tmp_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp_path.replace(notice_path)
    except Exception:
        # A partial write_text (e.g. disk full) or a failed replace must not
        # leave the `.tmp` file behind -- it is already excluded from every
        # `*.json` glob so it can't be misread, but an unreaped leak in a
        # directory any session can write to is a real defect on its own
        # (Review: code-reviewer, P3 orphaned-tmp-file finding). Best-effort:
        # the original exception is what the caller needs to see.
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    try:
        rel = notice_path.relative_to(worktree_root).as_posix()
    except ValueError:
        rel = str(notice_path)

    return {"ok": True, "notice_path": rel, "messaging_available": gate_up}
