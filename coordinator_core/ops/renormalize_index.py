"""
coordinator_core.ops.renormalize_index — ported from
coordinator/bin/coordinator-renormalize-index (DOE-PORT bin-entrypoint variant).

Purpose: clears EOL "phantom-dirty" index entries safely under concurrent-EM sessions.

THE BUG. On Git-for-Windows a tracked file's index entry can record a stale line-ending
-era blob size while HEAD and the worktree already agree on the normalized content.
`git diff` / `git diff --cached` renormalize then hash, so they see the content as equal
and report nothing to commit. But `git status` uses the recorded blob SIZE as a stat
shortcut, and size differs across the EOL round-trip -- so the file is flagged modified
forever. `git update-index --refresh` (even --really-refresh) cannot clear it because it
compares raw worktree bytes (no renormalization); `core.checkStat minimal` cannot either,
because size is compared even by minimal. The result is a perpetually phantom-dirty tree
that makes `git status` lie about thousands of files.

THE SAFE FIX (no commit needed). For each phantom path a plain `git add <path>`
re-stages the worktree content; because `git add` applies the gitattributes/autocrlf
filter, that content renormalizes to the SAME blob the index/HEAD already hold, so
NOTHING committable is produced -- only the index stat-cache is refreshed so size matches
the worktree again and `git status` goes clean.

CONCURRENT-EM SAFETY (the reason this is not a blanket `git add --renormalize .`). A
blanket renormalize re-stages every modified file's CURRENT worktree content --
absorbing siblings' live uncommitted edits into whatever commit follows, and clobbering
a sibling's mid-commit staged blob. This tool instead refreshes ONLY the phantom set:

    phantoms = (git ls-files -m)  MINUS  (git diff --name-only)  MINUS  deleted-in-worktree
             = stat-modified       MINUS  real worktree-vs-index content diffs

A path with a genuine worktree edit, OR a path a sibling has staged (index != HEAD with a
differing worktree), appears in `git diff --name-only` and is therefore EXCLUDED.

RESIDUAL RACE (honest scope -- NOT "by construction impossible"). The set is computed
from a snapshot; the `git add` fires later. If a sibling overwrites a phantom-classified
path with a real edit in that window, the add would stage their live content. The window
is shrunk to near-zero by re-confirming the set against a FRESH `git diff` taken
immediately before staging (a path that became a real edit is dropped). The residual
sub-millisecond window's worst outcome is bounded: the sweep only ever `git add`s, NEVER
commits, so an absorbed edit sits merely STAGED (visible in `git status`, recoverable)
and is caught downstream by coordinator-safe-commit's touched-files filter before it can
enter any commit. Safe to run on a live shared tree at any time.

Modes:
    renormalize_index.main([])            # refresh phantom entries in the current repo
    renormalize_index.main(["--check"])   # report the phantom count only; never writes

Exit codes:
    0 -- clean (no phantoms), counted (--check), refreshed, or deferred (index.lock present).
    1 -- not a git repository, a required `git diff` OR `git ls-files -m` probe FAILED
         (cannot safely classify), or the `git add` refresh reported a failure for at
         least one path (--ignore-errors still refreshes the rest best-effort, but git's
         exit is non-zero if ANY path failed -- callers cannot distinguish partial from
         total failure from the exit code alone).

Negative-spec (faithfully reproduced bash-oracle behavior -- do NOT "fix" mid-port):
    - Does NOT commit anything, ever -- only `git add` (stage), never `git commit`.
    - Does NOT run a blanket `git add --renormalize .` -- only the computed phantom set,
      confirmed twice against fresh `git diff` snapshots.
    - Does NOT stage a worktree-deleted path -- `git ls-files -m` reports a deleted
      tracked file as modified, but staging a deletion is a real change, not a stat
      refresh, so deleted paths are excluded from the phantom set.
    - The bash oracle's bash-4+ version guard (portability no-op on stock macOS bash 3.2)
      has no equivalent here -- this port never spawns bash/sh, so there is no shell
      version to gate on; the guard is dropped entirely, not translated.
    - Does NOT use a `-e`-style fail-fast; a `git diff`/`git ls-files` NON-ZERO exit from
      an otherwise-successful invocation is not conflated with a real subprocess failure
      (OSError/missing git) -- only the latter aborts loud.

Disclosed divergence from the oracle (Review: code-reviewer, Finding 2 -- was undisclosed
and mislabeled prior to this note): the bash oracle pipes `git ls-files -m -z` into a
`while` loop via process substitution and never checks its exit status, so a hard
`ls-files` failure (corrupted index, git internal error) is silently treated as "0
phantoms found" (exit 0). This port instead treats an `ls-files -m` subprocess failure
the same as a `git diff` failure -- `_git_ls_files_modified` returning None aborts loud
with exit 1 -- because silently reporting "clean" on a real probe failure would be a
worse failure mode than a loud abort. This is an intentional, disclosed improvement over
the oracle, not an oversight.
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import List, Optional, Set


def _diff_probe_failed_message(probe: str) -> str:
    """Shared wording for the three fail-loud abort sites (`git diff` x2, `git ls-files
    -m` x1) so the text and the failing probe name never drift apart.
    # Review: code-reviewer (Finding 8) -- was duplicated verbatim across three call
    # sites, one of which (the ls-files branch) named the wrong probe; extracted to keep
    # the wording and the probe name coupled at a single definition."""
    return (
        f"coordinator-renormalize-index: ERROR — {probe} failed; "
        "aborting to avoid misclassifying real edits as phantoms"
    )


