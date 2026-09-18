"""Zero-spawn git COMMON-dir resolution primitive for hook consumers.

Ported from DoE-claude `coordinator/hooks/scripts/_git_common_dir.py` per
docs/plans/2026-09-18-doe-holds-no-scripts.md chunk W4-C4, verbatim: this
module is self-contained (stdlib-only, `os` only, no cross-repo/cross-plane
reference), so no adaptation was needed.

`coordinator_core/git/git_dir.py::resolve_git_common_dir` is NOT a substitute
here despite the name overlap: that module's fail-open target is the literal
`<repo_root>/.git` join (a non-existent path a caller could go on to
`mkdir(parents=True)` under), while every consumer of THIS module treats the
empty-string return as "skip, do not build a path from it" -- swapping the
fail-open target would silently change what every existing caller does on an
unresolvable common dir. Kept as its own module for that reason, not
duplication for its own sake.

In an ordinary clone, `<git_root>/.git` IS the common dir (a directory). In a
worktree, `<git_root>/.git` is a FILE containing a single `gitdir: <path>`
line pointing at the worktree's own private git dir (`<path>` may be
relative to `git_root`); that private git dir in turn contains a `commondir`
file naming the actual shared common dir (again possibly relative -- this
time to the private git dir itself). Blindly joining `git_root + ".git"`
silently resolves to a location that doesn't exist as a directory under a
worktree -- a write there fails and a best-effort `except` swallows it; a
read there simply finds nothing. Subagents DO run in worktrees, so this is a
live fail-open portability defect, not a theoretical one.

Negative spec: do NOT reintroduce a subprocess (`git rev-parse
--git-common-dir`) here as a routine path -- this module stays the
zero-spawn primitive it was ported as. Every caller degrades to "" on an
unresolvable common dir and must treat that as "skip, do not build a path
from empty string" -- never as "already fired"/"already present".
"""

from __future__ import annotations

import os


def resolve_git_common_dir(git_root: str) -> str:
    """Resolve the git COMMON dir for `git_root` without spawning a subprocess. Fails open to
    "" on any error, including the plain-clone case where `.git` is simply a directory. Never
    raises."""
    try:
        dot_git = os.path.join(git_root, ".git")
        if os.path.isdir(dot_git):
            return dot_git
        if os.path.isfile(dot_git):
            with open(dot_git, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read().strip()
            if not text.startswith("gitdir:"):
                return ""
            gitdir_value = text[len("gitdir:"):].strip()
            git_dir = (
                gitdir_value
                if os.path.isabs(gitdir_value)
                else os.path.normpath(os.path.join(git_root, gitdir_value))
            )
            if not os.path.isdir(git_dir):
                return ""
            commondir_file = os.path.join(git_dir, "commondir")
            if os.path.isfile(commondir_file):
                with open(commondir_file, "r", encoding="utf-8", errors="replace") as fh:
                    common_value = fh.read().strip()
                if not common_value:
                    return git_dir
                return (
                    common_value
                    if os.path.isabs(common_value)
                    else os.path.normpath(os.path.join(git_dir, common_value))
                )
            return git_dir
        return ""
    except Exception:
        return ""
