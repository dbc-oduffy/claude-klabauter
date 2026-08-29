"""Authorship layer: did a human author this write, as strongly as this
harness can tell?

This module exposes one predicate — :func:`authorship_verdict` — for a
caller (the ask-the-PM step's grant-routing resolver) that needs to know
whether the process asking for a decision is a human-driven session or an
agent-driven one, and to REFUSE rather than guess when it cannot tell.

It reuses the existing harness-ancestor machinery from
``coordinator_core.session.core`` — ``_find_windows_claude_ancestor``, on
BOTH platforms. That walk is platform-neutral apart from its name: it climbs
``.ppid()`` links via ``psutil``, nothing Windows-specific about the
mechanics. A single-parent name check (POSIX's old approach here) cannot
tell a human's shell-launched CLI apart from an agent-spawned hook
subprocess — both commonly have a shell as their *immediate* parent (see
``core.py``'s compound-command hook topology, ~lines 1629-1639) — so POSIX
gets the same bounded climb Windows already had, not a weaker check.
``session/core.py`` is consumed, never modified, by this module.

Disposition is INVERTED from ``core.init()``'s. There, a walk/check miss
degrades permissively: ``stable_pid`` is simply left empty and Layer-1
liveness falls back to Layer-2 recency, a harmless precision loss on a
best-effort liveness stamp. Here, the same miss must REFUSE: this predicate
gates who gets asked a question that carries consequence (a human vs. a
relayed agent), so an inconclusive walk is not "assume human" or "assume
agent" — it is ``UNRESOLVED``, and ``UNRESOLVED`` refuses exactly like
``AGENT`` does. There is no fallback that returns a permissive verdict on
error; every exception this module cannot categorize as a specific miss
reason is folded into ``UNRESOLVED``, never into ``HUMAN``.

Deliberately excludes the ``CLAUDE_PID`` env-var leg
(``core._resolve_claude_pid_from_env``) that ``core.init()`` also consults.
``CLAUDE_PID`` is a value the process's OWN environment carries — the
harness exports it into the process being asked about. The whole point of
this predicate is to establish a fact recorded about that process from
OUTSIDE its own say-so (its ancestry, read via the OS process table), not
to trust a value the process itself could carry regardless of how it was
spawned. Using it here would let a value the subject controls answer a
question about the subject.

THE CEILING (state verbatim, do not soften): this is a layer, not a
boundary — an out-of-harness spawn (WMI/``schtasks``/service control)
writes with no tool call and no guard involvement.
"""

from __future__ import annotations

import enum
import os
from typing import NamedTuple, Optional

from coordinator_core.session.core import (
    _IS_WINDOWS,
    _find_windows_claude_ancestor,
    _psutil,
)


class Verdict(enum.Enum):
    """The three-way answer this module's predicate can give.

    HUMAN and AGENT are definitive; UNRESOLVED is "the walk/check could not
    tell" (an unreadable rung, a missing psutil, an unexpected exception).
    UNRESOLVED and AGENT are handled identically by every caller of this
    module: both REFUSE. Only HUMAN proceeds. See ``AuthorshipVerdict.refuses``.
    """

    HUMAN = "human"
    AGENT = "agent"
    UNRESOLVED = "unresolved"


class AuthorshipVerdict(NamedTuple):
    """``verdict`` plus the raw walk reason string that produced it, so a
    refusal can name which rung failed without the caller re-deriving it.
    ``reason`` is the underlying ``walk-hit:*`` / ``walk-miss:*`` string
    ``_find_windows_claude_ancestor`` produced — carried through unmodified
    from the reused ``core.py`` machinery, on both platforms.
    """

    verdict: Verdict
    reason: str

    @property
    def refuses(self) -> bool:
        """UNRESOLVED and AGENT both refuse — the caller treats them
        identically; only HUMAN does not refuse."""
        return self.verdict is not Verdict.HUMAN


