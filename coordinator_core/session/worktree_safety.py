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

from typing import NamedTuple, Optional, Tuple

from coordinator_core.session import core
from coordinator_core.session import liveness as _liveness

# _SENTINEL_REL (".git", "coordinator-sessions", ".current-session-id") REMOVED
# (KS-3, 2026-08-07): the sentinel tier it fed was unsound under concurrency
# (documented last-writer-wins across concurrent sessions sharing one
# worktree — coordinator_core/bash_guards/guard_inprocess_search.py ~L84)
# AND its sole writer (session-init.py, the DoE-claude SessionStart hook)
# was deleted by PM directive 2026-07-15 — no production writer survives.
# A stale sid used to subtract nothing from the live peer set here (over-
# refusal, noisy but fail-closed-safe); removal makes resolve_self_session_id
# honestly return "" instead, which still routes to the "unknown" (fail
# closed) branch below whenever the live set is non-empty — no refuse->allow
# flip.


class RewriteVerdict(NamedTuple):
    """Verdict for a proposed history-rewriting git operation.

    outcome: exactly one of ``"ok"``, ``"refused"``, ``"unknown"``.
    reason: human-readable one-liner suitable for printing to an operator.
    peer_session_ids: the live sessions other than self (empty for "ok").
    """

    outcome: str
    reason: str
    peer_session_ids: Tuple[str, ...]


# --- Branch-mutation operation kinds (C1) -------------------------------
#
# ``branch_mutation_verdict`` answers ONE question -- "do live peers share
# this worktree?" -- but the ANSWER it should give depends on which mutation
# the caller is about to perform. Before C1 the predicate answered one
# question for three structurally different mutations and refused all of
# them identically. The axis below is REQUIRED and KEYWORD-ONLY so no caller
# can inherit a permissive default by omission.
#
# Only FRESH_CUT_AT_HEAD is narrowed. Every other kind takes the unchanged
# refuse-under-peers path.

#: Create-and-switch a NEW branch AT CURRENT HEAD. Content-neutral: no file
#: touched, no index entry changed, HEAD does not move. Requires
#: ``current_branch`` and is permitted under live peers on ``main`` ONLY.
FRESH_CUT_AT_HEAD = "FRESH_CUT_AT_HEAD"
#: Checking out a DIFFERENT existing commit. Moves HEAD under every peer.
CHECKOUT_EXISTING = "CHECKOUT_EXISTING"
#: Renaming a branch with a remote delete. Genuinely destructive.
RENAME_WITH_REMOTE_DELETE = "RENAME_WITH_REMOTE_DELETE"
#: Catch-all for a branch mutation whose content-neutrality this predicate
#: cannot establish from its own inputs -- a cut bundled with a reset, or a
#: cut merely PRESCRIBED to a caller that controls how it is performed. Not
#: named in the originating plan; added because the two pre-existing
#: non-ceremony call sites (`coordinator/bin/merge-recovery-and-tag-cut.py`
#: cuts a recovery branch AND hard-resets main; `pickup_assemble`'s
#: `compute_branch_gate` prescribes an unqualified cut) are neither of the
#: three hazardous kinds by name, and mapping them onto one of those would
#: have been a lie in the argument. Behaviour is identical to the hazardous
#: kinds: refuse under peers.
UNQUALIFIED_BRANCH_CUT = "UNQUALIFIED_BRANCH_CUT"

_BRANCH_MUTATION_KINDS = frozenset(
    {
        FRESH_CUT_AT_HEAD,
        CHECKOUT_EXISTING,
        RENAME_WITH_REMOTE_DELETE,
        UNQUALIFIED_BRANCH_CUT,
    }
)


class BranchMutationVerdict(NamedTuple):
    """Verdict for a proposed branch-cutting operation (creating a NEW
    branch, as opposed to committing on one already checked out).

    outcome: exactly one of ``"ok"``, ``"refused"``, ``"unknown"``.
    reason: human-readable one-liner suitable for printing to an operator,
      naming peers and the branch each is on when ``outcome != "ok"``.
    peers: ``(session_id, branch)`` pairs for the live peers other than self
      (empty for "ok"). ``branch`` is ``None`` when that peer's meta.json
      carries no ``branch`` field.
    """

    outcome: str
    reason: str
    peers: Tuple[Tuple[str, Optional[str]], ...]


