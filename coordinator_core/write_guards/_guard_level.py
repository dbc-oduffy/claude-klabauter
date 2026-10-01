"""Shared doctrine-surface advisory text and risk wording; the level mapping
itself lives in ``coordinator_core.machine_profile.apply_guard_level``."""

from __future__ import annotations

from typing import Any, Dict

_BLAST_RADIUS = (
    "Doctrine surfaces reach every session and dispatched agent, so an edit "
    "here has the widest blast radius and is rarely the best way to get "
    "something done."
)


#: Shared by the Edit/Write and Bash doctrine-surface advisories.
DOCTRINE_SURFACE_ADVISORY = (
    "CLAUDE.md-class file: rarely the right home for a \"remember this\" -- "
    "prefer a structural guard, a test, or a wiki page.\n"
    "Doctrine text is the shortest form a fresh agent can act on; "
    "bloated doctrine gets ignored."
)


def doctrine_surface_advisory(extra: str = "") -> Dict[str, Any]:
    """Warn-and-pass envelope carrying ``DOCTRINE_SURFACE_ADVISORY``; ``extra``
    is one appended line (e.g. a runnable follow-up command)."""
    text = DOCTRINE_SURFACE_ADVISORY + (f"\n{extra}" if extra else "")
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": text}}


def doctrine_surface_risk(what: str) -> str:
    """``what`` plus the doctrine-surface blast-radius sentence, as ``risk=`` text."""
    return f"{what} {_BLAST_RADIUS}"
