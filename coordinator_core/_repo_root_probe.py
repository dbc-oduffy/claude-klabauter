"""coordinator_core._repo_root_probe — shared, per-cwd-keyed memoized
`git rev-parse --show-toplevel` probe.

Extracted 2026-08-16 (review-integration, slice contextvar-globals P2+P3) out
of near-verbatim duplicates in ``coordinator_core.machine_resolver`` and
``coordinator_core.person_resolver`` — both modules' own `_resolve_repo_root`
had identical `subprocess.run` args, exception handling, and docstring
shape, and neither imports the other (an import between the two is
anti-scope per ``person_resolver``'s own module docstring), so this is a new
shared leaf both can depend on without creating a cycle.

Why memoized (2026-08-16 P2 fix): under the warm resident engine (DR-315), a
process-lifetime cache keyed on the RESOLVED repo root (see each caller's own
"Repo-root cache-key fix" docstring note) still paid a fresh `git rev-parse`
spawn on every call just to compute that key — including on a cache HIT for
the value the key looks up. Before the warm engine, a cache hit was a pure
in-memory dict lookup with zero subprocess spawns; reintroducing a spawn into
a cache's hit path inverted the fix's own thesis (the spawn is the cost, not
the work inside it). Fixed by memoizing THIS probe too, keyed on the ambient
`os.getcwd()` at call time — a dict, not a single slot and not a zero-arg
`lru_cache`, mirroring ``coordinator_core.claude_klabauter_root._ROOT_MEMO``'s shape:
a warm process can serve dispatches from different cwds, and a single-slot
cache here would recreate the exact missing-key collision the callers' own
repo-root-keyed caches exist to fix.

Only a SUCCESSFUL resolution is memoized (mirrors ``_ROOT_MEMO`` and both
callers' own `_GitUserEmailResolutionFailed`/`_GitConfigResolutionFailed`
not-memoized-on-failure contract) — a transient failure (not a git repo yet,
git missing, timeout) must not poison the memo for the rest of the process;
the cwd can legitimately become a git repo later in the same process (e.g. a
caller runs `git init` after this was first probed).
"""

from __future__ import annotations

import os
import subprocess
from typing import Dict, Optional

from coordinator_core.git.repo_root import show_toplevel

_GIT_TIMEOUT = 10

#: cwd (as returned by ``os.getcwd()`` at call time) -> resolved repo root.
#: Only successful resolutions are stored — see module docstring.
_REPO_ROOT_MEMO: Dict[str, str] = {}


def reset_repo_root_memo() -> None:
    """Test/diagnostic escape hatch — clears the process-scope cwd-keyed
    memo. Callers' own cache-reset seams (``machine_resolver.
    reset_git_user_email_cache`` / ``person_resolver.
    reset_person_resolver_git_config_cache``) call this too, so a test that
    already resets one of those gets this memo cleared as a side effect
    without needing a third reset call threaded through every test."""
    _REPO_ROOT_MEMO.clear()


def resolve_repo_root() -> Optional[str]:
    """Resolve the current process cwd's git repo root, or ``None`` (not a
    git repo, git missing, or a timeout — fail-open).

    Delegates to ``coordinator_core.git.repo_root.show_toplevel`` rather
    than spawning `git rev-parse --show-toplevel` itself (2026-08-16, chunk
    C5 of docs/plans/2026-08-16-a-process-per-predicate.md). That seam walks
    the parent chain for a `.git` entry and spawns only when the walk finds
    none, so the ordinary case costs ~0.14ms instead of a ~13.6ms process.
    This module's own memo predates that: it was added the same day to stop
    re-spawning on cache HITS, which is the right fix for a probe that
    always spawns and the wrong layer once the probe stops spawning at all.

    The memo is kept regardless — it is now a dict lookup in front of a
    walk rather than in front of a spawn, and it preserves this module's
    only-successful-resolutions-are-memoized contract, which the delegate's
    own memo does not duplicate for this key shape (ambient `os.getcwd()`).
    """
    cwd_key = os.getcwd()
    cached = _REPO_ROOT_MEMO.get(cwd_key)
    if cached is not None:
        return cached
    try:
        root = show_toplevel()
    except (OSError, subprocess.TimeoutExpired):
        return None
    if not root:
        return None
    _REPO_ROOT_MEMO[cwd_key] = root
    return root
