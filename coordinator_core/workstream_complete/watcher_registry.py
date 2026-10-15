"""Registry of coordinator watcher command patterns: answers "is this process one of ours to stop".

A pattern is a launcher stem matched against the image or the first positional token of a command
line, extension and directory dropped, so `.exe/.cmd/.ps1/.py`, extensionless and either path
separator all match. A match is not ownership; session attribution is the caller's.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Tuple

from coordinator_core.bash_guards._taskstop_attribution import _argv

_SCRIPT_EXT = re.compile(r"\.(?:py|cmd|ps1|sh|bat|exe)$", re.IGNORECASE)


@dataclass(frozen=True)
class WatcherPattern:
    """One watcher. `stems` are launcher/script stems; empty with process_visible False marks
    a one-shot subscription that expires on its own."""

    watcher_id: str
    stems: Tuple[str, ...]
    process_visible: bool = True


PATTERNS: Tuple[WatcherPattern, ...] = (
    WatcherPattern("group-em-watch", ("group-em-watch",)),
    WatcherPattern("group-em-memory-governor", ("group-em-memory-governor",)),
    WatcherPattern("notify_when_idle", (), process_visible=False),
)


def _stem_of(token: str) -> str:
    base = re.split(r"[\\/]", token)[-1]
    return _SCRIPT_EXT.sub("", base).lower()


def _launcher_stems(argv: list) -> list:
    """Stems of the image and of the first positional token (the script an interpreter runs)."""
    if not argv:
        return []
    stems = [_stem_of(argv[0])]
    for tok in argv[1:]:
        if tok.startswith("-"):
            continue
        stems.append(_stem_of(tok))
        break
    return stems


def match(cmdline: str) -> Optional[WatcherPattern]:
    """The process-visible pattern a command line launches, else None."""
    stems = _launcher_stems(_argv(cmdline))
    for pat in PATTERNS:
        if pat.process_visible and any(s in pat.stems for s in stems):
            return pat
    return None


def expires_on_its_own() -> Tuple[str, ...]:
    """Watcher ids with no process presence: one-shot subscriptions that fire or expire, with no
    cancel verb. Informational only; no agent action."""
    return tuple(p.watcher_id for p in PATTERNS if not p.process_visible)


__all__ = ["WatcherPattern", "PATTERNS", "match", "expires_on_its_own"]
