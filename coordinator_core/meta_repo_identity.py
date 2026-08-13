"""
meta_repo_identity.py — canonical boolean primitive: is a git root the coordinator meta-repo?

Purpose: single definition of "is the current (or given) git repository the
coordinator meta-repo (~/.claude / CLAUDE_HOME)?"

Port of: coordinator-is-meta-repo.sh (coordinator-claude 6fb5fb37, 2026-07-22).

Design (mirrors the shell original):
  - CLAUDE_HOME-aware: resolves the meta-repo root via the same env-var
    precedence as coordinator/lib/claude-home/_claude_home.py::home_dir()
    (CLAUDE_HOME -> HOME -> USERPROFILE -> Path.home()), honouring
    CLAUDE_HOME overrides for test sandboxes and CI.
  - Canonicalized: both sides go through realpath (via Path.resolve()) before
    comparison, to survive symlinked paths.
  - Fail-loud: raises MetaRepoResolutionError with a remediation message when
    the git root is empty/unresolvable or the home directory cannot be
    determined (detect-then-fail-loud, not detect-then-silently-pick) —
    mirrors the shell original's exit-2 contract.

Public API:
    is_meta_repo(git_root: str | None = None) -> bool
        True  — <git_root> (or cwd's git root when omitted) IS the meta-repo.
        False — it is NOT the meta-repo.
        Raises MetaRepoResolutionError — git root or home dir unresolvable.

Negative-spec:
  - Does NOT mutate any process/global state.
  - Does NOT resolve the claude-klabauter root — purely about meta-repo identity.
  - Does NOT shell out to `claude-home` — reimplements the same env-var
    precedence in-process (bootstrap-safe, no subprocess for the home lookup;
    `git rev-parse` is still shelled out to resolve an omitted git_root, same
    as the shell original).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from coordinator_core.git import repo_root as _repo_root_seam


class MetaRepoResolutionError(RuntimeError):
    """Raised when the git root or meta-repo home directory cannot be resolved."""


def _resolve_home_dir() -> Path:
    """Resolve the home directory, first hit wins: the CLAUDE_HOME env var, then
    HOME, then USERPROFILE, then Path.home().

    Mirrors coordinator/lib/claude-home/_claude_home.py::home_dir() precedence.
    An explicitly-set-but-empty CLAUDE_HOME is a configuration error, not a
    silent fallthrough.
    """
    claude_home = os.environ.get("CLAUDE_HOME")
    if claude_home is not None:
        if not claude_home:
            raise MetaRepoResolutionError(
                "meta_repo_identity: CLAUDE_HOME is set but empty; "
                "unset it or provide an absolute path"
            )
        return Path(claude_home)

    home = os.environ.get("HOME")
    if home:
        return Path(home)

    userprofile = os.environ.get("USERPROFILE")
    if userprofile:
        return Path(userprofile)

    return Path.home()


def _resolve_meta_repo_root() -> Path:
    """Resolve the ~/.claude (or CLAUDE_HOME-substituted) meta-repo root."""
    try:
        home_dir = _resolve_home_dir()
    except MetaRepoResolutionError:
        raise
    except OSError as exc:
        raise MetaRepoResolutionError(
            f"meta_repo_identity: failed to resolve home directory: {exc}"
        ) from exc
    return home_dir / ".claude"


def _resolve_git_root(git_root: Optional[str]) -> str:
    """Resolve the git root from cwd if not provided by the caller."""
    if git_root:
        return git_root

    resolved = _repo_root_seam.show_toplevel()
    if not resolved:
        raise MetaRepoResolutionError(
            "meta_repo_identity: git rev-parse --show-toplevel failed — "
            "not in a git repository? Remediation: run from within a git "
            "repository, or pass the git root explicitly."
        )
    return resolved


def _canonicalize(path: Path) -> str:
    """Canonicalize a path via realpath; falls back to the literal path if
    the directory does not exist on disk (mirrors the shell original's
    `cd ... && pwd -P || echo "$path"` fallback)."""
    try:
        return str(path.resolve(strict=False))
    except OSError:
        return str(path)


def is_meta_repo(git_root: Optional[str] = None) -> bool:
    """Return True iff `git_root` (or cwd's git root when omitted) is the
    coordinator meta-repo (~/.claude / CLAUDE_HOME).

    Raises MetaRepoResolutionError on an unresolvable git root or home dir.
    """
    resolved_git_root = _resolve_git_root(git_root)
    meta_repo_root = _resolve_meta_repo_root()

    canon_git = _canonicalize(Path(resolved_git_root))
    canon_meta = _canonicalize(meta_repo_root)

    return canon_git == canon_meta
