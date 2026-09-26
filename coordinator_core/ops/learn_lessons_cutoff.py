"""coordinator_core.ops.learn_lessons_cutoff — the ONE cutoff oracle.

Purpose: promote the COMPLETE-sentinel scan (previously duplicated across
`central_run_due._find_cutoff` and `coordinator/bin/learn-lessons-age-sweep.py
:: derive_cutoff`) into a single shared reader. A completed central learn-lessons
run is a `<runs_dir>/learn-lessons-YYYY-MM-DD/` directory carrying a `COMPLETE`
sentinel file (learn-lessons Phase 8). A run-dir WITHOUT the sentinel is
in-progress/aborted and must never become the cutoff.

`resolve_runs_dir()` moves the `CLAUDE_HOME` → `HOME` → `USERPROFILE` →
`expanduser("~")` ladder out of `coordinator_core.ops.central_run_due` (that
module's `_claude_home` now delegates here) and out of
`coordinator_core.ops.learn_lessons_roots` (that module's `_claude_home` now
delegates here too — a third, undisclosed copy, character-for-character the
same four-step chain, folded in the same move). Note the ladder's own
contract: the env var CLAUDE_HOME, when set, overrides $HOME (not the full
.claude path) — reproduced verbatim, not "fixed".

`derive_cutoff()` matches the same `learn-lessons-20` directory-name prefix
the two existing derivations already share, not the stricter
`^\\d{4}-\\d{2}-\\d{2}$` shape — a directory named `learn-lessons-2026-13-45`
still matches the prefix and is excluded only if it lacks the `COMPLETE`
sentinel, same as today. It never raises on a missing/unreadable runs dir —
callers with a fail-open posture (`central_run_due`) rely on that to keep
their own skip-line-and-return-0 contract; it returns `None` instead.

Not a CLI. No `main()`, no `__main__` guard — this module is imported, never
invoked as a script.

Spec backlink: docs/plans/2026-09-11-the-lessons-pipeline-drains-without-a-ha.md § C1
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def _claude_home() -> str:
    """Mirror the ported bash oracle's `CLAUDE_HOME="${CLAUDE_HOME:-$HOME}/.claude"`.

    Note the oracle's own naming: the env var CLAUDE_HOME, when set, overrides
    $HOME (not the full .claude path) — reproduced verbatim, not "fixed".
    """
    base = (
        os.environ.get("CLAUDE_HOME")
        or os.environ.get("HOME")
        or os.environ.get("USERPROFILE")
        or os.path.expanduser("~")
    )
    return os.path.join(base, ".claude")


def resolve_runs_dir() -> Path:
    """`<claude_home>/tasks` — the directory central learn-lessons runs stamp
    their `learn-lessons-YYYY-MM-DD/` dirs into."""
    return Path(_claude_home()) / "tasks"


def derive_cutoff(runs_dir: Path) -> Optional[str]:
    """The lexically-latest `learn-lessons-YYYY-MM-DD/` dir carrying a
    `COMPLETE` file, or None if no completed run is reachable (including a
    missing/unreadable `runs_dir` — never raises).

    Matches the `learn-lessons-20` prefix, not a strict date-shape regex —
    same as the two derivations this module supersedes.
    """
    try:
        if not runs_dir.is_dir():
            return None
        entries = sorted(runs_dir.iterdir(), key=lambda p: p.name)
    except OSError:
        return None

    prefix = "learn-lessons-20"
    dir_prefix = "learn-lessons-"
    cutoff: Optional[str] = None
    for entry in entries:
        name = entry.name
        if not name.startswith(prefix):
            continue
        try:
            if not entry.is_dir():
                continue
            if not (entry / "COMPLETE").is_file():
                continue
        except OSError:
            continue
        cutoff = name[len(dir_prefix):]
    return cutoff
