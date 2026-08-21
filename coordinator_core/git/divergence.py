"""coordinator_core.git.divergence -- the index/worktree divergence
predicate shared by `bash_guards.commit_tripwires.check_staged_pathspec_
divergence` (Check 13, SC-DR-015) and any future selector that needs the
same answer: for a given set of paths, which of them have STAGED (index)
content that differs from their WORKTREE content right now.

Extracted from `commit_tripwires.py` (was inline at roughly :824-833 before
this port). The ANSWER is unchanged; the way it is read is not -- one `git
status --porcelain=v2` spawn replaced the `git diff --cached --name-only` /
`git diff --name-only` pair, see `diverging_paths` for the equivalence. A
second, independently-maintained copy of this sequence is exactly the
failure mode this module exists to prevent: the
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
from typing import List, NamedTuple, Optional, Tuple

from coordinator_core.win_portability import no_console_creationflags


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
            **no_console_creationflags(),
        )
    except subprocess.TimeoutExpired:
        return -1, ""
    except OSError:
        return 127, ""
    return result.returncode, result.stdout


class V2Record(NamedTuple):
    """One `git status --porcelain=v2` `1` record, field-named.

    `x`/`y`  -- index-vs-HEAD and worktree-vs-index status characters.
    `m_head`/`m_index` -- the path's mode in HEAD and in the index. A pure
        mode toggle (`git update-index --chmod=+x` under `core.fileMode=
        false`) is `m_head != m_index` with `sha_head == sha_index`.
    `sha_head`/`sha_index` -- the path's blob OID in HEAD and in the index.

    Deliberately carries every field of the record rather than the two the
    first caller needed: one `git status` spawn already paid for all of them,
    and a second caller re-spawning `git` to read a field this one discarded
    is the exact cost this module exists to stop paying.
    """

    x: str
    y: str
    m_head: str
    m_index: str
    sha_head: str
    sha_index: str


def parse_v2_records(out: str) -> "dict[str, V2Record]":
    """Parse `git status --porcelain=v2 -z --no-renames` into `{path: V2Record}`.

    A `1` record is `1 <XY> <sub> <mH> <mI> <mW> <hH> <hI> <path>` -- eight
    space-delimited fields ahead of the path, which is taken as the remainder
    so an embedded space survives. `X` is the index-vs-HEAD status (what `git
    diff --cached` reports) and `Y` the worktree-vs-index status (what a bare
    `git diff` reports); `.` in either position means "unchanged on that
    axis".

    `--no-renames` is load-bearing, not cosmetic: it suppresses the `2`
    record, whose original path is a SECOND NUL-separated field rather than
    part of the same record. Every other record type (`u` unmerged, `?`
    untracked, `!` ignored) is skipped -- none of them carries an XY pair,
    and the two `git diff` invocations this parser replaces reported nothing
    for those paths either.

    Negative-spec: do NOT switch this to `splitlines()`. `-z` is what makes a
    path containing a newline parseable at all, and it is also what keeps
    paths RAW -- the `git diff --name-only` pair this replaced returned
    non-ASCII paths C-quoted, which never matched the caller's own key
    strings (`git_native.commit_scoped`'s `known_diverged & set(path_list)`).
    """
    recs: "dict[str, V2Record]" = {}
    for field in out.split("\0"):
        if not field or field[0] != "1":
            continue
        parts = field.split(" ", 8)
        if len(parts) < 9 or len(parts[1]) < 2:
            continue
        recs[parts[8]] = V2Record(
            x=parts[1][0],
            y=parts[1][1],
            m_head=parts[3],
            m_index=parts[4],
            sha_head=parts[6],
            sha_index=parts[7],
        )
    return recs


def diverging_paths(
    paths: List[str],
    cwd: Optional[str] = None,
    *,
    timeout: float = 2.0,
    fail_loud: bool = False,
) -> List[str]:
    """Return the sorted subset of `paths` whose STAGED (index) content
    differs from their WORKTREE content, scoped to `paths` via a trailing
    `--` pathspec.

    ONE spawn, not two. This is the same set the paired `git diff --cached
    --name-only` / `git diff --name-only` intersection returned, read instead
    off a single `git status --porcelain=v2` XY pair: `X` IS the
    `--cached` answer and `Y` IS the bare-diff answer, so the intersection
    becomes `X != "." and Y != "."` over one record set. Verified equal on
    the live tree before the swap -- the spawn is the cost here, not the
    query (DR-344: `git --version` alone is a whole process), and this
    predicate is called TWICE per `ceremony.scoped_git_commit`
    (`commit_pipeline` then `git_native.commit_scoped`), so the halving lands
    twice.

    Negative-spec: do NOT "restore" the two-diff form to avoid parsing v2.
    The parse is ~15 lines in `parse_v2_records`; the second spawn is ~1s of wall
    on a loaded box and buys nothing.

    `timeout` -- per-call timeout in seconds, forwarded to `_run_git`.
    Default `2.0` matches Check 13's original advisory posture (a
    pathspec-scoped status is proportional to the batch size, not repo size,
    so a healthy process clears this comfortably; a caller under real timeout
    pressure -- e.g. Windows spawn-tax, a large concurrent-load repo --
    should pass a wider value explicitly rather than have this default
    silently grow for everyone, including Check 13's low-stakes advisory
    use).

    `fail_loud` -- when `False` (the default, and Check 13's usage,
    unchanged), a `git` failure or timeout collapses to `[]` same as
    "no divergence found" -- never raises. When `True`, the same failure
    raises `DivergenceCheckFailed` instead, so a caller that uses this
    answer to pick a commit MECHANISM (rather than merely to decide whether
    to print an advisory warning) can tell "clean" apart from "indeterminate"
    and refuse to silently guess. See `DivergenceCheckFailed` for the
    incident this distinction closes.
    """
    if not paths:
        return []

    rc, out = _run_git(
        # `--no-optional-locks` is not optional here: the `git diff` pair this
        # replaced never touched `.git/index.lock`, but `git status` takes it
        # opportunistically to write back its stat cache -- on this shared
        # worktree that is contention the predicate did not previously add.
        # Same reason `git_native.status_porcelain` carries it.
        ["--no-optional-locks", "status", "--porcelain=v2", "-z", "--no-renames", "--", *paths],
        cwd,
        timeout=timeout,
    )
    if rc != 0:
        if fail_loud:
            raise DivergenceCheckFailed(
                f"diverging_paths: `git status --porcelain=v2` failed or timed out "
                f"(rc={rc}) for {len(paths)} path(s) -- divergence indeterminate"
            )
        return []

    return sorted(p for p, r in parse_v2_records(out).items() if r.x != "." and r.y != ".")
