"""Route keys, folder constants and the EM route line for the guard-routes wiki.

Pure stdlib leaf: imports nothing from bash_guards or session, so both the
engine seams and the message-size accounting can depend on it without a new
import edge.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

#: Repo-relative resolution form of the generated guard-routes folder.
ROUTES_DIR = "docs/reference/guard-routes"

#: DR-290 form 2: literal, never expanded into a rendered message.
ROUTES_DIR_DISPLAY = "~/.coordinator-claude-settings/coordinator-claude/docs/wiki/guard-routes/"

ROUTES_INDEX = "README.md"

ROUTE_KEY_PREFIX = "gr:"

_KEBAB_SOURCE = r"[a-z0-9]+(?:-[a-z0-9]+)*"

#: Regex source (unanchored, no groups captured) matching one route key.
ROUTE_KEY_PATTERN = r"(?<![A-Za-z0-9])" + re.escape(ROUTE_KEY_PREFIX) + _KEBAB_SOURCE

_KEBAB_RE = re.compile(_KEBAB_SOURCE)


def route_key(guard_name: str) -> str:
    """Return ``gr:<guard_name>``; ValueError if the name is empty or not kebab-case."""
    if not isinstance(guard_name, str) or not _KEBAB_RE.fullmatch(guard_name):
        raise ValueError("guard route key needs a kebab-case guard name, got %r" % (guard_name,))
    return ROUTE_KEY_PREFIX + guard_name


def route_line(guard_name: str) -> str:
    """The single EM-only sentence locating a guard's section; carries no backtick."""
    return "Route key %s in %s (grep it)." % (route_key(guard_name), ROUTES_DIR_DISPLAY)


def routes_index_reachable() -> bool:
    """True if the routes index resolves to a file on this host; any error is False.

    Probes under ``COORDINATOR_SETTINGS_HOME`` when set, else the expanded display
    folder. The rendered display stays the literal ``~/`` form either way.
    """
    try:
        override = os.environ.get("COORDINATOR_SETTINGS_HOME")
        if override:
            folder = Path(override).expanduser() / "coordinator-claude" / "docs" / "wiki" / "guard-routes"
        else:
            folder = Path(ROUTES_DIR_DISPLAY).expanduser()
        return (folder / ROUTES_INDEX).is_file()
    except Exception:
        return False
