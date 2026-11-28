"""Shared predicate for the home-root write guards: does a write create a new entry directly in the home dir?

Underscore-prefixed so `engine._discover_guards` skips it. Stdlib plus two path
helpers from `bash_guards._write_bump_sink_shapes`; spawns nothing and never walks a directory.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Mapping, Optional, Tuple

from coordinator_core.bash_guards._write_bump_sink_shapes import (
    resolve_relative,
    translate_msys_path,
)

# Dotfiles owned by other tools (Claude Code, git, shells, boto, python, node, Git for Windows, Perforce).
TOOL_OWNED_DOTFILES = frozenset(
    {
        ".claude.json",
        ".gitconfig",
        ".bashrc",
        ".bash_profile",
        ".zshrc",
        ".boto",
        ".python_history",
        ".nodemid",
        ".git-for-windows-updater",
        "p4tickets.txt",
    }
)

# Names carry no path, so the message holds one fact plus one alternative.
_INSTEAD = "Instead: write under <repo>/scratch/ or the session scratchpad."
DENY_REASON = "DENY: file created directly in the home dir. " + _INSTEAD


_CLAUDE_JSON_WRITER = "Claude Code"


def writer_of(target: str) -> Optional[str]:
    """The tool that writes `target`'s basename into the home dir, or None when it is not a known tool artifact.

    `.claude.json` and its atomic-write temp (`.claude.json.tmp.<pid>.<id>`), lock and backup siblings are
    Claude Code's; no other name is attributed.
    """
    name = _basename(target).casefold()
    if name == ".claude.json" or name.startswith(".claude.json."):
        return _CLAUDE_JSON_WRITER
    return None


def _basename(target: str) -> str:
    return target.strip().replace("\\", "/").rsplit("/", 1)[-1]


def deny_reason(target: str) -> str:
    """`DENY_REASON`, naming the writer when `writer_of(target)` knows it."""
    writer = writer_of(target)
    if writer is None:
        return DENY_REASON
    return f"DENY: {_basename(target)} is {writer}'s file, created directly in the home dir. " + _INSTEAD


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


_TOOL_OWNED_NORMALISED = frozenset(_norm(d) for d in TOOL_OWNED_DOTFILES)


def home_roots(env: Optional[Mapping[str, str]] = None) -> Tuple[str, ...]:
    """Normalised union of USERPROFILE, HOME and `Path.home()`, deduplicated in that order."""
    source = os.environ if env is None else env
    raw: List[str] = []
    for key in ("USERPROFILE", "HOME"):
        val = source.get(key, "")
        if val and val.strip():
            raw.append(val.strip())
    try:
        raw.append(str(Path.home()))
    except Exception:
        pass
    out: List[str] = []
    for r in raw:
        try:
            native = translate_msys_path(r)
            if native is None:
                continue
            n = _norm(native)
        except Exception:
            continue
        if n not in out:
            out.append(n)
    return tuple(out)


def creates_home_root_entry(
    target: str, cwd: Optional[str], env: Optional[Mapping[str, str]] = None
) -> bool:
    """True when `target` is a not-yet-existing entry whose parent is a home root and whose name is not tool-owned.

    Fails open (False) on any unresolvable input. Descendants of the home dir never match.
    """
    try:
        if not target or not target.strip():
            return False
        target = target.strip()
        resolved = resolve_relative(cwd or "", target)
        if not resolved or not os.path.isabs(resolved):
            return False
        norm = _norm(resolved)
        parent, name = os.path.split(norm)
        if not name or parent not in home_roots(env):
            return False
        if name in _TOOL_OWNED_NORMALISED:
            return False
        return not os.path.lexists(resolved)
    except Exception:
        return False
