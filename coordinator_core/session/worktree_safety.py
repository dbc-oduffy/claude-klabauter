"""
coordinator_core.session.worktree_safety — history-rewrite safety predicate for the
shared-worktree concurrency model.

Purpose: this repo routinely runs 4-5 concurrent EM sessions in ONE shared working
tree (see project CLAUDE.md § Coordinator Operating Doctrine). A history-rewriting git
operation (``git rebase``, ``git push --force-with-lease``) mutates the local HEAD that
every live peer session's uncommitted diff is anchored to — ``--force-with-lease``
protects the REMOTE from a lost update, but says nothing about local peers. This module
answers exactly one question for a caller about to run such an operation: is it safe
w.r.t. OTHER live sessions sharing this checkout right now?

The verdict is a point-in-time read, not a durable fact: callers MUST re-resolve it
immediately before EACH destructive operation it gates, not reuse one snapshot across
a longer procedure. A caller that computes the verdict once and then performs
unrelated, potentially long-running work (sibling discovery, conflict-laden merges,
PM-attended resolution) before acting on it creates a window in which a peer session
that becomes live is invisible to the gate. See
``coordinator/bin/workday-complete-step3-consolidate.py`` for the reference pattern:
one early read for the up-front operator-visible line, then a fresh read immediately
before the rebase decision and again immediately before the force-push decision.

Spec backlink: workday-complete-ceremony-hardening spinoff, oracle finding F4
(coordinator/bin/workday-complete-step3-consolidate.py performed unconditional
rebase/force-push with no concurrency check against live local peers).

Negative-spec:
    - ``outcome="unknown"`` MUST NOT be collapsed into ``"ok"`` by any caller. Both
      ``"refused"`` and ``"unknown"`` mean "do not proceed with the history-rewriting
      form of the operation" — the only difference is whether we affirmatively saw a
      live peer (``refused``) or could not determine the live set / our own identity
      at all (``unknown``, FAIL CLOSED). A liveness-probe exception or an unresolvable
      self-identity while the live set is non-empty must read ``unknown``, never
      ``ok`` — this mirrors ``coordinator/bin/coordinator-safe-commit``'s blanket-
      commit refusal idiom (see that script's F0/F1 foreign-path subtract: an
      exception resolving the live set aborts the blanket commit rather than
      degrading to "no live siblings").
    - This predicate is about LOCAL SHARED-WORKTREE PEERS, not the remote —
      ``--force-with-lease`` already covers the remote-lost-update case and this
      module does not duplicate that check.
    - Deliberately NO environment-variable override / escape hatch. The refusal path
      degrades callers to a safe, still-useful non-rewriting operation (see
      ``workday-complete-step3-consolidate.py``'s ff-only reconcile / plain-push
      fallback), so a bypass can only ever exist to defeat the guard, never to unblock
      legitimate work.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import NamedTuple, Optional, Tuple

from coordinator_core.session import core
from coordinator_core.session import liveness as _liveness

_SENTINEL_REL = (".git", "coordinator-sessions", ".current-session-id")


class RewriteVerdict(NamedTuple):
    """Verdict for a proposed history-rewriting git operation.

    outcome: exactly one of ``"ok"``, ``"refused"``, ``"unknown"``.
    reason: human-readable one-liner suitable for printing to an operator.
    peer_session_ids: the live sessions other than self (empty for "ok").
    """

    outcome: str
    reason: str
    peer_session_ids: Tuple[str, ...]


def _resolve_self_session_id(cwd: Optional[str]) -> str:
    """CLAUDE_CODE_SESSION_ID env, falling back to the ``.current-session-id``
    sentinel file. Returns "" if neither resolves."""
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    if sid:
        return sid

    root = core.git_root(cwd)
    if not root:
        return ""
    sentinel_file = Path(root).joinpath(*_SENTINEL_REL)
    try:
        return sentinel_file.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def history_rewrite_verdict(
    cwd: Optional[str] = None, self_session_id: Optional[str] = None
) -> RewriteVerdict:
    """Decide whether a history-rewriting git operation (rebase / force-push) is safe
    to run against the shared worktree at ``cwd`` right now.

    ``self_session_id``, if passed, overrides both env-var and sentinel-file
    resolution (exists for testability — lets a caller pin identity without
    mutating the environment).

    FAIL CLOSED: any failure to establish either the live-session set or this
    session's own identity (while the live set is non-empty) returns
    ``outcome="unknown"``, which callers MUST treat exactly like ``"refused"``.
    """
    try:
        live = _liveness.live_session_ids(cwd)
    except Exception as exc:
        return RewriteVerdict(
            outcome="unknown",
            reason=f"cannot resolve live-session set ({exc}) — refusing history rewrite",
            peer_session_ids=(),
        )

    self_sid = self_session_id if self_session_id else _resolve_self_session_id(cwd)

    if not self_sid:
        if not live:
            # No live sessions at all and no identity to subtract — vacuously safe.
            return RewriteVerdict(outcome="ok", reason="no live sessions", peer_session_ids=())
        return RewriteVerdict(
            outcome="unknown",
            reason=(
                "cannot resolve this session's own identity (no CLAUDE_CODE_SESSION_ID, "
                "no .current-session-id sentinel) while other sessions are live — "
                "refusing history rewrite"
            ),
            peer_session_ids=tuple(sorted(live)),
        )

    peers = tuple(sorted(sid for sid in live if sid != self_sid))
    if not peers:
        return RewriteVerdict(outcome="ok", reason="no live peer sessions", peer_session_ids=())

    return RewriteVerdict(
        outcome="refused",
        reason=f"{len(peers)} live peer session(s) sharing this worktree: {', '.join(peers)}",
        peer_session_ids=peers,
    )
