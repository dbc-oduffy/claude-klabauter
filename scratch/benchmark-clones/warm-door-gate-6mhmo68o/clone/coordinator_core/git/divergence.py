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

C3e (2026-08-26, docs/dispatch-briefs/2026-08-26-the-commit-op-stops-
asking-git-eleven-times/C3e.md): `diverging_paths` no longer spawns `git`
for the common case. Both axes now have an in-process answer: `X`
(index-vs-HEAD) via `coordinator_core.git.git_state.read_index`/`head_
blobs` (already spawn-free), `Y` (worktree-vs-index) via `coordinator_core.
git.git_index.scoped_status`'s stat fast path, settled for a stat-mismatch
"candidate" by `coordinator_core.git.content_hash.content_matches_index_
sha`'s NORMALIZE-THEN-HASH -- see that module for the verification this
rests on and the exact preconditions it declines outside of. A path that
DECLINES (the normalizer's preconditions do not hold) falls back to the
SAME scoped `git status --porcelain=v2` spawn this module always issued,
restricted to just the undetermined subset -- this is an optimisation with
an escape hatch, never a narrowing of what this predicate can answer.

Spec backlink: docs/wiki/scoped-safety-commits.md § SC-DR-015
Spec backlink: docs/plans/2026-07-27-computed-commit-mechanism-selection.md
  (`fail_loud=` split -- see `DivergenceCheckFailed` below; a genuine `git
  diff` failure/timeout must be indeterminate, not "no divergence", for a
  caller that decides the commit MECHANISM on this answer.)
Spec backlink: docs/dispatch-briefs/2026-08-26-the-commit-op-stops-asking-
  git-eleven-times/C3e.md
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, NamedTuple, Optional, Tuple

from coordinator_core.git.content_hash import content_matches_index_sha
from coordinator_core.git.git_index import IndexParseError as _IndexStatIndexParseError
from coordinator_core.git.git_index import scoped_status as _scoped_worktree_status
from coordinator_core.git.git_state import IndexParseError as _FullIndexParseError
from coordinator_core.git.git_state import head_blobs as _head_blobs
from coordinator_core.git.git_state import read_index as _read_index
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


def _repo_relative_key(root: Path, p: str) -> str:
    """Return `p` in the CWD-relative, forward-slashed form git itself
    reports it in, mirroring `commit_pipeline._worktree_key` (carried, not
    re-derived -- that function's own docstring names the incident this
    guards: `git ls-files --deleted -- /abs/path/f` scopes correctly and
    PRINTS `f`, so a caller-supplied ABSOLUTE path never matches a
    relative-keyed lookup by raw string equality).

    In-process readers (`git_state.read_index`, `git_index.scoped_status`,
    `head_blobs`) key EVERYTHING off this same relative form -- there is no
    spawn here to do git's own resolution for us, so this function is load-
    bearing for `diverging_paths` to settle an absolute-path input at all,
    not merely a formatting nicety.

    A path outside `root` (or one that resolves outside it) is returned
    unchanged: it cannot correspond to an index entry anyway, and falling
    through to "not staged" (excluded from the result) is the correct
    classification for it."""
    candidate = Path(p)
    if not candidate.is_absolute():
        return p.replace("\\", "/")
    for base in (root, root.resolve()):
        try:
            return candidate.relative_to(base).as_posix()
        except ValueError:
            continue
    try:
        return candidate.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return p.replace("\\", "/")


def _spawn_diverging_subset(
    undetermined: List[str],
    cwd: Optional[str],
    *,
    timeout: float,
    fail_loud: bool,
) -> List[str]:
    """The original `git status --porcelain=v2` spawn, restricted to the
    subset of paths C3e's in-process settling could not determine. Same
    contract `diverging_paths` always had for its whole batch -- kept
    verbatim, not re-derived, as the escape hatch every DECLINE above
    falls through to."""
    rc, out = _run_git(
        # `--no-optional-locks` is not optional here: the `git diff` pair this
        # replaced never touched `.git/index.lock`, but `git status` takes it
        # opportunistically to write back its stat cache -- on this shared
        # worktree that is contention the predicate did not previously add.
        # Same reason `git_native.status_porcelain` carries it.
        ["--no-optional-locks", "status", "--porcelain=v2", "-z", "--no-renames", "--", *undetermined],
        cwd,
        timeout=timeout,
    )
    if rc != 0:
        if fail_loud:
            raise DivergenceCheckFailed(
                f"diverging_paths: `git status --porcelain=v2` failed or timed out "
                f"(rc={rc}) for {len(undetermined)} undetermined path(s) -- "
                "divergence indeterminate"
            )
        return []

    return [p for p, r in parse_v2_records(out).items() if r.x != "." and r.y != "."]


def diverging_paths(
    paths: List[str],
    cwd: Optional[str] = None,
    *,
    timeout: float = 2.0,
    fail_loud: bool = False,
) -> List[str]:
    """Return the sorted subset of `paths` whose STAGED (index) content
    differs from their WORKTREE content, scoped to `paths`.

    ZERO spawns in the common case (C3e, 2026-08-26, docs/dispatch-briefs/
    2026-08-26-the-commit-op-stops-asking-git-eleven-times/C3e.md). This is
    the same `X != "." and Y != "."` intersection `git status --porcelain=v2`
    always answered (`X` = index-vs-HEAD, `Y` = worktree-vs-index), now read
    off two in-process sources instead of one spawn:

      `X` -- `coordinator_core.git.git_state.read_index` (staged mode+sha)
          compared against `head_blobs` (HEAD tree mode+sha for the same
          paths). Already spawn-free; unchanged by this chunk.
      `Y` -- `coordinator_core.git.git_index.scoped_status`'s stat fast
          path (git's own `ce_match_stat`): a stat match reads `"clean"`
          (`Y = "."`) without a byte read. A stat MISMATCH ("candidate")
          is settled by `coordinator_core.git.content_hash.content_
          matches_index_sha` -- NORMALIZE-THEN-HASH against the index sha,
          verified byte-identical to `git hash-object` (see that module),
          restricted to the exact precondition set it was verified under.
          `"deleted"` (worktree file gone) is an unconditional `Y != "."`.

    THE PROHIBITION IS SATISFIED, NOT LIFTED. `coordinator_core/git/
    git_state.py`'s module docstring forbids hashing RAW worktree bytes
    and comparing the result to an index sha, because that NAIVE
    comparison is wrong under the checkin filters -- measured wrong, 326
    of 400 clean tracked files on this repo under `core.autocrlf=true`,
    the reverted `da156a723` incident. This function never does that: a
    candidate is only ever settled by `content_matches_index_sha`, which
    hashes NORMALIZED bytes (git's own checkin-side transform, reproduced
    verbatim) and DECLINES -- returns `None` -- for every path outside the
    precondition set that transform was verified under. A DECLINE (or an
    `X`-positive path whose `Y` verdict this function cannot itself
    settle -- `"deleted"`/`"untracked"`, and any `content_matches_index_
    sha` decline) falls through to `_spawn_diverging_subset`, the SAME
    scoped `git status --porcelain=v2` spawn this function always issued,
    now restricted to just the undetermined remainder. This is an
    optimisation with an escape hatch, never a narrowing of what this
    predicate can answer -- every case the old whole-batch spawn resolved,
    the new fallback still resolves, for the (typically empty, always
    bounded) subset that needed it.

    `timeout` -- per-call timeout in seconds, forwarded to the fallback
    spawn (unused when every path settles in-process). Default `2.0`
    matches Check 13's original advisory posture (a pathspec-scoped status
    is proportional to the batch size, not repo size, so a healthy process
    clears this comfortably; a caller under real timeout pressure -- e.g.
    Windows spawn-tax, a large concurrent-load repo -- should pass a wider
    value explicitly rather than have this default silently grow for
    everyone, including Check 13's low-stakes advisory use).

    `fail_loud` -- when `False` (the default, and Check 13's usage,
    unchanged), an in-process index read failure (`IndexParseError`) or a
    fallback `git` failure/timeout collapses to `[]` same as "no
    divergence found" -- never raises. When `True`, the same failure
    raises `DivergenceCheckFailed` instead, so a caller that uses this
    answer to pick a commit MECHANISM (rather than merely to decide
    whether to print an advisory warning) can tell "clean" apart from
    "indeterminate" and refuse to silently guess. See
    `DivergenceCheckFailed` for the incident this distinction closes.

    Negative-spec: do NOT "restore" the whole-batch spawn to avoid the
    in-process settle path. The two failure classes it closes over
    (`IndexParseError`/`IndexV4Unsupported` from either index reader) are
    already the SAME classes this function's `fail_loud` contract has
    always had to handle for a corrupt/unsupported index; folding a
    healthy batch back onto a spawn buys nothing the fallback subset
    doesn't already cover for the genuinely undetermined case.
    """
    if not paths:
        return []

    root = Path(cwd) if cwd is not None else Path(".")

    try:
        index_snapshot = _read_index(cwd if cwd is not None else ".")
    except (_FullIndexParseError, _IndexStatIndexParseError) as exc:
        if fail_loud:
            raise DivergenceCheckFailed(
                f"diverging_paths: in-process index read failed for "
                f"{len(paths)} path(s) -- divergence indeterminate ({exc})"
            ) from exc
        return []

    # Repo-relative, forward-slashed keys -- `read_index`/`scoped_status`/
    # `head_blobs` key everything this way, and (unlike the spawn this
    # replaces) there is no `git` process here to resolve an absolute-path
    # input on this function's behalf. See `_repo_relative_key`.
    relative = {p: _repo_relative_key(root, p) for p in paths}

    # Only a STAGED path can diverge at all (a `1` porcelain record needs an
    # index entry to exist in the first place) -- an unstaged path is
    # excluded here exactly as it would never appear in the old spawn's
    # output.
    staged = [p for p in paths if relative[p] in index_snapshot]
    if not staged:
        return []
    staged_rel = [relative[p] for p in staged]

    try:
        head = _head_blobs(cwd if cwd is not None else ".", staged_rel)
        worktree_verdicts = _scoped_worktree_status(cwd if cwd is not None else ".", staged_rel)
    except (_FullIndexParseError, _IndexStatIndexParseError) as exc:
        if fail_loud:
            raise DivergenceCheckFailed(
                f"diverging_paths: in-process worktree/HEAD read failed for "
                f"{len(staged)} staged path(s) -- divergence indeterminate ({exc})"
            ) from exc
        return []

    settled: "set[str]" = set()
    undetermined: List[str] = []

    for p in staged:
        rel = relative[p]
        idx_entry = index_snapshot[rel]
        head_entry = head.get(rel)
        x_diverged = head_entry is None or (idx_entry.mode, idx_entry.sha) != head_entry
        if not x_diverged:
            continue  # X == "." -- cannot diverge regardless of Y

        verdict = worktree_verdicts.get(rel, "untracked")
        if verdict == "clean":
            continue  # Y == "."
        if verdict == "deleted":
            settled.add(rel)
            continue
        if verdict == "candidate":
            match = content_matches_index_sha(root, rel, idx_entry.sha)
            if match is True:
                continue
            if match is False:
                settled.add(rel)
                continue
            undetermined.append(p)
            continue
        # "untracked" here means the index changed under us between the
        # snapshot above and this read -- indeterminate, not "clean".
        undetermined.append(p)

    if undetermined:
        settled.update(_spawn_diverging_subset(undetermined, cwd, timeout=timeout, fail_loud=fail_loud))

    return sorted(settled)
