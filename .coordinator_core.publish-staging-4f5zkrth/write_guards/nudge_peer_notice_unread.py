"""coordinator_core.write_guards.nudge_peer_notice_unread — advisory guard.

Delivery seam for the same-repo peer-contention notice channel
(``coordinator_core.ops.peer_notice_send`` / ``peer_notice_check``). A notice
nobody reads is worse than none -- it manufactures a false sense that the peer
was told (dispatch brief, "Delivered on the path a session already walks").
This guard is that path: it fires at the PreToolUse Write/Edit/MultiEdit/
NotebookEdit boundary -- a point every session walks BEFORE mutating -- and
surfaces any unread notice addressed to the CALLING session (``payload
["session_id"]``), before the write proceeds.

This is a NEW guard, not a port of a DoE reference ``.sh`` hook -- there is no
same-repo peer-notice concept in the retired bash-hook corpus. See
``docs/reference/peer-notice-channel.md`` for the channel's full design and
why it exists (harness cross-session messaging is remote-gated and
unforceable -- ``coordinator_core.session.reachability`` /
``coordinator_core.session.harness_registry``).

CLASS is "advisory", deliberately -- never a hard-deny. A peer notice is
information, not a permission decision: the write always proceeds, and the
guard only adds ``additionalContext`` naming the contended artifact(s) and
the sender, per design-as-offers (global CLAUDE.md § Implementation
Standards).

Deliberately NOT identity-scoped to the artifact being written: this guard
surfaces EVERY unread notice addressed to the calling session, not only
notices whose ``artifact_path`` matches the file under edit. A narrower
"only if this exact file collides" gate would silently drop a notice about a
DIFFERENT contended file the session has not yet read -- worse than a
slightly-noisier surface, per the channel's own "delivered, not merely
sent" design goal.

Delivery side-effect: once surfaced, each notice file is MOVED from
``state/peer-notices/<session_id>/<id>.json`` into a sibling
``.delivered/<id>.json`` -- so the same notice is not repeated on every
subsequent write. This move is wrapped in its own ``try/except`` (INTERFACE.md
rule 4: preserve side-effects, wrapped so a failure can NEVER flip the
decision) -- an ``OSError`` moving a file never turns an advisory into a deny,
and never prevents the ``additionalContext`` from being returned for THIS
call (the in-memory list is already built before the move is attempted).

PRIORITY = 101, in the ADVISORY PHASE's own PRIORITY namespace (see
docs/wiki/write-guard-priority-bands.md, the SSOT for this convention). This
guard's firing CONDITION is rare by construction -- it returns ``None`` on
every call where the session has zero unread notices, i.e. the overwhelming
majority of writes -- so the same-surface collision rule (does a lower-
numbered advisory already win the single slot on the SAME write) matters only
in the narrow case both this guard AND another advisory would fire on the
same write. Given that rarity, this sits just below ``validate_frontmatter_
schema_advisory`` (100, a content-shape gate on a different surface) and
above ``nudge_em_code_dispatch`` (105) — a peer-contention notice is safety-
relevant (git-history-adjacent: the incident this exists to prevent was two
sessions editing the same function with incompatible, uncommitted designs)
and should win the slot over a narrower content-specific advisory on the rare
write where both would otherwise co-match.

MATCHERS covers all four mutating tools ("Write", "Edit", "MultiEdit",
"NotebookEdit") -- a notice may be addressed generically (no specific
``artifact_path`` collision requirement, see above), so it must not be
narrower than the full mutating surface.

Negative-spec:
    - Does NOT block/deny anything -- CLASS is "advisory"; see CLASS
      rationale above.
    - Does NOT resolve ``session_id`` a second way -- reads
      ``payload.get("session_id")`` directly, the same field every other
      write_guards module reads from the PreToolUse payload (INTERFACE.md).
      Does NOT call ``coordinator_core.ops.session_context.
      resolve_current_session_id`` -- that resolver is for an OP handler with
      no PreToolUse payload; this guard already has the harness-supplied
      ``session_id`` in hand.
    - Does NOT build a second notice-scan -- reuses
      ``coordinator_core.ops.peer_notice_check.list_unread_notices`` (the
      SAME scan the ``peer_notice.check`` op uses), never a second glob.
    - Does NOT attempt to use, force, or pin harness cross-session messaging
      -- unrelated to this guard's concern; see ``peer_notice_send``'s module
      docstring for that gate's own rationale.
    - Never raises: any unexpected input shape, unreadable directory, or
      failed move returns ``None``/an advisory with no exception escaping --
      matching every other write_guards module's fail-open contract.
    - Bounded: surfaces at most ``_MAX_NOTICES`` notices per call (oldest-
      ``created_at``-first, matching ``list_unread_notices``'s own order,
      which sorts on the record's ``created_at`` field rather than the
      random ``uuid4`` filename -- see that function's docstring) -- a
      pathological backlog of unread notices renders one bounded message, not
      an unbounded wall of text.
    - Does NOT accept an unvalidated ``session_id`` -- ``payload["session_id"]``
      is checked against ``ops._path_guard.safe_id`` before being handed to
      ``list_unread_notices`` (which interpolates it into a directory name).
      An unsafe value returns ``None`` here (never a raise -- this guard's
      fail-open contract, see above), not a post-join containment check on
      the resulting path.

Spec backlink: dispatch brief "Build a same-repo peer-contention notice
channel" (2026-08-15); ``coordinator_core.ops.peer_notice_send``,
``coordinator_core.ops.peer_notice_check``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.ops._path_guard import safe_id
from coordinator_core.ops.peer_notice_check import list_unread_notices

CLASS = "advisory"
MATCHERS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
PRIORITY = 101  # advisory-phase band; see module docstring for the same-surface
# collision reasoning (docs/wiki/write-guard-priority-bands.md)

_MAX_NOTICES = 5


def _repo_root(cwd: Optional[str]) -> Path:
    """Best-effort repo root -- ``cwd`` if it (or an ancestor, bounded) holds
    ``.git``, else ``cwd`` itself. No subprocess, no ``git`` invocation --
    mirrors ``nudge_terminal_artifact_edit._repo_root`` exactly.

    The upward walk itself is NOT ``lifecycle.main_worktree_root``'s job --
    that helper takes an already-known git-common-dir (or worktree-root) path
    and does not search for one, and this guard has only a PreToolUse
    ``cwd`` that may be a subdirectory. But once the walk finds a directory
    holding ``.git``, resolution of "the main worktree root from here" IS
    exactly what ``main_worktree_root`` already does for the ops
    (``peer_notice_send`` / ``peer_notice_check``) -- so the found directory
    is handed to it rather than returned as-is, closing the previous
    independent-reimplementation gap (Review: code-reviewer, P2
    repo-root-divergence finding) for the common case where a session's cwd
    sits under the main worktree root. ``main_worktree_root`` raises
    ``ValueError`` only on a layout its own negative-spec excludes (bare
    repo, unusual ``--separate-git-dir``); this guard's blanket
    ``except Exception`` in ``check()`` already covers that without a local
    ``try`` here.
    """
    base = Path(cwd) if cwd else Path.cwd()
    probe = base
    for _ in range(8):
        if (probe / ".git").exists():
            return main_worktree_root(probe)
        parent = probe.parent
        if parent == probe:
            break
        probe = parent
    return base


def _format_notice(notice: dict) -> str:
    sender = notice.get("from_session_id") or "(unknown sender)"
    artifact = notice.get("artifact_path") or "(no artifact path given)"
    fn = notice.get("enclosing_function")
    location = f"{artifact} :: {fn}()" if fn else artifact
    message = notice.get("message") or ""
    return f"  - from {sender} — {location}\n    {message}"


def _deliver(notice: dict) -> None:
    """Move a surfaced notice into ``.delivered/`` so it is not repeated.

    Failure here is swallowed -- a stuck/undeliverable move must never flip
    this guard's decision or suppress the ``additionalContext`` already
    built for this call (see module docstring)."""
    path = notice.get("_path")
    if path is None or not isinstance(path, Path):
        return
    try:
        delivered_dir = path.parent / ".delivered"
        delivered_dir.mkdir(parents=True, exist_ok=True)
        path.replace(delivered_dir / path.name)
    except OSError:
        pass


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        tool_name = payload.get("tool_name") or ""
        if tool_name not in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            return None

        session_id = (payload.get("session_id") or "").strip()
        if not session_id:
            return None
        if not safe_id(session_id):
            # Unsafe id (path separators, "..", etc.) -- session_id is
            # interpolated into a directory name by list_unread_notices, so
            # this must be rejected here rather than audited post-join
            # (Review: code-reviewer, P1 path-traversal finding). An advisory
            # guard never raises into the caller's write -- an unusable id
            # just means "nothing to surface," same as no notices at all.
            return None

        repo_root = _repo_root(payload.get("cwd"))
        notices = list_unread_notices(repo_root, session_id)
        if not notices:
            return None

        surfaced = notices[:_MAX_NOTICES]
        remaining = len(notices) - len(surfaced)

        lines = [_format_notice(n) for n in surfaced]
        overflow = f"\n  ...and {remaining} more unread notice(s)." if remaining > 0 else ""

        reason = (
            "PEER NOTICE: unread contention notice(s) addressed to this session "
            f"(harness cross-session messaging unavailable -- see "
            f"docs/reference/peer-notice-channel.md):\n"
            + "\n".join(lines)
            + overflow
        )

        for notice in surfaced:
            _deliver(notice)

        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": reason,
            }
        }
    except Exception:
        return None
