"""
coordinator_core.ops._git_root_util — shared `git rev-parse --show-toplevel` resolver.

Purpose: single implementation of the "resolve the enclosing git repo root, or
None if not in one" primitive shared by scope_soak_enable and
scope_warning_resolve (both need GIT_ROOT to locate `.git/coordinator-sessions/`).

Spec backlink: DoE-claude:pln-bash-to-naked-python-engine-mi-c09292,
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
import os
import sys

from pathlib import Path
from typing import Optional

from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.telemetry import op_latency


def git_root(cwd: Optional[Path] = None) -> Optional[str]:
    """Return the absolute path of the enclosing git repo's toplevel, or None.

    Mirrors ``git rev-parse --show-toplevel``. Returns None on any failure
    (not in a repo, git not on PATH, non-zero exit) rather than raising —
    callers degrade with their own error message, matching the bash
    `GIT_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || true)` convention.

    Delegates to `coordinator_core.git.repo_root.show_toplevel` (walk-first,
    spawn-fallback, cwd-keyed memo) rather than shelling out directly — see
    that module's docstring for the caching/negative-caching contract. The
    module docstring's "Does NOT cache" note above describes this function's
    pre-migration behaviour; the seam it now delegates to caches per resolved
    cwd, never a failed resolution.
    """
    return show_toplevel(str(cwd) if cwd is not None else None)


def git_root_zero_spawn(start: Optional[Path] = None) -> Optional[str]:
    """Resolve the enclosing git repo's root WITHOUT spawning a subprocess.

    Sibling to `git_root()`, not a replacement — `git_root()` has live
    callers with their own semantics (see module docstring) and is left
    untouched. This one exists because `git_root()` shells out to
    `git rev-parse` per call and spawn cost is a standing P0 on a machine
    that runs 50-70 concurrent agents; these app-session ops are called
    repeatedly, so a pure-Python upward walk pays for itself immediately.

    Resolution order:
      1. `CLAUDE_PROJECT_DIR` env var, if set AND it is a real directory —
         honoured first, ahead of any walk, but ONLY when this process is
         the one the caller ran in (``execution_route() == IN_PROCESS``).
         `CLAUDE_PROJECT_DIR` is a property of a CALLING process; under the
         warm engine this function can run in a long-lived server process
         whose environment was inherited from whichever session happened to
         spawn it, so trusting the raw read there would resolve a stranger's
         project dir instead of the current caller's (see
         `queue_append._output_root_override`'s docstring for the full
         hazard on a sibling op). ``app_session.launch``/``census``/
         ``teardown`` reach this function via `_resolve_repo_root` whenever
         neither an explicit `repo_root` param nor an injected one is
         supplied.
      2. Otherwise walk upward from `start` (default: cwd) looking for a
         `.git` entry at each level, stopping at the first hit.

    `.git` is a DIRECTORY in a normal clone but a FILE in a git worktree
    (it holds a `gitdir: <path>` pointer). A directory-only test silently
    fails to resolve inside a worktree, which is exactly the class of
    silent misresolution this whole primitive exists to avoid — so this
    walk accepts either a file or a directory named `.git`.

    Anchors on `start` (or cwd), the CONSUMING repo — never on `__file__` —
    per Hard constraint 6: a `__file__`-relative walk misresolves the
    moment this code ships anywhere but the tree it operates on.

    Returns None (never raises) when no `.git` entry is found before
    reaching the filesystem root.
    """
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if project_dir and op_latency.execution_route() == op_latency.IN_PROCESS:
        p = Path(project_dir)
        if p.is_dir():
            return str(p)

    current = Path(start) if start is not None else Path.cwd()
    current = current.resolve()
    while True:
        git_entry = current / ".git"
        if git_entry.is_dir() or git_entry.is_file():
            return str(current)
        parent = current.parent
        if parent == current:
            return None
        current = parent