def _git_rev_parse_git_dir(cwd: Optional[str] = None) -> Optional[str]:
    # `git rev-parse --git-dir` output is always a clean ASCII path (never an arbitrary
    # filename byte sequence), so text=True is safe here -- unlike _git_diff_name_only /
    # _git_ls_files_modified below, which decode manually with surrogateescape because
    # their NUL-list output can contain arbitrary filename bytes.
    # Review: code-reviewer (Finding 9) -- mixed text=True/manual-decode style read as an
    # oversight rather than a deliberate per-output choice; this comment disambiguates.
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            capture_output=True,
            text=True,
            cwd=cwd,
            check=False,
        )
    except (OSError, FileNotFoundError):
        return None
    if proc.returncode != 0:
        return None
    git_dir = proc.stdout.strip()
    if not git_dir:
        return None
    return git_dir


def _git_diff_name_only(cwd: Optional[str] = None) -> Optional[Set[str]]:
    """Real worktree-vs-index content diffs (renormalize-aware). None on a hard
    subprocess/git failure -- the caller must fail loud rather than misclassify."""
    try:
        proc = subprocess.run(
            ["git", "diff", "--name-only", "-z"],
            capture_output=True,
            cwd=cwd,
            check=False,
        )
    except (OSError, FileNotFoundError):
        return None
    if proc.returncode != 0:
        return None
    raw = proc.stdout.decode("utf-8", errors="surrogateescape")
    return {p for p in raw.split("\0") if p}


def _git_ls_files_modified(cwd: Optional[str] = None) -> Optional[List[str]]:
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-m", "-z"],
            capture_output=True,
            cwd=cwd,
            check=False,
        )
    except (OSError, FileNotFoundError):
        return None
    if proc.returncode != 0:
        return None
    raw = proc.stdout.decode("utf-8", errors="surrogateescape")
    return [p for p in raw.split("\0") if p]


def _path_exists_in_worktree(cwd: str, rel_path: str) -> bool:
    full = os.path.join(cwd, rel_path) if cwd else rel_path
    return os.path.lexists(full)


def _git_add_pathspec_from_stdin(paths: List[str], cwd: Optional[str] = None) -> bool:
    """Stage exactly `paths` via NUL-safe pathspec-from-stdin (never shell-globbed, never
    flag-parsed -- safe for leading-dash / space-containing names). Returns True on a
    clean git exit, False if git reported any failure (--ignore-errors still refreshes
    the rest best-effort)."""
    payload = b"\0".join(p.encode("utf-8", errors="surrogateescape") for p in paths) + b"\0"
    try:
        proc = subprocess.run(
            [
                "git",
                "add",
                "--ignore-errors",
                "--pathspec-from-file=-",
                "--pathspec-file-nul",
            ],
            input=payload,
            capture_output=True,
            cwd=cwd,
            check=False,
        )
    except (OSError, FileNotFoundError):
        return False
    return proc.returncode == 0