def authorship_verdict(start_pid: Optional[int] = None) -> AuthorshipVerdict:
    """Did a human author this write, as strongly as this harness can tell?

    ``start_pid`` defaults to ``os.getppid()`` — the caller's own direct
    parent, matching what ``core.init()`` passes into the same underlying
    checks. Exposed as a parameter purely for test injection; production
    callers should not need to pass it.

    Both platforms run the SAME bounded ``.ppid()`` climb
    (``_find_windows_claude_ancestor`` — platform-neutral apart from its
    name; see this module's own docstring). Only the categorization of a
    miss differs, and only there:

    Verdict rules:
      AGENT       — a harness ("claude") ancestor was found anywhere in the
                    climb (``walk-hit:*``), on either platform.
      HUMAN       — POSIX ONLY: the climb reached the TOP of the process
                    tree with no harness ancestor found
                    (``walk-miss:no-parent``) — a COMPLETED climb, and
                    completion with nothing found is the only honest
                    positive signal this mechanism has. ``depth-exhausted``
                    is NOT this case: it means the climb hit its bound
                    (``_STABLE_PID_WINDOWS_ANCESTOR_DEPTH`` rungs) while the
                    chain was still going, so a harness ancestor could still
                    sit above the cap — that is an INCOMPLETE climb, not a
                    clean one. Windows draws no HUMAN line at all: its
                    reused walk vocabulary cannot distinguish these cases as
                    cleanly (a wider set of hook topologies feed it), so on
                    Windows EVERY miss folds to UNRESOLVED, never HUMAN.
      UNRESOLVED  — every OTHER ``walk-miss:*`` reason on POSIX, including
                    ``walk-miss:depth-exhausted`` (the climb did not
                    complete) and ``rung-unreadable:*`` (an unreadable
                    rung), plus missing ``psutil`` or any other exception
                    this module did not expect, on EITHER platform. On
                    Windows this also includes ``walk-miss:no-parent`` (see
                    HUMAN above — Windows draws no HUMAN line). Never
                    degrades to HUMAN or AGENT — REFUSE is the only safe
                    default for "could not tell".

    UNRESOLVED and AGENT are both refusals (``AuthorshipVerdict.refuses``);
    only HUMAN is not.

    RESIDUAL, stated plainly rather than papered over: ``core.py``'s walk
    can also return ``walk-miss:no-parent`` from its skip branch, after
    stepping over a rung whose NAME was unreadable (``AccessDenied`` /
    ``ZombieProcess``) but whose ``.ppid()`` link was still good enough to
    keep climbing. The ``+skipped:`` annotation the walk appends is only
    ever appended on HIT paths, so a bare ``no-parent`` reason cannot prove
    every rung along the way was readable — it proves only that the climb
    reached the top without finding a harness ancestor. Under this
    deliverable's stated threat model — the adversary is a confused peer,
    not a malicious one — accepting that gap is the deliberate call, not an
    oversight: a completed climb with an unreadable rung along the way is
    still treated as HUMAN, because the alternative (folding every
    ``no-parent`` to UNRESOLVED) would refuse the ordinary human case this
    predicate exists to recognize.
    """
    ppid = start_pid if start_pid is not None else os.getppid()

    _ps = _psutil()
    if _ps is None:
        return AuthorshipVerdict(Verdict.UNRESOLVED, "walk-miss:psutil-absent")
    try:
        match, reason = _find_windows_claude_ancestor(ppid)
    except Exception as exc:  # pragma: no cover - defensive, matches core.init()'s own guard
        return AuthorshipVerdict(Verdict.UNRESOLVED, f"walk-miss:{type(exc).__name__}")

    if match is not None:
        return AuthorshipVerdict(Verdict.AGENT, reason)

    if _IS_WINDOWS:
        # Every walk-miss:* reason refuses on Windows — the reused walk's
        # vocabulary has no clean/ambiguous split for this module to lean
        # on (see authorship_verdict's docstring).
        return AuthorshipVerdict(Verdict.UNRESOLVED, reason)

    # POSIX draws the clean/ambiguous line Windows does not: a climb that
    # reaches the TOP of the process tree with no harness ancestor found
    # (walk-miss:no-parent) is a COMPLETED climb and the only clean HUMAN
    # answer this mechanism has. depth-exhausted means the climb hit its
    # bound while the chain was still going — a harness ancestor could
    # still sit above the cap — so that stays ambiguous, along with every
    # other miss reason (an unreadable rung, etc).
    if reason == "walk-miss:no-parent":
        return AuthorshipVerdict(Verdict.HUMAN, reason)
    return AuthorshipVerdict(Verdict.UNRESOLVED, reason)