def branch_mutation_verdict(
    cwd: Optional[str] = None,
    self_session_id: Optional[str] = None,
    *,
    operation: str,
    current_branch: Optional[str] = None,
) -> BranchMutationVerdict:
    """Decide whether cutting a NEW branch in the shared worktree at ``cwd``
    is safe right now, i.e. whether any OTHER live session is sharing this
    checkout.

    Cutting a branch switches the working tree under every live peer
    session in this repo's shared-worktree concurrency model (see this
    module's docstring) — a strictly worse hazard than the history-rewrite
    case ``history_rewrite_verdict`` already covers, because it is not even
    a destructive git operation on shared history: an ordinary `git
    checkout -b` on a workday with 4-5 concurrent EM sessions is enough to
    pull every peer's uncommitted diff onto a branch its own session never
    asked for.

    ``self_session_id``, if passed, overrides both env-var and
    sentinel-file resolution (testability, mirrors
    ``history_rewrite_verdict``).

    FAIL CLOSED, mirroring ``history_rewrite_verdict``: any failure to
    resolve either the live-session set or this session's own identity
    (while the live set is non-empty) returns ``outcome="unknown"``, which
    callers MUST treat exactly like ``"refused"`` — the cautious answer
    here is to NOT prescribe a branch cut whose safety cannot be proven,
    not to default to "no peers".

    Negative-spec:
        - ``outcome="unknown"`` MUST NOT be collapsed into ``"ok"`` by any
          caller, for the same reason ``history_rewrite_verdict`` forbids
          it: the difference between ``refused`` and ``unknown`` is only
          whether a live peer was affirmatively observed or the live set
          could not be resolved at all — both mean "do not cut a branch".
        - This predicate answers ONLY the branch-mutation question. It does
          not gate committing on an already-checked-out branch, and it does
          not duplicate ``history_rewrite_verdict``'s history-rewrite
          question — a caller needing both must call both.
        - ``operation`` is REQUIRED and KEYWORD-ONLY. There is deliberately
          no default: a default would be inherited silently by every future
          caller, and the only safe default (the hazardous path) would make
          the narrowing unreachable while a permissive one would hand every
          caller the relaxation. Callers name the mutation they are about
          to perform; the predicate answers for that mutation.
        - ``operation=FRESH_CUT_AT_HEAD`` is narrowed ONLY when
          ``current_branch == "main"``. A detached HEAD and a zero-ahead
          non-span branch are NOT "on main" and route to the unchanged
          refusal — widening this admission set to match
          ``session_ensure_branch``'s ceremony-time set would silently
          extend a PM-authorised reversal past what was authorised, on a
          path that now fires on every boot.

    Empirical proof carried per C1 (negative-spec — read this before
    widening anything):
        A fresh cut at current HEAD while on ``main`` is CONTENT-NEUTRAL. It
        was reproduced in a scratch repo with a peer's modified-tracked,
        untracked, and staged files all present across the cut: all three
        survived and HEAD did not move. That proof is NARROWER than the
        doctrine it bumps into and does not rebut it. The
        ``SHARED-TREE-BRANCH-NOT-ISOLABLE`` hazard is IDENTITY SURPRISE —
        the peer's NEXT commit, on unrelated files, lands on a branch it
        never checked out. The experiment says nothing about that. What
        authorises accepting that residual hazard is the PM ruling of
        2026-08-18 ("we cut automatically if we're on main"), not this
        proof. Do not cite the experiment as though it discharged the
        doctrine.
    """
    if operation not in _BRANCH_MUTATION_KINDS:
        raise ValueError(
            f"unknown branch-mutation operation {operation!r}; "
            f"expected one of {sorted(_BRANCH_MUTATION_KINDS)}"
        )

    try:
        verdicts = _liveness.live_session_verdicts(cwd)
    except Exception as exc:
        return BranchMutationVerdict(
            outcome="unknown",
            reason=f"cannot resolve live-session set ({exc}) — refusing branch cut",
            peers=(),
        )

    live_ids = [sid for sid, (live, _basis, _age) in verdicts.items() if live]

    self_sid = self_session_id if self_session_id else resolve_self_session_id(cwd)

    if not self_sid:
        if not live_ids:
            return BranchMutationVerdict(outcome="ok", reason="no live sessions", peers=())
        return BranchMutationVerdict(
            outcome="unknown",
            reason=(
                "cannot resolve this session's own identity (no CLAUDE_CODE_SESSION_ID) "
                "while other sessions are live — "
                "refusing branch cut"
            ),
            peers=tuple((sid, _peer_branch(sid, cwd)) for sid in sorted(live_ids)),
        )

    peer_ids = sorted(sid for sid in live_ids if sid != self_sid)
    if not peer_ids:
        return BranchMutationVerdict(outcome="ok", reason="no live peer sessions", peers=())

    peers = tuple((sid, _peer_branch(sid, cwd)) for sid in peer_ids)
    named = ", ".join(f"{sid} (on {branch or 'unknown branch'})" for sid, branch in peers)

    if operation == FRESH_CUT_AT_HEAD and current_branch == "main":
        return BranchMutationVerdict(
            outcome="ok",
            reason=(
                f"content-neutral fresh cut at HEAD while on main; "
                f"{len(peers)} live peer session(s) sharing this worktree are "
                f"unaffected (no file touched, no index entry changed, HEAD "
                f"unmoved): {named}"
            ),
            peers=peers,
        )

    return BranchMutationVerdict(
        outcome="refused",
        reason=f"{len(peers)} live peer session(s) sharing this worktree: {named}",
        peers=peers,
    )


