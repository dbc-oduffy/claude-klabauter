"""Completion decision-key aliases: which `completion_*` keys apply, which are refused.

Pure: no I/O, no git. `apply` resolves the entry's title, prose and nature through
`resolve_entry_surfaces` and refuses a `completion_`-prefixed key outside the alias table.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

COMPLETION_KEY_ALIASES: Mapping[str, str] = {
    "completion_title": "title",
    "completion_prose": "prose",
    "completion_nature": "nature",
}

_COMPLETION_PREFIX = "completion_"


@dataclass(frozen=True)
class EntrySurfaces:
    """Resolved entry surfaces; `aliased` lists the `completion_*` keys that supplied a value."""

    title: Optional[str]
    prose: Optional[str]
    nature_key_value: Optional[str]
    aliased: tuple[str, ...]


def _text(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value
    return None


def resolve_entry_surfaces(decisions: Mapping[str, Any]) -> EntrySurfaces:
    """A non-empty `completion_<x>` wins over `<x>`; an empty one falls through to the bare key.

    `nature_key_value` is the raw `nature` surface only; the judgment fallback stays in apply.
    """
    resolved: dict[str, Optional[str]] = {}
    aliased: list[str] = []
    for alias, bare in COMPLETION_KEY_ALIASES.items():
        value = _text(decisions.get(alias))
        if value is not None:
            aliased.append(alias)
        else:
            value = _text(decisions.get(bare))
        resolved[bare] = value
    return EntrySurfaces(
        title=resolved["title"],
        prose=resolved["prose"],
        nature_key_value=resolved["nature"],
        aliased=tuple(aliased),
    )


def refused_decision_keys(decisions: Mapping[str, Any]) -> tuple[str, ...]:
    """Sorted `completion_` keys outside the alias table; hyphenated judgment ids never match."""
    return tuple(
        sorted(
            k
            for k in decisions
            if isinstance(k, str)
            and k.startswith(_COMPLETION_PREFIX)
            and k not in COMPLETION_KEY_ALIASES
        )
    )


def refusal_message(keys: tuple[str, ...]) -> str:
    """One line naming each refused key and the accepted set."""
    accepted = ", ".join(sorted(COMPLETION_KEY_ALIASES))
    return f"unknown decision key(s): {', '.join(keys)}. accepted: {accepted}."
