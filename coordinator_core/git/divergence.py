"""coordinator_core.git.divergence -- the index/worktree divergence
predicate shared by `bash_guards.commit_tripwires.check_staged_pathspec_
divergence` (Check 13, SC-DR-015) and any future selector that needs the
same answer: for a given set of paths, which of them have STAGED (index)
content that differs from their WORKTREE content right now.

Extracted verbatim from `commit_tripwires.py` (was inline at roughly
:824-833 before this port) -- the computation itself is unchanged, only its
location. A second, independently-maintained copy of this three-git-call
sequence is exactly the failure mode this module exists to prevent: the
selector (a future consumer of this module) must compute the identical
divergence set Check 13 already advises on, not a close-enough
re-derivation.

Spec backlink: docs/wiki/scoped-safety-commits.md § SC-DR-015
Spec backlink: docs/plans/2026-07-27-computed-commit-mechanism-selection.md
  (`fail_loud=` split -- see `DivergenceCheckFailed` below; a genuine `git
  diff` failure/timeout must be indeterminate, not "no divergence", for a
  caller that decides the commit MECHANISM on this answer.)
"""

from __future__ import annotations

import subprocess
from typing import List, Optional, Tuple

_CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class DivergenceCheckFailed(Exception):
    """Raised by `diverging_paths(..., fail_loud=True)` when either
    underlying `git diff` invocation fails (non-zero rc, process never ran,
    or timed out) instead of collapsing that outcome to `[]`.

    Exists because `[]` is ambiguous -- "genuinely no divergence" and "we
    could not tell" are indistinguishable once collapsed to the same empty
    list, and that ambiguity is harmless for an ADVISORY caller (Check 13:
    a failure just means no warning is printed) but load-bearing for a
    caller that picks the commit MECHANISM from this answer
    (`git_native.commit_scoped`, and `commit_pipeline.explicit_stage`'s own
    `git add` decision feeding it) -- there, treating "we could not tell"
    as "clean" silently selects the unsafe branch on a genuinely diverged
    path, reproducing the claude-klabauter 506748a0 incident through the
    very tool built to prevent it. Callers that need to distinguish the two
    outcomes pass `fail_loud=True` and catch this exception; callers that
    are fine with the ambiguous collapse (Check 13) leave the default
    `fail_loud=False` and keep their exact prior behaviour.
    """


def _run_git(args: List[str], cwd: Optional[str] = None, timeout: float = 2.0) -> Tuple[int, str]:
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
            timeout=timeout,
            creationflags=_CREATIONFLAGS,
        )
    except subprocess.TimeoutExpired:
        return -1, ""
    except OSError:
        return 127, ""
    return result.returncode, result.stdout


def diverging_paths(
    paths: List[str],
    cwd: Optional[str] = None,
    *,
    timeout: float = 2.0,
    fail_loud: bool = False,
) -> List[str]:
    """Return the sorted subset of `paths` whose STAGED (index) content
    differs from their WORKTREE content, per `git diff --cached --name-only`
    intersected with `git diff --name-only`, both scoped to `paths` via a
    trailing `--` pathspec.

    `timeout` -- per-`git diff`-call timeout in seconds, forwarded to
    `_run_git`. Default `2.0` matches Check 13's original advisory posture
    (a `git diff --name-only` on a pathspec-scoped batch is proportional to
    the batch size, not repo size, so a healthy process clears this
    comfortably; a caller under real timeout pressure -- e.g. Windows
    spawn-tax, a large concurrent-load repo -- should pass a wider value
    explicitly rather than have this default silently grow for everyone,
    including Check 13's low-stakes advisory use).

    `fail_loud` -- when `False` (the default, and Check 13's usage,
    unchanged), a `git diff` failure or timeout collapses to `[]` same as
    "no divergence found" -- never raises. When `True`, the same failure
    raises `DivergenceCheckFailed` instead, so a caller that uses this
    answer to pick a commit MECHANISM (rather than merely to decide whether
    to print an advisory warning) can tell "clean" apart from "indeterminate"
    and refuse to silently guess. See `DivergenceCheckFailed` for the
    incident this distinction closes.
    """
    if not paths:
        return []

    rc_cached, cached_out = _run_git(["diff", "--cached", "--name-only", "--", *paths], cwd, timeout=timeout)
    if rc_cached != 0:
        if fail_loud:
            raise DivergenceCheckFailed(
                f"diverging_paths: `git diff --cached --name-only` failed or timed out "
                f"(rc={rc_cached}) for {len(paths)} path(s) -- divergence indeterminate"
            )
        return []
    rc_plain, plain_out = _run_git(["diff", "--name-only", "--", *paths], cwd, timeout=timeout)
    if rc_plain != 0:
        if fail_loud:
            raise DivergenceCheckFailed(
                f"diverging_paths: `git diff --name-only` failed or timed out "
                f"(rc={rc_plain}) for {len(paths)} path(s) -- divergence indeterminate"
            )
        return []

    staged_changed = set(l for l in cached_out.splitlines() if l)
    worktree_changed = set(l for l in plain_out.splitlines() if l)
    return sorted(staged_changed & worktree_changed)
