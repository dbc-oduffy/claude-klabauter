"""Guard-level gate shared by the doctrine-surface guards: maps a strict deny
onto the machine's `coordinator.guard_level` (strict | warn | off)."""

from __future__ import annotations

from typing import Any, Dict, Optional

_BLAST_RADIUS = (
    "Doctrine surfaces reach every session and dispatched agent, so an edit "
    "here has the widest blast radius and is rarely the best way to get "
    "something done."
)


def level_for(guard_name: str) -> str:
    """`strict` when `coordinator_core.machine_profile.guard_level` is not
    importable, so a missing resolver never loosens a guard."""
    try:
        from coordinator_core.machine_profile import guard_level
    except ImportError:
        return "strict"
    try:
        return guard_level(guard_name)
    except Exception:
        return "strict"


def warn_advisory(guard_name: str, what: str) -> Dict[str, Any]:
    text = (
        f"[{guard_name}] warning: {what} {_BLAST_RADIUS} Allowed at "
        "guard_level warn; `machine-local set coordinator.guard_level strict` "
        f"blocks it, `machine-local set coordinator.guard_level.{guard_name} "
        "off` silences this guard."
    )
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": text}}


def apply_level(
    guard_name: str, denial: Optional[Dict[str, Any]], what: str
) -> Optional[Dict[str, Any]]:
    """`denial` unchanged at strict; an advisory at warn; None at off."""
    if denial is None:
        return None
    level = level_for(guard_name)
    if level == "off":
        return None
    if level == "warn":
        return warn_advisory(guard_name, what)
    return denial