def main(argv: List[str], cwd: Optional[str] = None) -> int:
    """CLI entrypoint: coordinator-renormalize-index.

    `cwd` lets callers/tests target a specific repo without chdir-ing the process;
    defaults to the process's current working directory when omitted.
    """
    check_only = bool(argv) and argv[0] == "--check"

    git_dir = _git_rev_parse_git_dir(cwd)
    if git_dir is None:
        print("coordinator-renormalize-index: not a git repository", file=sys.stderr)
        return 1

    real = _git_diff_name_only(cwd)
    if real is None:
        print(_diff_probe_failed_message("git diff"), file=sys.stderr)
        return 1

    modified = _git_ls_files_modified(cwd)
    if modified is None:
        # Review: code-reviewer (Finding 2) -- was mislabeled as "git diff failed" even
        # though this branch is the git-ls-files probe; disclosed divergence documented
        # in the module docstring's negative-spec section.
        print(_diff_probe_failed_message("git ls-files -m"), file=sys.stderr)
        return 1

    phantoms = [
        p
        for p in modified
        if p not in real and _path_exists_in_worktree(cwd or "", p)
    ]

    n = len(phantoms)
    if n == 0:
        print(
            "coordinator-renormalize-index: no EOL phantom-dirty entries (clean)",
            file=sys.stderr,
        )
        return 0

    if check_only:
        entries = "entry" if n == 1 else "entries"
        print(
            f"coordinator-renormalize-index: {n} EOL phantom-dirty {entries} "
            "detected (--check: not refreshed)",
            file=sys.stderr,
        )
        return 0

    # Review: code-reviewer (Finding 1) -- `os.sep` is '\\' on Windows, but
    # `git rev-parse --git-dir` always emits forward-slash paths on every platform, and
    # an absolute Windows git-dir is drive-letter-prefixed (C:/repo/.git), never
    # backslash-prefixed. `startswith(os.sep)` was False for both a relative git-dir AND
    # an absolute Windows one, silently double-joining the latter onto cwd.
    # `os.path.isabs` handles both POSIX and Windows absolute forms correctly.
    if not os.path.isabs(git_dir) and cwd:
        git_dir_abs = os.path.join(cwd, git_dir)
    else:
        git_dir_abs = git_dir
    lock_path = os.path.join(git_dir_abs, "index.lock")
    if os.path.exists(lock_path):
        print(
            "coordinator-renormalize-index: index.lock present (live commit) — "
            "deferring sweep",
            file=sys.stderr,
        )
        return 0

    real2 = _git_diff_name_only(cwd)
    if real2 is None:
        print(_diff_probe_failed_message("git diff"), file=sys.stderr)
        return 1

    safe = [
        p
        for p in phantoms
        if p not in real2 and _path_exists_in_worktree(cwd or "", p)
    ]

    m = len(safe)
    if m == 0:
        print(
            f"coordinator-renormalize-index: all {n} candidate(s) became live edits "
            "before staging — nothing refreshed",
            file=sys.stderr,
        )
        return 0

    # CATASTROPHE GUARD (defence-in-depth, mirrors the oracle's own re-check comment):
    # `m = len(safe)` makes `m == 0` and `not safe` the same condition on the same
    # unmutated list today, so this re-check is unreachable by construction as written.
    # It is kept anyway as a second guard immediately adjacent to the `git add` call --
    # an empty pathspec to `git add --pathspec-from-file=-` means "add everything" (≈
    # `git add .`), absorbing the entire concurrent tree, so this guards against a
    # *future* edit inserting code between the `m == 0` check above and the pipe below
    # that could repopulate/mutate `safe`.
    # Review: code-reviewer (Finding 7) -- flagged as dead code; annotated with the
    # defense-in-depth rationale rather than dropped, per the oracle's own CATASTROPHE
    # GUARD comment at coordinator-renormalize-index:175-179.
    if not safe:
        print(
            "coordinator-renormalize-index: internal — SAFE empty at staging step "
            "(refused)",
            file=sys.stderr,
        )
        return 0

    if _git_add_pathspec_from_stdin(safe, cwd):
        entries = "entry" if m == 1 else "entries"
        print(
            f"coordinator-renormalize-index: refreshed {m} EOL phantom-dirty index "
            f"{entries} (0 real edits touched)",
            file=sys.stderr,
        )
        return 0

    print(
        "coordinator-renormalize-index: WARNING — git add refresh failed for one or "
        "more paths (others refreshed best-effort)",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
