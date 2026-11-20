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
    "Alternative: a PM-approved .coordinator-doctrine-edit-approved in the git dir admits for 30 minutes, "
    "then removes itself. Nothing to clean up."
)


def deny_reason(cwd: str) -> str:
    """``DENY_REASON`` plus the human ``!`` touch of the approval marker at the absolute git dir
    governing ``cwd`` (forward slashes, so it runs in any shell); bare ``DENY_REASON`` when no git dir resolves."""
    marker = _sentinel_path(cwd)
    if marker is None:
        return DENY_REASON
    return f'{DENY_REASON} Human: `! touch "{Path(os.path.abspath(marker)).as_posix()}"`.'


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


def _sentinel_path(cwd: str) -> Optional[Path]:
    git_dir = git_dir_for(cwd)
    return None if git_dir is None else git_dir / SENTINEL_BASENAME


def sentinel_present(cwd: str) -> bool:
    """True when ``<git dir>/.coordinator-doctrine-edit-approved`` exists and is under
    ``FRESH_SECONDS`` old. A stale one is unlinked best-effort; an unlink failure still
    reads as not admitting. Any filesystem error reads as absent."""
    try:
        path = _sentinel_path(cwd)
        if path is None:
            return False
        age = time.time() - path.stat().st_mtime
        if age <= FRESH_SECONDS:
            return True
        try:
            path.unlink()
        except OSError:
            pass
        return False
    except OSError:
        return False


def admit_until(cwd: str) -> Optional[str]:
    """Local ``HH:MM`` at which the present sentinel stops admitting (mtime + ``FRESH_SECONDS``);
    None when absent or unreadable."""
    try:
        path = _sentinel_path(cwd)
        if path is None:
            return None
        expiry = path.stat().st_mtime + FRESH_SECONDS
    except OSError:
        return None
    return time.strftime("%H:%M", time.localtime(expiry))


def admit_advisory(cwd: str) -> str:
    """The advisory text a sentinel admit carries, shared by the Edit/Write and Bash guards."""
    until = admit_until(cwd)
    when = f"until {until}" if until else "for 30 minutes"
    return (
        f"[doctrine-surface guard] doctrine-edit approval admits this write {when}, "
        "then removes itself. Nothing to clean up."
    )


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
