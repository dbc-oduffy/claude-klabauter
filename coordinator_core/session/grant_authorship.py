"""Authorship layer: did a human author this write, as strongly as this
harness can tell?

This module exposes one predicate — :func:`authorship_verdict` — for a
caller (the ask-the-PM step's grant-routing resolver) that needs to know
whether the process asking for a decision is a human-driven session or an
agent-driven one, and to REFUSE rather than guess when it cannot tell.

It reuses the existing harness-ancestor machinery from
``coordinator_core.session.core`` — ``_find_windows_claude_ancestor`` on
Windows, the same comm-verified single-parent check ``core.init()`` already
performs on POSIX (Guard-1 leg (a)) — rather than re-implementing a walk.
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
    _harness_process_comm,
    _is_harness_process,
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
    """``verdict`` plus the raw walk/check reason string that produced it,
    so a refusal can name which rung failed without the caller re-deriving
    it. ``reason`` is the underlying ``walk-hit:*`` / ``walk-miss:*`` /
    ``posix-parent-*`` string this module's categorization was based on —
    carried through unmodified from the reused ``core.py`` machinery (or
    this module's own POSIX single-check vocabulary, prefixed the same way
    for a reader used to that vocabulary).
    """

    verdict: Verdict
    reason: str

    @property
    def refuses(self) -> bool:
        """UNRESOLVED and AGENT both refuse — the caller treats them
        identically; only HUMAN does not refuse."""
        return self.verdict is not Verdict.HUMAN


def _posix_parent_check(ppid: int) -> "tuple[Optional[bool], str]":
    """Comm-verify ``ppid`` exactly as ``core.init()``'s POSIX Guard-1 leg
    (a) does — NOT a walk (POSIX's ``sh -c "<cmd>"`` exec-replace makes the
    direct parent the harness process where it exec-replaced; this checks
    only that one rung, mirroring ``core.init()``'s inline logic rather
    than re-implementing ``_find_windows_claude_ancestor``'s bounded climb,
    which POSIX has no need of).

    Returns ``(is_claude, reason)``. ``is_claude`` is ``True`` on a
    comm-verified "claude" parent, ``False`` on a comm-verified NON-"claude"
    parent (a clean, completed answer — the parent process itself is
    live and readable, it simply is not the harness), or ``None`` when the
    parent could not be read at all (ambiguous — ``psutil`` absent, or the
    parent process raised ``NoSuchProcess`` / ``AccessDenied`` / any other
    ``psutil.Error``).

      posix-parent-hit                  comm-verified "claude" parent
      posix-parent-miss:name-mismatch   comm-verified, NOT "claude" — clean
      walk-miss:psutil-absent           psutil unavailable — ambiguous
      walk-miss:rung-unreadable:<exc>   parent process unreadable — ambiguous
    """
    _ps = _psutil()
    if _ps is None:
        return None, "walk-miss:psutil-absent"
    try:
        parent = _ps.Process(ppid)
        comm = _harness_process_comm(parent)
    except (_ps.NoSuchProcess, _ps.AccessDenied, _ps.Error) as exc:
        return None, f"walk-miss:rung-unreadable:{type(exc).__name__}"
    if _is_harness_process(comm):
        return True, "posix-parent-hit"
    return False, "posix-parent-miss:name-mismatch"


def authorship_verdict(start_pid: Optional[int] = None) -> AuthorshipVerdict:
    """Did a human author this write, as strongly as this harness can tell?

    ``start_pid`` defaults to ``os.getppid()`` — the caller's own direct
    parent, matching what ``core.init()`` passes into the same underlying
    checks. Exposed as a parameter purely for test injection; production
    callers should not need to pass it.

    Verdict rules:
      HUMAN       — no harness ancestor found AND the check completed
                    cleanly. Windows' reused walk vocabulary draws no line
                    between "cap reached with nothing to report" and "a
                    rung along the way could not be read" — every no-match
                    outcome from ``_find_windows_claude_ancestor`` comes
                    back ``walk-miss:*``, so on Windows EVERY miss folds to
                    UNRESOLVED (see below), never HUMAN: this predicate has
                    no clean-vs-ambiguous distinction to draw on that arm.
                    POSIX's single-rung check (no walk) DOES draw that
                    line: a comm-verified parent that reads successfully
                    and is NOT "claude" (``posix-parent-miss:name-mismatch``)
                    is a clean, completed answer — that is the only path
                    that reaches HUMAN.
      AGENT       — a harness ("claude") ancestor was found (Windows:
                    ``walk-hit:*``; POSIX: ``posix-parent-hit``).
      UNRESOLVED  — every ``walk-miss:*`` reason on Windows (depth-exhausted,
                    no-parent, and rung-unreadable alike — the reused walk
                    cannot distinguish a clean cap-out from an unreadable
                    rung, so ALL of them refuse), plus an unreadable POSIX
                    parent, missing ``psutil``, or any other exception this
                    module did not expect. Never degrades to HUMAN or
                    AGENT — REFUSE is the only safe default for "could not
                    tell".

    UNRESOLVED and AGENT are both refusals (``AuthorshipVerdict.refuses``);
    only HUMAN is not.
    """
    ppid = start_pid if start_pid is not None else os.getppid()

    if _IS_WINDOWS:
        _ps = _psutil()
        if _ps is None:
            return AuthorshipVerdict(Verdict.UNRESOLVED, "walk-miss:psutil-absent")
        try:
            match, reason = _find_windows_claude_ancestor(ppid)
        except Exception as exc:  # pragma: no cover - defensive, matches core.init()'s own guard
            return AuthorshipVerdict(Verdict.UNRESOLVED, f"walk-miss:{type(exc).__name__}")
        if match is not None:
            return AuthorshipVerdict(Verdict.AGENT, reason)
        # Every walk-miss:* reason refuses on Windows — the reused walk's
        # vocabulary has no clean/ambiguous split for this module to lean
        # on (see authorship_verdict's docstring).
        return AuthorshipVerdict(Verdict.UNRESOLVED, reason)

    try:
        is_claude, reason = _posix_parent_check(ppid)
    except Exception as exc:  # pragma: no cover - defensive, matches core.init()'s own guard
        return AuthorshipVerdict(Verdict.UNRESOLVED, f"walk-miss:{type(exc).__name__}")
    if is_claude is True:
        return AuthorshipVerdict(Verdict.AGENT, reason)
    if is_claude is False:
        return AuthorshipVerdict(Verdict.HUMAN, reason)
    return AuthorshipVerdict(Verdict.UNRESOLVED, reason)
