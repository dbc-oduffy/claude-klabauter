"""
coordinator_core.ops._git_root_util — shared `git rev-parse --show-toplevel` resolver.

Purpose: single implementation of the "resolve the enclosing git repo root, or
None if not in one" primitive shared by scope_soak_enable and
scope_warning_resolve (both need GIT_ROOT to locate `.git/coordinator-sessions/`).

Spec backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md,
    chunk B3 (handoff-gate-aging.sh + scope-soak-enable + scope-warning-resolve port)

Negative-spec:
    - Does NOT cache — each call shells out fresh (mirrors the bash scripts'
      `git rev-parse --show-toplevel` per-invocation call; these are one-shot
      CLI processes, not long-lived servers, so caching buys nothing).
    - Does NOT raise on failure — returns None so callers can emit their own
      "not inside a git repository" message (message wording differs slightly
      per caller in the bash originals, so the message is NOT centralized here).
"""

from __future__ import annotations
import sys

import subprocess
from coordinator_core.win_portability import no_console_creationflags
from pathlib import Path
from typing import Optional


def git_root(cwd: Optional[Path] = None) -> Optional[str]:
    """Return the absolute path of the enclosing git repo's toplevel, or None.

    Mirrors ``git rev-parse --show-toplevel``. Returns None on any failure
    (not in a repo, git not on PATH, non-zero exit) rather than raising —
    callers degrade with their own error message, matching the bash
    `GIT_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || true)` convention.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            # Review: code-reviewer — Windows portability convention applied
            # inconsistently across this wave's siblings; align this call site.
            **no_console_creationflags(),
        )
    except OSError:
        print(f"skip: git_root: result = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.strip()
