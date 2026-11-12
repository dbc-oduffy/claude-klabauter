"""The doctrine-edit sentinel reader shared by the Edit/Write and Bash doctrine-surface guards (DR-502).

Stdlib plus ``content_root``; imported on the hook hot path, so no spawn. The engine never
creates the sentinel: an operator does, and ``block_approval_sentinel_creation`` denies any agent.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

from coordinator_core.content_root import read_content_root

SENTINEL_BASENAME = ".coordinator-doctrine-edit-approved"
FRESH_SECONDS = 1800

DENY_REASON = (
    "[doctrine-surface guard] EM writes to CLAUDE.md and rules files are denied. "
    "Alternative: a fresh .coordinator-doctrine-edit-approved in the git dir opens one."
)


def git_dir_for(cwd: str) -> Optional[Path]:
    """The git dir governing ``cwd``, read off the filesystem (no spawn); a
    ``.git`` file (worktree / submodule) is followed to its ``gitdir:`` target."""
    here = Path(cwd or os.getcwd())
    for base in (here, *here.parents):
        dot_git = base / ".git"
        if dot_git.is_dir():
            return dot_git
        if dot_git.is_file():
            try:
                text = dot_git.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None
            if text.startswith("gitdir:"):
                target = Path(text[len("gitdir:"):].strip())
                return target if target.is_absolute() else base / target
            return None
    return None


def sentinel_present(cwd: str) -> bool:
    """True when ``<git dir>/.coordinator-doctrine-edit-approved`` exists and is under
    ``FRESH_SECONDS`` old. Any filesystem error reads as absent."""
    try:
        git_dir = git_dir_for(cwd)
        if git_dir is None:
            return False
        age = time.time() - (git_dir / SENTINEL_BASENAME).stat().st_mtime
        return age <= FRESH_SECONDS
    except OSError:
        return False


def _under(target: str, root: str) -> bool:
    t = os.path.normcase(os.path.realpath(os.path.abspath(target)))
    r = os.path.normcase(os.path.realpath(os.path.abspath(root)))
    return t == r or t.startswith(r.rstrip(os.sep) + os.sep)


def is_rules_path(target: str) -> bool:
    """True when ``target`` lies under ``~/.claude/rules`` or ``<content-root>/global-doctrine/rules``."""
    roots = []
    home = os.path.expanduser("~")
    if home and home != "~":
        roots.append(os.path.join(home, ".claude", "rules"))
    try:
        content_root = read_content_root() or None
    except Exception:
        content_root = None
    if content_root:
        roots.append(os.path.join(content_root, "global-doctrine", "rules"))
    return any(_under(target, root) for root in roots)