def _peer_branch(sid: str, cwd: Optional[str]) -> Optional[str]:
    """meta.json's ``branch`` field for ``sid``, or ``None`` when absent —
    evidence-only, never re-derived from the live git checkout (a peer's
    recorded branch can legitimately differ from the CURRENT checkout in a
    shared worktree, and this predicate must not paper over that by
    substituting the caller's own `git branch --show-current`)."""
    sdir = core.session_dir(sid, cwd)
    if not sdir:
        return None
    return core.read_meta_field(sdir, "branch") or None


def resolve_self_session_id(cwd: Optional[str]) -> str:
    """Delegates to ``core.resolve_session_id`` (KS-6, 2026-08-07): the full
    3-tier ``SESSION_ENV_PRECEDENCE`` ladder (``COORDINATOR_SESSION_ID``,
    ``CLAUDE_SESSION_ID``, ``CLAUDE_CODE_SESSION_ID``), widened from the
    prior ``CLAUDE_CODE_SESSION_ID``-only read — this module's own
    ``branch_mutation_verdict``/``history_rewrite_verdict`` callers gate on
    "can we resolve OUR OWN identity", and narrowing that to one tier while
    the canonical resolver reads three is exactly the guard/op precedence
    mismatch ``core.SESSION_ENV_PRECEDENCE``'s docstring records as a prior
    break-class defect. Returns "" if unresolved.

    The ``.current-session-id`` sentinel-file fallback that used to sit here
    was REMOVED (KS-3, 2026-08-07) — see the module-level comment by the
    former ``_SENTINEL_REL`` constant for the rationale. ``cwd`` is accepted
    for call-site compatibility but unused — ``core.resolve_session_id``'s
    tiers are all env-var reads."""
    return core.resolve_session_id(cwd)


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

    self_sid = self_session_id if self_session_id else resolve_self_session_id(cwd)

    if not self_sid:
        if not live:
            # No live sessions at all and no identity to subtract — vacuously safe.
            return RewriteVerdict(outcome="ok", reason="no live sessions", peer_session_ids=())
        return RewriteVerdict(
            outcome="unknown",
            reason=(
                "cannot resolve this session's own identity (no CLAUDE_CODE_SESSION_ID) "
                "while other sessions are live — "
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
