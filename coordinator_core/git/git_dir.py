"""coordinator_core.git.git_dir -- pure-Python resolution of a repo's git
COMMON dir (the shared, per-repo gitdir every linked worktree/submodule
ultimately points at), used wherever a caller needs the ONE location a
repo-level artifact (e.g. `push-failures.log`) should live regardless of
which worktree wrote it.

Why this exists: `<repo_root>/.git` is a plain DIRECTORY only in an ordinary
clone. In a linked worktree, a `--separate-git-dir` clone, or a submodule,
`<repo_root>/.git` is a regular FILE containing a `gitdir: <path>` pointer --
treating it as a directory (`open(path / ".git" / "some.log", "a")`) raises
`NotADirectoryError`. The submodule form is the sharpest trap: its pointer is
RELATIVE (e.g. `gitdir: ../.git/modules/<name>`), so a resolver that only
handles the absolute form silently mis-resolves it.

No subprocess is spawned here on purpose -- this module is called from a
post-commit hook and from the orientation-cache hot path, both of which must
stay cheap and Windows-safe (a cold subprocess per call is exactly the shape
CLAUDE.md's Runtime conventions calls break-class). Pure path/text reads
only.

Spec backlink: dispatch brief "auto-push log path resolution -- git common
dir" (2026-08-01), ruling: target = the git COMMON dir, one shared log per
repo, matching `git_common_dir` keying already used throughout
`coordinator_core/op_scopes.py`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Union

_GITDIR_PREFIX = "gitdir:"


def resolve_git_dir(repo_root: Union[str, Path]) -> Path:
    """Resolve `repo_root`'s (private, per-worktree) git dir without
    spawning `git` -- i.e. what `git rev-parse --git-dir` reports, NOT what
    `--git-common-dir` reports. The two differ inside a linked worktree: this
    function returns the worktree-private gitdir (e.g.
    `<main>/.git/worktrees/<name>`), never chasing the `commondir` file
    `resolve_git_common_dir` follows to find the shared repo-level dir.

    - `<repo_root>/.git` is a directory -> return it (plain clone; the
      private and common dirs are identical here).
    - `<repo_root>/.git` is a file -> parse its `gitdir: <path>` line. A
      relative `<path>` (the submodule form) is resolved against
      `repo_root`, not against `<repo_root>/.git`'s own parent -- that is
      what git itself does, and getting this wrong silently mis-resolves
      every submodule. The result -- the private gitdir -- is returned
      directly, without following any `commondir` indirection.
    - Anything else -- missing `.git` entirely, an OSError reading it, a
      pointer file that doesn't start with `gitdir:`, or any other
      unparseable content -- fails open to the literal `<repo_root>/.git`
      join, silently (no print; this runs under a hook that must stay
      quiet). The degraded case is never worse than the pre-change
      behavior.

    DELIBERATE TWIN, do not "consolidate": ``coordinator/bin/coordinator-
    prepare-commit-msg.py::_resolve_git_dir`` derives the same answer by
    hand and must keep doing so. It runs before that hook imports any
    engine, and on a non-coordinator commit the hook exits without ever
    importing one -- reusing this function there would add a
    ``coordinator_core`` import to every commit on every machine. That
    twin also honours ``$GIT_DIR`` and returns ``""`` on failure, neither
    of which this function does. See its docstring for the full argument.
    """
    repo_root = Path(repo_root)
    dot_git = repo_root / ".git"

    try:
        if dot_git.is_dir():
            return dot_git

        if not dot_git.is_file():
            return dot_git

        text = dot_git.read_text(encoding="utf-8")
    except OSError:
        return dot_git

    line = text.strip()
    if not line.startswith(_GITDIR_PREFIX):
        return dot_git

    pointer = line[len(_GITDIR_PREFIX):].strip()
    if not pointer:
        return dot_git

    private_gitdir = Path(pointer)
    if not private_gitdir.is_absolute():
        # A relative `gitdir:` pointer (the submodule form) can itself
        # contain `..` traversal segments -- `Path.__truediv__` joins
        # lexically and does NOT collapse them, so a bare join can leave a
        # dangling `..` in the result. `os.path.normpath` collapses it with
        # no filesystem I/O, no symlink resolution -- see
        # `resolve_git_common_dir`'s identical join below, where the same
        # unresolved-`..` shape was reproduced live (a linked worktree's
        # `commondir` file content is always relative).
        private_gitdir = Path(os.path.normpath(repo_root / private_gitdir))

    return private_gitdir


def resolve_git_common_dir(repo_root: Union[str, Path]) -> Path:
    """Resolve `repo_root`'s git COMMON dir without spawning `git`.

    - `<repo_root>/.git` is a directory -> return it (plain clone; the
      overwhelmingly common case, byte-for-byte identical to the pre-change
      literal join).
    - `<repo_root>/.git` is a file -> resolve the private gitdir via
      `resolve_git_dir` (same `gitdir:` pointer parse, submodule-relative
      case included), then:
        - If that private gitdir contains a `commondir` file (the linked-
          worktree case), read it and resolve its contents against the
          private gitdir -> that is the common dir.
        - Otherwise (submodule, `--separate-git-dir`) the private gitdir IS
          the common dir.
    - Anything else -- missing `.git` entirely, an OSError reading it, a
      pointer file that doesn't start with `gitdir:`, or any other
      unparseable content -- fails open to the literal `<repo_root>/.git`
      join, silently (no print; this runs under a hook that must stay
      quiet). The degraded case is never worse than the pre-change
      behavior.
    """
    repo_root = Path(repo_root)
    dot_git = repo_root / ".git"

    private_gitdir = resolve_git_dir(repo_root)
    if private_gitdir == dot_git:
        # `resolve_git_dir` returns the literal `.git` join both for the
        # plain-directory case (correct: private == common there) and for
        # every fail-open case (missing/unreadable/unparseable `.git`) --
        # in both, the common dir is the same literal join.
        return dot_git

    return common_dir_from_gitdir(private_gitdir, dot_git)


def common_dir_from_gitdir(
    private_gitdir: Union[str, Path], fallback: Union[str, Path]
) -> Path:
    """Resolve the git COMMON dir from an ALREADY-RESOLVED private gitdir,
    without spawning `git` and without needing the repo toplevel.

    Split out of `resolve_git_common_dir` (which keeps calling it, with
    `fallback=<repo_root>/.git`, so that function's behaviour is unchanged
    for every existing caller). The split exists because a caller that
    already HOLDS a private gitdir should not have to re-derive a toplevel
    to get a common dir: `resolve_git_common_dir` takes a repo ROOT and
    computes `<repo_root>/.git` itself, so reaching it from a gitdir means
    paying a `git rev-parse --show-toplevel` spawn purely to throw the
    answer away.

    That spawn is not merely wasteful, it is a CORRECTNESS hazard on this
    box. `subagent_sandbox.engine.resolve_git_root` documents `None` as
    fail-open for "not a git repo, git missing, transient spawn error", and
    under the documented load norm (`docs/wiki/machine-load-norm.md`) the
    transient case is routine. A consumer that reads that `None` as a
    negative answer rather than as "unresolved" inverts its own verdict --
    which is exactly the live 2026-08-21 defect where the cross-repo write
    bump denied a session a write to its OWN repo (bug-backlog row
    `2026-08-21-foreign-write-guard-denies-own-repo-push-on-unresolved-target-root`).
    Callers holding a gitdir reach a common dir through here instead, and
    the failure mode cannot arise.

    `fallback` is returned only on `OSError` reading the `commondir` file --
    the caller names it because the right degraded answer differs by call
    site (`resolve_git_common_dir` wants its literal `<repo_root>/.git`
    join; a gitdir-holding caller wants the gitdir itself).
    """
    private_gitdir = Path(private_gitdir)
    try:
        commondir_file = private_gitdir / "commondir"
        if commondir_file.is_file():
            common_pointer = commondir_file.read_text(encoding="utf-8").strip()
            if common_pointer:
                common_dir = Path(common_pointer)
                if not common_dir.is_absolute():
                    # A linked worktree's `commondir` file content is
                    # ALWAYS relative (git's own convention -- typically
                    # `../..`) and joining it lexically leaves that `..`
                    # unresolved in the returned path. Downstream callers
                    # (e.g. `lifecycle.main_worktree_root`'s bare `.parent`,
                    # which assumes a lexically clean absolute path) then
                    # silently mis-resolve the main worktree root by one or
                    # more directory levels. `os.path.normpath` collapses
                    # the traversal with no filesystem I/O or symlink
                    # resolution, unlike `Path.resolve()`.
                    #
                    # Accepted limitation, stated rather than implied:
                    # lexical collapse and the kernel disagree whenever ANY
                    # symlinked path segment is collapsed past — `<x>/link/..`
                    # normalizes to `<x>`, while the OS would land in the
                    # link target's parent. That segment need not be `.git`
                    # itself: a symlinked intermediate directory, or a
                    # symlinked linked-worktree directory, collapses just as
                    # wrongly. Taken deliberately: `.resolve()` is forbidden
                    # here (see this function's rationale), and the
                    # un-collapsed form was wrong for EVERY `.parent`
                    # consumer, symlink or not.
                    common_dir = Path(os.path.normpath(private_gitdir / common_dir))
                return common_dir
    except OSError:
        return Path(fallback)

    return private_gitdir
