"""coordinator_core.session.claim_self_held -- "does THIS session hold the claim dir?"

One question: is the calling session the recorded holder of a
``<class>-claims/<basename>`` lock dir, under the identity an enclosing
``apply_base.session_identity()`` scope resolved. A leaf so a caller that only
needs that answer (``handoff.correct_body``) does not load ``pickup_assemble``
or ``session.claims``.

Negative spec: imports nothing from ``coordinator_core.ops``,
``coordinator_core.pickup_assemble`` or ``coordinator_core.session.claims``, and
never falls back to ``os.environ`` for identity.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.session import liveness as _liveness


def _explicitly_scoped_session_id() -> str:
    """The session id an enclosing ``apply_base.session_identity()`` scope
    resolved under its OWN rules, or ``""`` when no such scope is active.

    ``liveness.claim_held_by_me`` fails closed inside a warm-served request
    that carried no identity: ``os.environ`` there names whoever SPAWNED the
    server, so granting a mutex on it would admit every session that server
    serves. That refusal is correct and is not relaxed here. What it cannot
    see on its own is a caller which HAS already resolved identity — an
    explicit ``--session-id``, or a carried id — and entered
    ``session_identity()`` with it. ``claim_held_by_me``'s documented
    contract admits exactly that caller through ``my_sid``, and this is the
    accessor that supplies it.

    NEGATIVE SPEC — contextvar only, never an ``os.environ`` fallback. The
    fallback is the very read the warm refusal exists to reject, and adding
    it here would reintroduce the ambient grant through the parameter meant
    to close it. No active scope yields ``""``, which leaves
    ``claim_held_by_me`` on its own unchanged resolution path.
    """
    from coordinator_core.contract.apply_base import current_session_env

    scoped = current_session_env()
    for value in scoped.values():
        value = (value or "").strip()
        if value:
            return value
    return ""


def _claim_already_self_held(repo_root: Path, class_: str, basename: str) -> bool:
    """True iff THIS session is the recorded holder of the ``<class>-claims/
    <basename>`` lock dir — the same identity predicate ``compute_claim_grant``'s
    self-holder row (AC3b row 2, "you already hold this") already answers
    internally via ``liveness.claim_held_by_me``. Exposed as its own predicate
    so a ``directives[]`` builder can mark its own ``claim-artifact`` directive
    ``already_satisfied`` for idempotent same-session re-entry.

    Fixes: same-session ``pickup-assemble apply`` re-entry on a memo whose
    prior partial run already landed the `d1` claim-artifact directive used to
    hard-fail on re-apply — `apply` is explicitly designed to be re-runnable
    (module docstring § "the hold-path residue"; `directives[].already_
    satisfied` is skipped, never re-dispatched), but memo's `d1` was
    unconditionally `already_satisfied: False`, so a second `apply` in the
    SAME session called `claims.claim_artifact` again, which REJECTS a
    same-session reclaim for the memo class by design (`session.claims`
    module negative-spec) and raised, halting the whole run at `d1` — even
    though every OTHER directive gated behind it (including the terminal
    `d-action-memo`) was otherwise ready to land.

    NEVER broadens to a different session, live or dead — ``claims.
    claim_artifact``'s own EEXIST branch is untouched by this predicate and
    still denies/takes-over exactly as it does today; this exists purely so
    ``apply``'s directive layer never re-asks that primitive a question the
    brief-time snapshot already answered for THIS session, not to change what
    the primitive decides for anyone else.
    """
    claims_dir = repo_root / ".git" / "coordinator-sessions" / f"{class_}-claims" / basename
    if not claims_dir.is_dir():
        return False
    try:
        return _liveness.claim_held_by_me(
            str(claims_dir), my_sid=_explicitly_scoped_session_id(), cwd=str(repo_root)
        )
    except (OSError, ValueError):
        return False
