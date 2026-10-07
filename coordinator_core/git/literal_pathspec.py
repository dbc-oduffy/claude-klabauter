"""Pathspec entry handed to git: a bracket-bearing literal path stays literal."""

from __future__ import annotations

GLOB_CHARS = ("*", "?")


def is_magic_pathspec(entry: str) -> bool:
    """True for a ``:``-prefixed magic pathspec or an entry with ``*``/``?``.
    A ``[`` alone is not a glob here: ``[id]`` is a Next.js route segment."""
    return entry.startswith(":") or any(ch in entry for ch in GLOB_CHARS)


def git_pathspec(entry: str) -> str:
    """``:(literal)<entry>`` for a bracket-bearing entry with no ``*``/``?`` and
    no ``:`` magic (git reads ``[id]`` as a one-character class and matches
    nothing); every other entry, globs and magic included, passes unchanged."""
    if "[" in entry and not is_magic_pathspec(entry):
        return f":(literal){entry}"
    return entry
