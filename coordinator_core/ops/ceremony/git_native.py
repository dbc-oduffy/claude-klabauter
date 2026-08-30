"""
coordinator_core.ops.ceremony.git_native — Windows-safe shared git-subprocess helper.

Purpose: every native `git` subprocess invocation the `wsc_tail` rebuild issues for
staging and commit routes through the single `_git()` helper in this module, so the
Windows-safe subprocess flags are carried exactly once instead of being re-typed
(and re-forgotten) at each of the ~13 call sites the OLD `wsc_commit.py` scattered
them across with none of these flags present (the portability gap this chunk
closes).

NOT the sole native-git spawn path for `ceremony.scoped_git_commit` as a whole:
`_handler`'s push-drain leg (`commit_pipeline._drain_pending_push_after_sync` ->
`auto_push.drain_pending_push` -> `coordinator_core/hooks/auto_push.py`'s
`_run_git`/`push_once`/`_is_ancestor`/`_invoke_cockpit_publish`), claim release
(`coordinator_core/session/scope.py :: _git_run`), `coordinator_core/git/
divergence.py :: _run_git`, and `coordinator_core/git/repo_root.py ::
_spawn_rev_parse` all spawn `git` without going through this module's `_git()`.

Flags carried by every invocation (AC3):
    creationflags=CREATE_NO_WINDOW — sourced from `coordinator_core.win_portability.
        no_console_creationflags()` (Windows-only; no-ops elsewhere). Suppresses
        the focus-stealing console popup a bare `subprocess.run` spawns per-invocation
        on Windows under a headless parent process.
    stdin=subprocess.DEVNULL — NOT carried by `commit_anchors.py` today. Load-bearing
        for the Windows non-hang: a git subprocess with inherited stdin can block
        indefinitely on an interactive prompt (credential helper, merge conflict editor,
        pager) — exactly the class of >120s wedge this rebuild targets. Must never be
        dropped from any wrapper added here.
    capture_output=True, text=True — every wrapper returns decoded stdout/stderr;
        callers never need to touch raw bytes or re-invoke with different capture flags.

Spec backlink: pln-rebuild-the-wsc-commit-ceremon-f7c2a0 § C1 (AC3 foundation).

Negative-spec (hard-won):
  - Does NOT shell out to bash, node, or any `.sh`/`.js` script — `git` only (AC2).
  - Does NOT use `shell=True` — argv lists only, never a shell-interpolated string.
  - Does NOT swallow `OSError`/`TimeoutExpired` silently — every wrapper surfaces a
    typed failure result rather than raising past the caller (callers are op handlers
    that must degrade gracefully, never crash the daemon on a git-availability blip).
  - Does NOT inherit the parent's stdin — see `stdin=DEVNULL` above.

REMOVED 2026-08-27 (PM ruling, abd587695): the in-plane archival sweep
`commit_pipeline._run_in_plane_archive_sweep` and its three legs are GONE from the
commit path. Text below describing it is retained only as history of why this code
looks the way it does -- it asserts nothing about the commit path today. Handoffs are
archived at the occasions that create the work (pickup, workstream-complete,
workday-complete, and the per-artifact lifecycle paths), never by sweeping a corpus on
commit. See state/kill-ledger.md.
"""

from __future__ import annotations

import contextvars
import functools
import ntpath
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple, Union

from coordinator_core.git.content_hash import (
    _attributes_pattern_matches,
    _autocrlf_checkin_normalize,
    _clean_filter_may_apply,
    _repo_autocrlf_true,
    _system_gitconfig_paths,
    _text_attribute_pinned,
)
from coordinator_core.git.commit_trailers import (
    _extract_trailer_block,
    can_format_trailers_in_process,
    compute_missing_trailer_args,
    format_trailers_in_process,
    read_trailer_value,
    trailer_values_from_argv,
)
from coordinator_core.git.divergence import (
    DivergenceCheckFailed,
    V2Record,
    diverging_paths,
)
from coordinator_core.git.git_dir import resolve_git_common_dir, resolve_git_dir
from coordinator_core.git.git_index import scoped_status as _git_index_scoped_status
from coordinator_core.git.git_objects import cas_ref, write_object
from coordinator_core.git.git_state import (
    IndexEntry,
    IndexParseError,
    head_blobs as _git_state_head_blobs,
    head_sha as _git_state_head_sha,
    head_tree_sha as _git_state_head_tree_sha,
    index_read_cache_scope,
    read_index,
    read_index_stat_identity,
    read_tree_spine,
)
from coordinator_core.git_lock_retry import run_with_lock_retry
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.git.run import REMOTE_BUDGET_SECS

#: Timeout (seconds) `commit_scoped()` gives each `diverging_paths()` `git
#: diff` call. Wider than Check 13's `2.0s` advisory default (see
#: `coordinator_core.git.divergence.diverging_paths` for that default's
#: reasoning) -- here a timeout is escalated to a FAILED commit
#: (`fail_loud=True`, below), not a silently-skipped warning, so it is worth
#: paying extra wall-clock to absorb a transient Windows spawn-tax/large-repo
#: hiccup before concluding the check is genuinely stuck. Still well under
#: `_DEFAULT_TIMEOUT_SECS` (60s, for `push`/`fetch`) -- a `git diff --name-
#: only` scoped to an explicit pathspec is proportional to the pathspec size,
#: not repo size, so a healthy process should never need anywhere near this.
_DIVERGENCE_CHECK_TIMEOUT_SECS = 5.0

#: Default subprocess timeout (seconds) for a single git invocation. Generous enough
#: for `git push`/`git fetch` on a slow link, bounded enough to never itself become
#: the >120s-wedge class this rebuild exists to kill.
_DEFAULT_TIMEOUT_SECS = 60

#: `_chunk_paths` + its budget constant were relocated (2026-08-26, C1 of
#: docs/plans/2026-08-26-the-archival-commit-helper-computes-its-own-tree.md)
#: to `coordinator_core.git.argv_batch` -- argv batching, not tree algebra,
#: does not belong in this module. Re-exported here under their original
#: names so every existing caller (`commit_pipeline.py`'s module-level
#: aliases, `commit_gates.py`'s direct import, `git_state.py`'s function-
#: scoped import) keeps importing what it imports today.
from coordinator_core.git.argv_batch import (  # noqa: E402
    _DIVERGENCE_CHECK_ARGV_BUDGET_CHARS,
    _chunk_paths,
)


def _diverging_paths_chunked(
    paths: Sequence[str],
    cwd: str,
    *,
    timeout: float,
) -> Set[str]:
    """Chunked `diverging_paths(..., fail_loud=True)` for `commit_scoped()`'s
    own divergence check -- closes the same Windows argv-length defect
    `commit_pipeline._diverging_paths_chunked` closes for `explicit_stage()`,
    one call site over (this function's own docstring there covers the full
    incident: at percolate-publish scale, ~2000-2700 paths on one `git diff
    --cached --name-only` argv exceeds the 32767-char Windows command-line
    cap, `subprocess` reports `rc=127`, and the whole batch reads as
    indeterminate even though nothing actually diverged). `git diff` cannot
    take a `--pathspec-from-file` (verified empirically against this
    machine's git 2.55.0.windows.3 -- usage error), unlike `git add`/`git
    commit` below, which is why this call is CHUNKED rather than converted
    to a pathspec file the way the agree-branch `git add` and the final
    `git commit` are (see `commit_scoped`'s own docstring for that split).

    Each chunk gets its own independent `diverging_paths()` call -- a
    path's divergence answer comes from exactly the one chunk it was placed
    in, never a whole-batch verdict ORed/ANDed across chunks, so a peer
    session's own deliberate partial-hunk staging protection (state/lessons/
    2026-08-14-partial-stage-protection-did-not-survive-a-moving-head.md)
    is preserved at scale, not just at the aggregate. A `DivergenceCheckFailed`
    from ANY chunk (a genuine `git diff` error, not an argv-length artifact
    -- each chunk is already sized to avoid that) propagates immediately and
    uncaught, exactly like an unchunked `fail_loud=True` call would --
    `commit_scoped()`'s own `try`/`except DivergenceCheckFailed` still
    refuses the whole call rather than guess at paths whose chunk never got
    an answer.
    """
    diverged: Set[str] = set()
    for chunk in _chunk_paths(list(paths)):
        diverged.update(diverging_paths(list(chunk), cwd=cwd, timeout=timeout, fail_loud=True))
    return diverged


def _v2_state_records_chunked(
    paths: Sequence[str],
    cwd: str,
    *,
    timeout: float,
) -> "Dict[str, V2Record]":
    """In-process `{path: V2Record}` map -- the spawn-free replacement for
    what was a chunked `git status --porcelain=v2 -z --no-renames`
    (C11, `state/dispatch-briefs/2026-08-23-the-scoped-commit-rebuilt-from-
    first-principles/C11.md`; census: `docs/plans/2026-08-23-the-scoped-
    commit-rebuilt-from-first-principles.md` "What the census established").
    `timeout` is accepted and unused -- kept so `commit_scoped()`'s call site
    does not need its own signature edit; nothing here can hang the way a
    subprocess can.

    The single state read `commit_scoped()` takes for BOTH of its
    branch-selection questions: content divergence (`X`/`Y`) and mode delta
    (`m_head`/`m_index` + `sha_head`/`sha_index`) -- unchanged from the
    porcelain-v2-backed version's own contract; only the SOURCE of each
    field moved off a `git` spawn:

      `m_index`/`sha_index` -- `coordinator_core.git.git_state.read_index`
          (already spawn-free, C2 promotion). Absent index entry -> the
          porcelain-v2 zero shape (`"000000"` mode, 40 `"0"` sha), exactly
          what git itself prints for a missing side of a changed-entry line.
      `m_head`/`sha_head`  -- `coordinator_core.git.git_state.read_tree_spine`
          (C3's in-process HEAD-tree reader -- walks only the directory
          spine each path needs, never spawns `git ls-tree`). `None` back
          from `read_tree_spine` (unresolvable/corrupt HEAD) folds to the
          same "absent from HEAD" zero shape as an unborn repo, matching
          `_head_blobs`'s own fold for that case one call site up.
      `x`   -- `"."` when `(m_head, sha_head) == (m_index, sha_index)`,
          else a non-`.` placeholder. `_diverged_from_records()` (the sole
          reader) only ever tests `!= "."`, never the letter itself, so this
          function is not required to reproduce git's actual status-letter
          vocabulary (`M`/`A`/`D`/...) -- only the same/differ verdict.
      `y`   -- derived from `coordinator_core.git.git_index.scoped_status`'s
          verdict for the path: `"clean"` -> `"."`, `"candidate"`/`"deleted"`
          -> a non-`.` placeholder. `"untracked"` (no index entry at all)
          also maps non-`.`, conservatively -- this call site's own `path_
          list` is always a path already staged or about to be, so an
          `"untracked"` verdict here means the world moved out from under
          this call, and the safe direction is to read it as changed, never
          as `"."`.

    Same fail-loud posture as the version this replaces: `read_index`/
    `scoped_status` raising `IndexParseError` (a malformed/unsupported/
    unmerged index) is re-raised as `DivergenceCheckFailed` rather than
    ever collapsing to an empty/partial map -- `commit_scoped()` still picks
    the commit MECHANISM off this answer, so an indeterminate read must
    never read as "clean".

    Negative-spec: this does NOT answer "does the worktree match HEAD".
    Same as the porcelain-v2-backed version, `_reject_stale_index_paths`,
    `_index_blobs` and `_head_blobs` stay on their own reads. Folding those
    in would silently turn "clean" into "absent"."""
    del timeout  # unused -- see docstring
    path_list = list(paths)
    try:
        index_snapshot = read_index(cwd)
        worktree_verdicts = _git_index_scoped_status(cwd, path_list)
    except IndexParseError as exc:
        raise DivergenceCheckFailed(
            f"_v2_state_records_chunked: in-process index read failed for "
            f"{len(path_list)} path(s) -- index/worktree state indeterminate ({exc})"
        ) from exc
    spine = read_tree_spine(cwd, path_list)

    zero_sha = "0" * 40
    records: "Dict[str, V2Record]" = {}
    for p in path_list:
        idx_entry = index_snapshot.get(p)
        m_index = f"{idx_entry.mode:06o}" if idx_entry is not None else "000000"
        sha_index = idx_entry.sha if idx_entry is not None else zero_sha

        head_entry = None
        if spine is not None:
            parts = p.split("/")
            dirpath = "/".join(parts[:-1])
            name = parts[-1]
            dir_entries = spine.get(dirpath)
            if dir_entries is not None:
                head_entry = dir_entries.get(name)
        m_head = f"{head_entry[0]:06o}" if head_entry is not None else "000000"
        sha_head = head_entry[1] if head_entry is not None else zero_sha

        x = "." if (m_head, sha_head) == (m_index, sha_index) else "M"
        y_verdict = worktree_verdicts.get(p, "untracked")
        y = "." if y_verdict == "clean" else "M"

        records[p] = V2Record(
            x=x, y=y, m_head=m_head, m_index=m_index, sha_head=sha_head, sha_index=sha_index
        )
    return records


def _mode_delta_paths_chunked(
    paths: Sequence[str],
    cwd: str,
    *,
    timeout: float,
) -> Set[str]:
    """Chunked `git diff --cached --raw -- <paths>`, scoped to the subset of
    `paths` whose STAGED (index) file mode differs from HEAD's while the
    blob content is IDENTICAL -- the observation `commit_scoped()`'s own
    divergence check (`_diverging_paths_chunked`, above) was missing, which
    let a mode-only delta reach the agree branch's path-restricted commit
    (`commit_with_message_file_pathspec_scoped`) and be silently discarded
    under `core.fileMode=false` (DR-151 -- see that wrapper's own docstring,
    and `ops/ceremony/commit_exec_bit.py`'s module docstring for the
    original naming of this footgun). `--raw` emits `:<oldmode> <newmode>
    <oldsha> <newsha> <status>\\t<path>` per changed path -- both the old
    and new mode are already on ONE line, so no second call is needed the
    way a sha-only read would require.

    Same chunking seam as `_diverging_paths_chunked()` above (`_chunk_
    paths()`), for the identical amplification-gate reason: this adds one
    extra batched git read per `commit_scoped()` call, proportional to the
    pathspec, never a per-path spawn
    (`coordinator_core/tests/test_no_unbatched_per_item_git_spawn.py`).

    Restricted to `<oldsha> == <newsha>` -- a mode delta on a path whose
    CONTENT also changed (e.g. a brand-new file's `A` status, which carries
    an `<oldmode>` of `000000`) is not this defect's shape: that path is
    already correctly handled by whichever branch the content divergence
    itself selects. Only a pure mode toggle (same blob, different mode) --
    exactly what `git update-index --chmod=+x` produces under `core.
    fileMode=false` -- is what this function surfaces, so a caller adding
    this set to `diverged` never reroutes a path for a reason unrelated to
    the mode-preservation defect.

    A `git diff` failure or timeout in ANY chunk raises `DivergenceCheckFailed`
    (reusing `coordinator_core.git.divergence`'s own exception, already
    imported into this module) rather than collapsing to "no delta found" --
    same fail-loud posture as `_diverging_paths_chunked()` above, for the
    same reason: `commit_scoped()` picks the commit MECHANISM off this
    answer, so an indeterminate read must never be silently treated as
    clean.
    """
    mode_delta: Set[str] = set()
    for chunk in _chunk_paths(list(paths)):
        result = _git(["diff", "--cached", "--raw", "--", *chunk], cwd=cwd, timeout=timeout)
        if not result.ok:
            raise DivergenceCheckFailed(
                f"_mode_delta_paths_chunked: `git diff --cached --raw` failed or "
                f"timed out (rc={result.returncode}) for {len(chunk)} path(s) -- "
                "mode-delta indeterminate"
            )
        for line in result.stdout.splitlines():
            if not line or not line.startswith(":"):
                continue
            meta, _, path = line.partition("\t")
            fields = meta.split()
            if len(fields) < 5:
                continue
            old_mode = fields[0][1:]
            new_mode = fields[1]
            old_sha = fields[2]
            new_sha = fields[3]
            if old_mode != new_mode and old_sha == new_sha:
                mode_delta.add(path)
    return mode_delta


@dataclass(frozen=True)
class GitResult:
    """Typed result of one `_git()` invocation.

    Purpose: uniform envelope every wrapper in this module returns, so callers never
    branch on whether a `CalledProcessError` vs a raw `CompletedProcess` came back.

    Fields:
        returncode — the git process's exit code, or -1 when the process never ran
            (OSError — git not on PATH) or timed out (TimeoutExpired).
        stdout — decoded stdout, or "" when the process never ran. When the
            originating `_git()` call passed `capture=False`, this is ALWAYS ""
            — output streamed straight to the caller's own stdout and was never
            captured, so there is no real text to place here. Never a
            reconstruction or a stand-in for "succeeded with no output" — a
            `capture=False` caller that needs the process's output text must
            call again with `capture=True` (or not pass `capture=False` at all).
        stderr — decoded stderr, or a synthesized message describing the failure
            (OSError/TimeoutExpired) when the process never ran. Same
            `capture=False` rule as `stdout` above: always "" on a successful
            run, never real stderr text (a synthesized OSError/TimeoutExpired
            message is still produced on those paths, since the process never
            ran and nothing was streamed either).
        ok — True iff returncode == 0. Convenience predicate for the common case.
        worktree_excluded — repo-relative paths, sorted, whose WORKING-TREE
            content was NOT included in this result's commit because it
            differed from what was actually committed (the STAGED/index
            content, per `_commit_scoped_private_index`'s "diverged" set --
            see that function's own docstring). Empty tuple (the default,
            and the value on every `GitResult` this module already
            constructed before this field existed) means either "nothing was
            excluded" or "this result was never a commit outcome at all" --
            callers must not read an empty tuple as an affirmative "worktree
            and staged content agreed", only as "no exclusion is being
            reported here". Populated ONLY by `_commit_scoped_private_index`
            (state/bug-backlog/2026-08-10-scoped-git-commit-reports-success-
            while-334e90d707f9.yaml) -- the private-index branch commits each
            diverged path's STAGED blob verbatim, by design (see
            `commit_scoped`'s own module-section docstring for why that is
            the safe behaviour on a shared tree), but previously did so with
            no signal that the caller's own worktree edits to those paths
            were excluded. A caller that wants the worktree version
            committed instead must re-stage it and re-call -- this field
            exists purely to make the exclusion visible, not to change which
            content lands.
    """

    returncode: int
    stdout: str
    stderr: str
    worktree_excluded: Tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _git(
    args: Sequence[str],
    *,
    cwd: Union[str, Path],
    check: bool = False,
    timeout: float = _DEFAULT_TIMEOUT_SECS,
    env: Optional[Dict[str, str]] = None,
    input_data: Optional[str] = None,
    capture: bool = True,
) -> GitResult:
    """Run one `git <args>` subprocess with the Windows-safe flag set.

    Purpose: the single choke point every native git call in the `wsc_tail` rebuild
    routes through (AC3). Carries `creationflags=CREATE_NO_WINDOW` +
    `stdin=subprocess.DEVNULL` (unless `input_data` is given — see below) +
    `text=True` unconditionally, plus `capture_output=True` when `capture` (default
    True) — see module docstring for why each flag is load-bearing, and `capture`
    below for the one flag this parameter controls.

    Params:
        args    — git subcommand + arguments, WITHOUT the leading "git" token
                   (e.g. ["status", "--porcelain"], NOT ["git", "status", ...]).
        cwd     — working directory the git process runs in (always pass the
                   worktree root or common dir explicitly — never rely on the
                   daemon process's own cwd).
        check   — when True, raises `subprocess.CalledProcessError` on non-zero
                   exit (mirrors `subprocess.run(..., check=True)`). Default False:
                   callers inspect `GitResult.ok`/`returncode` themselves, which is
                   the shape every op handler in this rebuild needs (git failure is
                   business-logic data, not a daemon-crashing exception). Combined
                   with `capture=False`: mirrors stdlib exactly — `subprocess.run(
                   check=True, capture_output=False)` still raises
                   `CalledProcessError` on non-zero exit, with `.output`/`.stderr`
                   `None` (nothing was captured to attach); this wrapper's raised
                   error carries the same `None` values, never a synthesized `""`.
        timeout — seconds before the subprocess is killed and TimeoutExpired is
                   converted to a GitResult(returncode=-1, ...).
        env     — when given, replaces the subprocess's environment wholesale
                   (mirrors `subprocess.run(..., env=...)`). `None` (default)
                   inherits the parent process's environment unchanged.
                   `commit_scoped()`'s private-index branch is the only caller
                   that passes this, to redirect `GIT_INDEX_FILE` at a
                   throwaway temp index without ever mutating `os.environ`
                   for the whole process.
        input_data — when given, fed to the subprocess's stdin (`subprocess.
                   run(..., input=...)`) instead of `subprocess.DEVNULL`.
                   `check_ignore()` is the only caller today, feeding a NUL-
                   separated path list to `git check-ignore --stdin -z`. This
                   is NOT the "inherit the parent's stdin" hazard the module
                   docstring's `stdin=DEVNULL` note warns against — `input=`
                   still creates its own pipe and writes exactly this data,
                   it never lets the subprocess block on an interactive
                   parent stream. Composes with `capture=False`: stdin is a
                   pipe `subprocess.run` writes to internally regardless of
                   whether stdout/stderr are captured, so passing both together
                   is coherent (fed input, streamed output) — never rejected.
        capture — when True (default, unchanged from this wrapper's prior
                   hardcoded behaviour), passes `capture_output=True` so
                   stdout/stderr are captured and decoded onto the returned
                   `GitResult`. When False, `capture_output` is omitted (stdout/
                   stderr are inherited from the parent — the caller's own tty)
                   for a caller that wants git's output to stream live rather
                   than be captured. See `GitResult`'s own docstring for exactly
                   what `stdout`/`stderr` hold on the returned result in that
                   case — never real output, since none was captured.

    Returns a `GitResult`. Never raises `OSError` or `subprocess.TimeoutExpired` —
    both are caught and converted to a returncode=-1 GitResult so callers never need
    their own try/except around every `_git()` call site.

    Raises `subprocess.CalledProcessError` only when `check=True` AND the process
    ran to completion with a non-zero exit code (mirrors stdlib `check=True`
    semantics exactly; a process that never ran — OSError/timeout — still returns
    a GitResult rather than raising, since there is no CompletedProcess to attach
    to a CalledProcessError).
    """
    full_args: List[str] = ["git", *args]
    stdin_kwargs: Dict[str, Any] = (
        {"input": input_data} if input_data is not None else {"stdin": subprocess.DEVNULL}
    )
    run_kwargs: Dict[str, Any] = {}
    if capture:
        run_kwargs["capture_output"] = True
    def _invoke() -> "subprocess.CompletedProcess[str]":
        return subprocess.run(
            full_args,
            cwd=str(cwd),
            text=True,
            timeout=timeout,
            **no_console_creationflags(),
            env=env,
            **run_kwargs,
            **stdin_kwargs,
        )

    try:
        # Retry boundary: wraps ONLY this single `subprocess.run` invocation
        # (a zero-arg closure over it), never `commit_scoped()` or any
        # multi-step caller -- per `run_with_lock_retry`'s own negative-spec.
        # Every seam that routes through `_git()` (scoped_git_commit,
        # commit_pipeline, consumed_handoff_stamp, post_commit_tail,
        # wsc_tail) inherits lock-contention retry here, at this one choke
        # point, rather than composing it individually.
        #
        # `capture=False` bypasses the retry wrapper deliberately: with no
        # `capture_output`, `.stderr` on the CompletedProcess is `None` (per
        # this function's own `check=True`+`capture=False` docstring
        # contract, verified by `test_git_capture_false_check_true_raises_
        # with_none_output_not_synthesized_string`), and `is_lock_
        # contention()`'s substring test crashes on a `None` `.stderr`. No
        # production caller passes `capture=False` today (only `capture=
        # True`, the default, routes through the five inheriting seams), so
        # this narrows the retry boundary rather than widening
        # `run_with_lock_retry`/`is_lock_contention` (out of this chunk's
        # scope -- C3 owns `git_lock_retry.py`) to tolerate `None`.
        result = _invoke() if not capture else run_with_lock_retry(_invoke)
    except subprocess.TimeoutExpired as exc:
        return GitResult(
            returncode=-1,
            stdout="",
            stderr=f"git {' '.join(args)}: timed out after {timeout}s ({exc})"[:500],
        )
    except OSError as exc:
        return GitResult(
            returncode=-1,
            stdout="",
            stderr=f"git {' '.join(args)}: {type(exc).__name__} — {exc}"[:500],
        )

    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, full_args, output=result.stdout, stderr=result.stderr
        )

    return GitResult(
        returncode=result.returncode,
        stdout=result.stdout if capture else "",
        stderr=result.stderr if capture else "",
    )


#: git's canonical empty tree — the sha `git write-tree` emits for an index
#: holding zero entries. Load-bearing, not trivia: a MISSING `GIT_INDEX_FILE`
#: produces it with rc=0 and empty stderr. Mirrors
#: `coordinator_core.ops.fleet._common.EMPTY_TREE_SHA`; the two seams are
#: independent commit paths and neither imports the other.
EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def _has_windows_drive(normalized: str) -> bool:
    """True iff `normalized` (already `/`-normalized) carries a Windows
    drive letter (`"C:/foo"`, `"C:foo"`) -- platform-independent by
    construction. `ntpath.splitdrive` is used explicitly rather than
    `os.path.splitdrive`/`Path(...).is_absolute()`: `os.path` resolves to
    `posixpath` on a non-Windows host, whose `splitdrive` is a no-op, and a
    `PosixPath("C:/foo").is_absolute()` is False -- either would let a
    drive-letter path slip the parse-time containment guard on Linux while
    catching it on Windows, exactly the platform-dependent gap this check
    exists to close. `ntpath` itself is a pure-Python module present on
    every platform, so this reads the same drive-letter shape everywhere."""
    return ntpath.splitdrive(normalized)[0] != ""


def _empty_private_index_refusal(
    tree_sha: str,
    *,
    root: Union[str, Path],
    caller: str,
) -> Optional[GitResult]:
    """Refuse a pathspec-less private-index commit that would commit NOTHING.

    Returns None when `tree_sha` is safe to hand to `commit-tree`, or a failed
    `GitResult` the caller returns verbatim through its existing
    git-failure path.

    WHY THIS EXISTS — the incident of 2026-08-18 (`fbfbd061d`), which committed
    a tree of `4b825dc…` and thereby deleted all 26,264 files in the repo on a
    shared branch that was already pushed. `git write-tree` against a MISSING
    `GIT_INDEX_FILE` returns `EMPTY_TREE_SHA` with **exit code 0 and empty
    stderr** (verified on git 2.55.0.windows.4); a zero-byte index fails loud
    (rc=128) but an *absent* one fails silent, so every `.ok` check upstream is
    blind to it by construction — the index can go missing AFTER a successful
    `read-tree` seed.

    The first guard landed on the fleet seams
    (`fleet/_common.py :: _empty_private_index_breach`). This module's
    `commit-tree` seams are the CEREMONY path — reached by every
    `pickup-assemble apply` on the box, which is where claude-klabauter-7a
    observed the collapse reproduce. Both are pathspec-less by design: the
    private index IS the commit scope, so a lost index commits the empty tree
    rather than committing nothing.

    Deliberately trigger-independent — it does not care WHY the index went
    missing (still open as of 2026-08-18), only that a commit is about to
    erase the repo.

    Negative spec: this does NOT relax the pathspec-less design. A
    `-- <paths>` pathspec would make git read the WORKTREE for those paths
    instead of the index, which is the shared-tree absorption hazard both
    seams exist to avoid.
    """
    if tree_sha != EMPTY_TREE_SHA:
        return None
    return GitResult(
        returncode=1,
        stdout="",
        stderr=(
            "empty-private-index: git write-tree returned git's canonical "
            "EMPTY TREE (%s), meaning the private index holds zero entries — "
            "committing it with no pathspec would delete every tracked file "
            "in %s. Refused by %s; nothing was committed. The index file "
            "named by GIT_INDEX_FILE is missing or holds zero entries."
            % (EMPTY_TREE_SHA, root, caller)
        ),
    )


# ---------------------------------------------------------------------------
# Thin typed wrappers — one per git subcommand the wsc_tail rebuild needs.
# Each routes through `_git()`; none constructs its own subprocess.run call.
# ---------------------------------------------------------------------------


def status_porcelain(
    cwd: Union[str, Path], paths: Optional[Sequence[str]] = None
) -> GitResult:
    """`git status --porcelain` — dirty-tree gate classification (C3).

    `--no-optional-locks` (pre-subcommand, per `git`'s placement rule)
    suppresses the opportunistic stat-cache write-back a bare `git status`
    takes `.git/index.lock` for — contention noise on this shared worktree.
    Read-only call; never applied to a writing invocation.

    `paths` — an optional pathspec. `None` (the default) keeps the whole-tree
    scan every existing caller gets. When given, the answer is scoped to those
    paths: this is the only query on the commit hot path whose cost scales
    with the TREE rather than with what is being committed.

    Measured on claude-klabauter at ~40 dirty paths: 1071ms unscoped floor
    against 884ms scoped. The walk is NOT the main cost and this parameter is
    not where the ceremony's latency lives — process creation is (DR-344),
    and both numbers are one spawn. It is taken because it is free and
    scales with the tree, not because ~190ms closes any gap; the ceremony's
    budget is made or missed on how many times `git` is spawned at all.

    Output SHAPE is byte-identical either way — same porcelain v1, same
    C-quoting, same ` -> ` rename separator — deliberately, so a caller that
    already parses `status_porcelain()` output can pass a pathspec without
    touching its parse. That is why this takes a parameter instead of routing
    to `status_porcelain_scoped()` above, whose `-z`/`-uall` shape answers a
    different question (a committable path list, not a dirty/clean verdict)
    and would silently change how a `R`-record or a non-ASCII path reads.

    Chunked against the Windows 32767-char argv cap, because failing that cap
    here is worse than slow: `git` reports `rc=127`, `stdout` comes back
    empty, and an empty dirty set reads as "tree is clean" — a gate that
    FAILS OPEN on exactly the large-pathspec batch (percolate-publish scale,
    ~2000-2700 paths) it most needs to hold. Any chunk that fails short-
    circuits and is returned as-is, so the caller sees an unsuccessful
    `GitResult` rather than a partial dirty set that looks complete."""
    base = ["--no-optional-locks", "status", "--porcelain"]
    if paths is None:
        return _git(base, cwd=cwd)

    combined: List[str] = []
    for chunk in _chunk_paths(list(paths)):
        result = _git([*base, "--", *chunk], cwd=cwd)
        if not result.ok:
            return result
        combined.append(result.stdout)
    return GitResult(returncode=0, stdout="".join(combined), stderr="")


def status_porcelain_scoped(cwd: Union[str, Path], paths: Sequence[str]) -> GitResult:
    """`git status --porcelain=v1 -z -uall -- <paths>` — the dirty set beneath
    an explicit pathspec, NUL-separated, with untracked directories expanded
    to their individual files.

    Companion to `status_porcelain` above, for the caller that needs a
    COMMITTABLE path list rather than a dirty/clean verdict. Three
    differences, each load-bearing:

      `-z`      — a path containing a space, a quote, or a non-ASCII byte is
                  emitted raw rather than C-quoted, so the caller never has to
                  unquote (and never silently mis-unquotes) a real filename.
      `-uall`   — without it git collapses a wholly-new directory into ONE
                  `dir/` entry. A pathspec carrying a directory element is
                  refused downstream (`commit_pipeline.explicit_stage`), so
                  the collapsed form is not merely lossy here, it is unusable.
      `<paths>` — scopes the answer to the subtrees the caller names, leaving
                  unrelated dirt elsewhere in the worktree out of the result
                  (and therefore out of any commit built from it).

    `--no-optional-locks` for the same reason as `status_porcelain`: read-only
    call, no reason to contend for `.git/index.lock` on a shared worktree.

    Negative-spec: does NOT parse its own output — the porcelain-v1 record
    format (2-char code, space, path; a second NUL-separated field for `R`/`C`)
    is the caller's to read, same as every other wrapper in this module returns
    raw `stdout`."""
    return _git(
        [
            "--no-optional-locks",
            "status",
            "--porcelain=v1",
            "-z",
            "-uall",
            "--",
            *paths,
        ],
        cwd=cwd,
    )


def diff_cached_name_status(
    cwd: Union[str, Path], *, find_renames: bool = True
) -> GitResult:
    """`git diff --cached --name-status [--find-renames]` — staged-set classification (C4)."""
    args = ["diff", "--cached", "--name-status"]
    if find_renames:
        args.append("--find-renames")
    return _git(args, cwd=cwd)


def diff_cached_name_only(
    cwd: Union[str, Path],
    paths: Optional[Sequence[str]] = None,
    *,
    nul_separated: bool = False,
) -> GitResult:
    """`git diff --cached --name-only [-z] [-- <paths>]` — staged-diff plan/anchor resolution parity.

    `paths` optionally scopes the pathspec. `explicit_stage()`'s post-`git
    add`-failure residue reconciliation always passes its own attempted
    batch here — never call this unscoped when the goal is to check a
    bounded set, or the result widens to the WHOLE index and could read a
    concurrent peer session's own staged work as this call's residue (the
    same peer-safety hazard `reset_paths()`'s own docstring warns against
    for its rollback pathspec, one layer over).

    `nul_separated` (default False, preserving `commit_gates.py`'s existing
    `deletion_block_gate()` caller unchanged) -- when True, passes `-z` so
    git emits NUL-separated, UNQUOTED entries instead of newline-separated
    output. Without `-z`, `git diff --cached --name-only` C-quotes any path
    containing non-ASCII bytes, quotes, backslashes, or control characters
    (e.g. `caf\\303\\251.md` renders as `"caf\\303\\251.md"`), which breaks a
    plain-string membership comparison against an unquoted candidate path --
    exactly the residue-reconciliation caller's use case in
    `explicit_stage()` (code-reviewer Finding 4, fa1aeeeb9187 review: a
    missed non-ASCII path there is unreconciled residue, invisible to the
    caller's rollback). Callers passing `nul_separated=True` must split
    `stdout` on `"\\0"` and drop the trailing empty entry, never
    `.splitlines()` (which would silently misparse NUL-delimited output).
    """
    args = ["diff", "--cached", "--name-only"]
    if nul_separated:
        args.append("-z")
    if paths:
        args.extend(["--", *paths])
    return _git(args, cwd=cwd)


def diff_quiet(cwd: Union[str, Path], paths: Optional[Sequence[str]] = None) -> GitResult:
    """`git diff --quiet [-- <paths>]` — EOL-phantom-diff detection (C3 dirty-tree gate).

    Returncode 0 = no diff (phantom / already-matching); 1 = real diff present.
    Callers must NOT treat returncode==1 as an error — it is the documented
    "diff present" contract result (see the exit-code-semantics discipline in this
    module's callers' own docstrings).
    """
    # DELIBERATELY NOT `--no-optional-locks`, unlike `status_porcelain` and
    # every other read wrapper in this module. Suppressing the optional lock
    # makes git compute the stat-cache refresh in memory and discard it, so a
    # lock-suppressed read can never CLEAR phantom-dirty state -- only a real
    # lock-taking write does. This wrapper's sole production caller is
    # `commit_gates`' EOL-phantom filter, which exists precisely to absorb
    # phantom-dirty entries; the flag would leave every phantom permanently
    # dirty and re-filtered on each ceremony (the flapping-count symptom in
    # DoE-claude's bash-on-windows-gotchas.md § 11). The contention win here is
    # small anyway -- this is a narrow per-path diff, not the whole-tree status
    # scan the adoption pass targets.
    args = ["diff", "--quiet"]
    if paths:
        args.extend(["--", *paths])
    return _git(args, cwd=cwd)


def cat_file_batch(repo_root: Union[str, Path], ref: str, rel_paths: Sequence[str]) -> Dict[str, Optional[str]]:
    """Resolve `rel_paths` at `ref` to blob text via ONE `git cat-file --batch`
    feed instead of one `git show <ref>:<path>` spawn per file. Deliberately
    `--batch`, NOT `--batch-check`: this helper reads blob CONTENT, and
    `--batch-check` returns metadata only -- a caller that only needs
    existence/size/type must not route through this helper (shipping
    `--batch-check`'s shape here would silently return None for every
    resolvable entry, since this parser expects a content body after each
    header line).

    Promoted (2026-08) from `coordinator_core.reconcile.ac27_differential_oracle
    ._git_cat_file_batch` (authored there, C5, because this module belonged to
    no chunk in that wave) into this module -- the house home for the repo's
    other native git read-wrappers -- so a second N+1-git-spawn site does not
    grow its own independent copy. Behaviour is preserved byte-for-byte from
    the original; this is a promotion, not a rewrite.

    Bypasses `_git()` deliberately (like `_hash_object_stdin_bytes` above) --
    `_git()`'s `text=True` leg would mis-decode a batch feed whose per-record
    boundaries are computed from byte-length `size` fields, not text lines,
    so this helper drives `subprocess.run` directly in bytes mode.

    `cat-file --batch` resolves each stdin line INDEPENDENTLY (unlike a
    multi-range `rev-list`/`log` feed, which computes one combined
    reachability set across all inputs) -- so batching the object list here
    carries none of the set-algebra hazard that forbids batching a range list.

    Reconciliation: a batched feed can silently drop or misalign entries if
    stdout is short or malformed, so every requested `rel_path` is bound to
    an explicit slot in the returned dict by walking the SAME `rel_paths`
    order the input was written in -- resolved -> blob text, missing/
    truncated/malformed -> `None`. Absence from git's output is never read
    as "resolved"; the caller decides what `None` means.

    Returns `{}` immediately for an empty `rel_paths`, spawning no
    subprocess.
    """
    if not rel_paths:
        return {}
    objects = [f"{ref}:{rel_path}" for rel_path in rel_paths]
    resolved = cat_file_batch_objects(repo_root, objects)
    return {rel_path: resolved[f"{ref}:{rel_path}"] for rel_path in rel_paths}


def cat_file_batch_objects(
    repo_root: Union[str, Path], objects: Sequence[str]
) -> Dict[str, Optional[str]]:
    """Resolve arbitrary `<rev>:<path>` object specs to blob text in ONE
    `git cat-file --batch` feed, keyed by the spec string as passed.

    The generalization of `cat_file_batch` (which is now a thin
    single-ref wrapper over this): that function batches many PATHS at ONE
    ref, which forces a caller needing many (rev, path) PAIRS into one spawn
    per rev. A history sweep across every commit that touched a directory is
    exactly that caller -- per-rev spawning would put a cold Windows `git`
    process in a loop over the whole commit list, the shape CLAUDE.md's
    Runtime conventions calls break-class. `git cat-file --batch` resolves
    each stdin line INDEPENDENTLY, so the pairs carry none of the set-algebra
    hazard that forbids batching a `rev-list` range feed.

    Same reconciliation contract as `cat_file_batch`: every requested spec is
    bound to an explicit slot by walking the SAME `objects` order the input
    was written in -- resolved -> blob text, missing/truncated/malformed ->
    `None`. Absence from git's output is never read as "resolved".

    Returns `{}` for an empty `objects`, spawning no subprocess.
    """
    if not objects:
        return {}
    from coordinator_core.win_portability import leaf_spawn_creationflags

    stdin_bytes = ("\n".join(objects) + "\n").encode("utf-8")
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "cat-file", "--batch"],
        input=stdin_bytes,
        capture_output=True,
        **leaf_spawn_creationflags(),
    )
    stdout = proc.stdout
    results: Dict[str, Optional[str]] = {}
    pos = 0
    for spec in objects:
        nl = stdout.find(b"\n", pos)
        if nl == -1:
            # stdout ran out relative to the requested set -- every remaining
            # spec is unresolved; never guess at a partial record.
            results[spec] = None
            continue
        header = stdout[pos:nl]
        pos = nl + 1
        if header.endswith(b" missing"):
            results[spec] = None
            continue
        parts = header.split(b" ")
        if len(parts) != 3:
            results[spec] = None
            continue
        _sha, _type, size_field = parts
        try:
            size = int(size_field)
        except ValueError:
            results[spec] = None
            continue
        content = stdout[pos:pos + size]
        pos += size
        if stdout[pos:pos + 1] == b"\n":
            pos += 1
        try:
            results[spec] = content.decode("utf-8")
        except UnicodeDecodeError:
            results[spec] = content.decode("utf-8", errors="replace")
    return results


def add_paths(cwd: Union[str, Path], paths: Sequence[str]) -> GitResult:
    """`git add --pathspec-from-file=<f>` — explicit-pathspec stage (never a
    bare `git add -A`/`.`).

    Delivers `paths` via a temp pathspec file (`_write_pathspec_file`,
    same helper `add_paths_pathspec_file()` below uses) rather than argv:
    the whole `paths` list on argv is capped at 32767 chars on Windows
    (`CreateProcess`), and at percolate scale (thousands of paths) that cap
    was forcing callers to chunk this call across many `git add` spawns.
    `--pathspec-from-file` removes argv from the picture entirely and is
    empirically supported on this machine's git 2.55.0.windows.4/.3 (see
    `add_paths_pathspec_file()`'s own docstring) — one call regardless of
    batch size. Empty `paths` is a no-op `git add --` with no pathspec file
    ever written (never spend a temp-file write on nothing to stage).

    Also the correct staging call for a DELETION: per `git-add(1)`, an
    explicit pathspec naming a tracked file that has been removed from the
    working tree stages the removal (records the deletion in the index) —
    this has been git's default behaviour for an explicit path since older
    Git's `--no-all`-required era ended; only a fileglob/directory pathspec
    ever needed `-u`/`-A` to pick up removals. Callers do not need a
    separate "stage this deletion" primitive. Verified still true through
    `--pathspec-from-file`: a deleted tracked path named in the pathspec
    file stages as `D`, same as the old argv form.

    `git add` is atomic per invocation (all-or-nothing on the whole
    pathspec, unlike a chunked multi-call loop, which could stage some
    chunks before a later one fails) — a caller reconciling post-failure
    residue against a partial-batch window (the old chunked shape) now
    reconciles against an add that either fully succeeded or staged
    nothing.
    """
    if not paths:
        return _git(["add", "--"], cwd=cwd)
    return add_paths_pathspec_file(cwd, paths)


def ls_files_deleted(cwd: Union[str, Path], paths: Sequence[str]) -> GitResult:
    """`git ls-files --deleted -- <paths>` — tracked paths, scoped to *paths*,
    whose content is missing from the WORKING TREE relative to the index
    (i.e. `rm <path>` without a following `git rm`/`git add`) — the
    UNSTAGED-deletion case.

    Deliberately distinct from a STAGED deletion (index already differs from
    HEAD — see `diff_cached_name_status`'s `D` lines): once a deletion is
    staged, the index no longer holds the file's content, so `--deleted`
    (which compares worktree against the INDEX) no longer reports it. A
    caller wanting "is this path a deletion in ANY form right now" must
    check `diff_cached_name_status` for the staged case and this helper for
    the unstaged case — never one alone.

    Scoped to `paths` (never called bare) so this never widens into a
    whole-tree deleted-file scan on a shared branch; mirrors every other
    pathspec-scoped read in this module. Empty `paths` returns immediately
    with an empty, `ok=True` result — `git ls-files --deleted --` with no
    trailing pathspec would scan the WHOLE tree, which is never the intent
    of a caller that passed nothing to scope to.
    """
    if not paths:
        return GitResult(returncode=0, stdout="", stderr="")
    return _git(["ls-files", "--deleted", "--", *paths], cwd=cwd)


def status_porcelain_v2(
    cwd: Union[str, Path], paths: Optional[Sequence[str]] = None
) -> GitResult:
    """`git status --no-optional-locks --porcelain=v2 -z` -- the full v2
    classification, NUL-separated, UNSCOPED by default (`paths=None`).

    Purpose: `commit_pipeline._worktree_deleted_paths`'s single fail-loud
    read, replacing the old `git ls-files --deleted` probe that degraded to
    a permissive "found nothing" guess on failure (see that function's own
    docstring for the incident this closes), and replacing this seam's own
    earlier pathspec-scoped/chunked shape (2026-08-26 second fix,
    `docs/research/spike-verdicts/2026-08-26-one-porcelain-v2-read-
    replaces-the-probe-suite.md`): a pathspec here only filters git's
    OUTPUT -- `git status` refreshes the index and walks the whole worktree
    regardless of what pathspec it is given -- so chunking a pathspec to
    dodge the Windows argv cap bought nothing but spawns (measured on this
    repo, 36547 tracked files: 32 spawns for a 2600-path chunked batch vs 1
    spawn for the same call unscoped, at ANY batch size). Taking one
    unscoped read and intersecting the caller's requested paths against it
    IN-PROCESS costs the SAME one walk git was always doing, for one
    spawn total instead of one per chunk.

    `paths` optionally scopes the OUTPUT (mirrors `status_porcelain`'s own
    shape above) -- `commit_pipeline` never passes it, since scoping the
    output here would still require chunking against the same argv cap this
    rewrite exists to avoid; kept for parity with `status_porcelain` and any
    future caller that genuinely wants a bounded read.

    Deliberately NOT `--ignored -uall`: measured on this repo, `--ignored
    -uall` produces 873KB of stdout against 10.6KB without it, and
    `_worktree_deleted_paths` (this seam's sole caller) never reads a `?`
    or `!` record -- it only classifies `1`/`2` records' `D` worktree
    status. `-uall` without `--ignored` would still expand untracked
    directories into per-file `?` records this caller discards anyway, so
    neither flag earns its cost here. `status_porcelain_scoped()` above
    keeps `-uall` for its own caller, which DOES need per-file untracked
    expansion (`commit_pipeline.explicit_stage` refuses a directory
    pathspec) -- the two seams intentionally diverge on this flag, not by
    oversight. <!-- Review: coordinator:code-reviewer -- cross-reference
    added so the omission here reads as deliberate, not a miss. -->

    `--no-optional-locks`: read-only call, no reason to contend for
    `.git/index.lock` on a shared worktree, same as every other read in
    this module.

    Negative-spec: does NOT parse its own output -- the v2 record format
    (leading-token dispatch: `1` ordinary, `2` rename with its `<origPath>`
    as the NEXT NUL field, `?` untracked, `u` unmerged, `#` header) is the
    caller's to read; see `docs/research/spike-verdicts/2026-08-26-one-
    porcelain-v2-read-replaces-the-probe-suite.md` for the confirmed shapes
    on git 2.55.0.windows.4.
    """
    args = ["--no-optional-locks", "status", "--porcelain=v2", "-z"]
    if paths:
        args.extend(["--", *paths])
    return _git(args, cwd=cwd)


def reset_paths(cwd: Union[str, Path], paths: Sequence[str]) -> GitResult:
    """`git reset -q HEAD -- <paths>` — explicit-pathspec unstage-only rollback.

    Purpose: the scoped-rollback counterpart to `add_paths()` — undoes
    exactly the index-side effect of an `add_paths()` call this process
    itself made, for a `commit_pipeline` caller that staged real paths and
    then hit a post-stage failure (a gate, or the commit subprocess itself)
    before a commit ever landed. Never touches the worktree (`git reset`,
    unlike `git checkout`, only moves the index pointer back to `HEAD` for
    the given pathspec) and never a bare `git reset` — see `paths` below.

    `paths` MUST be the caller's own previously-staged set, never derived
    from `git status`/`git diff --cached` at call time — a bare
    `git reset -q HEAD --` (empty pathspec after `--`) unstages the ENTIRE
    index, not nothing, silently discarding a concurrent peer session's own
    staged work on the SAME shared working tree (the identical
    directory-pathspec/`git add -A` hazard `commit_scoped()`'s own directory
    refusal and `add_paths()`'s explicit-pathspec contract both exist to
    close, one layer up). `paths` empty here is therefore a documented
    no-op — not "reset everything" — matching every other empty-input guard
    in this module (see `commit_scoped()`'s own empty-path-set handling).

    Idempotent/quiet by construction: unstaging a path whose staged content
    already matches `HEAD` (the ordinary already-committed no-op shape a
    caller may roll back after a `git commit` that exits 1 on an empty
    commit set) is a harmless no-op — `git reset -q` never raises or prints
    for that case, so callers need no special-casing to keep this silent on
    the routine no-op path.

    Directory entries in `paths` are silently DROPPED before the `git reset`
    call, never passed through as part of the pathspec — the same hazard
    `commit_scoped()`'s own directory-pathspec refusal exists for, one layer
    up (see this module's own directory-pathspec check in `commit_scoped`):
    a directory pathspec matches whatever is CURRENTLY inside it at reset
    time, including a peer's own file added under that directory after this
    call's own `add_paths()` ran, silently unstaging content this call never
    touched. Reintroducing that hazard in the rollback path would be worse
    than the residue it exists to clean up. A caller that legitimately
    staged a directory pathspec (`commit_scoped()` itself refuses to commit
    such a path — see its own docstring) gets a best-effort PARTIAL rollback
    here: every non-directory entry in `paths` is still unstaged; the
    directory entry itself is left as-is (a loud, diagnostic-visible residue
    a caller already sees via `commit_failed`/`diagnostics`, not a silent
    unattributed one).
    """
    if not paths:
        return GitResult(returncode=0, stdout="", stderr="")
    root = Path(cwd)
    file_paths = [p for p in paths if not (root / p).is_dir()]
    if not file_paths:
        return GitResult(returncode=0, stdout="", stderr="")
    return _git(["reset", "-q", "HEAD", "--", *file_paths], cwd=cwd)


#: One parsed `git check-ignore -v --stdin -z` match: `source:line:pattern`
#: tuple plus the matched repo-relative pathname.
_CheckIgnoreMatch = Tuple[str, str, str, str]


def parse_check_ignore_stdin_z(stdout: str) -> List[_CheckIgnoreMatch]:
    """Parse `git check-ignore -v --stdin -z` output into 4-field groups.

    Format: NUL-separated `<source>\\0<line>\\0<pattern>\\0<pathname>\\0`,
    repeated once per MATCHED input path (an input path with no matching
    `.gitignore` rule simply produces no group at all -- callers must not
    assume one-group-per-input-path). `-z` (never the newline-separated
    default) is required for the same C-quoting reason `diff_cached_name_only
    (..., nul_separated=True)`'s own docstring documents (code-reviewer
    Finding 4, fa1aeeeb9187 review): a non-ASCII/quote/backslash-containing
    path would otherwise come back C-quoted and silently fail a plain-string
    membership test against the unquoted candidate.
    """
    fields = stdout.split("\0")
    if fields and fields[-1] == "":
        fields = fields[:-1]
    matches: List[_CheckIgnoreMatch] = []
    for i in range(0, len(fields) - 3, 4):
        matches.append((fields[i], fields[i + 1], fields[i + 2], fields[i + 3]))
    return matches


def check_ignore(cwd: Union[str, Path], paths: Sequence[str]) -> GitResult:
    """`git check-ignore -v --stdin -z` — batched `.gitignore` match test.

    Purpose: `explicit_stage()`'s pre-`git add` ignored-path classification
    (session incident, live 2026-08-03 `safe-commit-offer` run: a mixed
    batch containing a gitignored path, e.g. `state/orientation_cache.md`,
    failed the WHOLE `git add` batch rather than skipping just that path --
    see `explicit_stage()`'s own docstring for the fix this feeds). Plain
    `git check-ignore` (no `--no-index`) is index-aware BY DEFAULT: a
    tracked path never reports as ignored even if a `.gitignore` pattern
    added later would match it, and an untracked path is matched purely
    against `.gitignore` PATTERNS -- exactly the single "would a fresh `git
    add` refuse this path" question this classification needs, with no
    separate tracked-gate required of the caller. `--stdin -z` (NUL-
    separated in AND out) avoids both the argv-length limit a large
    touch-list batch could hit and the C-quoting hazard
    `parse_check_ignore_stdin_z()`'s own docstring names.

    Negative-spec: `--no-index` is deliberately NOT used here -- it makes
    the match purely pattern-based and INDEX-BLIND, so a tracked path
    matching a later-added `.gitignore` pattern would misreport as ignored
    (measured live in this repo, 2026-08-03: `git check-ignore -v` on a
    tracked path matching `.gitignore:11` -> rc=1, correctly not ignored;
    `git check-ignore -v --no-index` on the same path -> rc=0, incorrectly
    ignored). Re-adding `--no-index` re-introduces that misclassification
    and reopens the need for a separate `ls_files_tracked()` gate this
    function's callers no longer carry -- do not "fix" it back in.

    Returns a `GitResult` whose `returncode` is 0 (>=1 path matched), 1 (no
    path matched -- NOT a failure; `stdout` is simply empty), or >=2 on a
    genuine `git check-ignore` error (bad repo state, etc.) -- callers must
    branch on `returncode in (0, 1)` to treat this call as having answered
    the question at all, never on `.ok` alone (`.ok` is False for the
    entirely-normal "nothing matched" case). Empty `paths` short-circuits to
    the same "nothing matched" shape without spawning a subprocess.
    """
    if not paths:
        return GitResult(returncode=1, stdout="", stderr="")
    stdin_data = "\0".join(paths) + "\0"
    return _git(
        ["check-ignore", "-v", "--stdin", "-z"],
        cwd=cwd,
        input_data=stdin_data,
    )


def directory_pathspecs(cwd: Union[str, Path], paths: Sequence[str]) -> List[str]:
    """Return every entry of `paths` currently a directory on disk, order preserved.

    Shared predicate behind `commit_scoped()`'s own directory-pathspec
    refusal and (until its C4 deletion) the now-killed `commit_pipeline.
    run_commit_pipeline()`'s pre-stage guard -- `coordinator_core.git.commit.
    commit_paths` (C3's repoint target for every `run_commit_pipeline`
    caller) does NOT use this module or this predicate at all; it carries
    its own inline directory-pathspec check (session fb5fa766, 2026-07-31
    incident: a directory pathspec reached
    `git add` before ever hitting this check, one layer down, leaving
    staged-and-abandoned residue on refusal) -- extracted so both refuse the
    IDENTICAL input shape rather than drifting into two subtly different
    notions of "is a directory pathspec". A directory pathspec matches
    whatever is CURRENTLY inside it at git-invocation time, including a
    peer's file added after the caller computed `paths` -- the same
    partial-blanket-add hazard `git add -A` is banned for.
    """
    root = Path(cwd)
    return [p for p in paths if (root / p).is_dir()]


def directory_pathspec_diagnostic(path: str) -> str:
    """The shared diagnostic wording for one rejected directory pathspec `path`.

    Callers prefix this with their own identity (e.g. `"commit_scoped: "` or
    `"run_commit_pipeline: pre-stage guard: "`) so the underlying cause reads
    identically everywhere a directory pathspec is refused -- see
    `directory_pathspecs()` for why this is a shared, not forked, predicate.
    """
    return (
        f"directory pathspec {path!r} rejected -- a directory matches "
        "whatever is inside it AT COMMIT TIME, including a peer's file "
        "added after this path set was computed. Pass explicit file paths "
        "instead."
    )


#: Env var `coordinator_core.hooks.auto_push` reads to stand down for one
#: commit. Imported by name rather than restated as a literal so the two
#: modules cannot drift apart silently.
_AUTO_PUSH_SUPPRESS_ENV = "COORDINATOR_AUTO_PUSH_SUPPRESS_FOR_SYNC_PUSH"


#: Widens `_sole_publisher_env()`'s suppression decision for the DURATION of
#: a caller-declared span, independent of the per-call `suppress_post_commit_
#: auto_push` argument each higher-level caller (`commit_pipeline.commit`,
#: `post_commit_tail`, `consumed_handoff_stamp`) computes for itself as
#: `(push_mode == PUSH_MODE_SYNC)`. A `contextvars.ContextVar`, deliberately
#: NOT `os.environ`: `_sole_publisher_env`'s own docstring names the reason
#: `os.environ` is never mutated here (a cold-spawn engine hides a process-
#: global toggle; a warm one turns it into a cross-request leak) -- a
#: contextvar is coroutine/task-local under asyncio (and copied into an
#: `asyncio.to_thread` worker via `contextvars.copy_context()`, so it
#: survives the `commit_pipeline`/`post_commit_tail` call chain's own
#: to_thread hop) and carries none of that leak risk. Sole writer today:
#: `wsc_tail._deferred_publisher_backstop()`, wrapping the deferred-path's
#: steps 5a-5d so the hook's own push stands down for the WHOLE span (the
#: main ceremony commit and its 5c/5d follow-up commits alike), leaving
#: step 5e's own detached push as the sole publisher (opro-01 C-01 follow-up,
#: docs/plans/2026-08-19-windows-commit-hook-starts-python-once.md C5).
_deferred_publisher_active: "contextvars.ContextVar[bool]" = contextvars.ContextVar(
    "_deferred_publisher_active", default=False
)


@contextmanager
def deferred_publisher_span() -> Iterator[None]:
    """Mark every `_sole_publisher_env()` call reached during this span as
    "the caller will publish" (see `_sole_publisher_env`'s own docstring:
    "the invariant is not enforceable here ... held at each call site").
    `wsc_tail`'s deferred-path backstop is this contextvar's sole caller --
    see its own docstring for why the widening is scoped to that one span
    rather than applied unconditionally to every `push_mode="deferred"`
    caller in the codebase (there is, today, exactly one: `wsc_tail`).

    Resets via the token on exit (including on an exception) rather than
    unconditionally clearing to `False` -- a NESTED span (not exercised
    today, but the correct contract for one) restores the OUTER span's
    value instead of clobbering it to `False` on the inner span's exit.
    """
    token = _deferred_publisher_active.set(True)
    try:
        yield
    finally:
        _deferred_publisher_active.reset(token)


def _sole_publisher_env(suppress_post_commit_auto_push: bool) -> Optional[Dict[str, str]]:
    """Env for a `git commit` whose caller will publish the commit itself.

    opro-01 C-01 (state/audits/2026-08-18-opro-01-where-the-push-outcome-is-
    known.md). `git commit` fires the installed `post-commit` hook, which
    detaches and pushes; a caller that then runs its own synchronous `git
    push` has TWO publishers racing for one branch tip. When the detached
    child wins, the caller's own push fails on a commit that is already on
    the remote -- the 2026-07-30 false negative, and the reason
    `scoped_git_commit` grew a remote-confirmation probe to walk its own
    verdict back.

    Standing the hook's push down for this one commit makes the caller's own
    push outcome authoritative BY CONSTRUCTION rather than by corroborating
    it against the remote. The commit is still published -- synchronously, by
    the caller, in the same invocation.

    Accepted delta, named because it is a real one (review, s2): on a
    suppressed commit whose synchronous push then FAILS, no pending-push record
    is written by anyone -- the hook that would have written one stood down, and
    the caller's re-hosted drain only runs after a push that succeeded. This is
    not silent (`integrity_breach` fires on exactly that path) and the commit is
    not orphaned (it rides the branch tip on the next successful push), so the
    "delay, never lose" contract degrades to "delay" rather than breaking. The
    alternative -- writing a record from the failure path -- would hand the
    next drain a push this caller already owns, which is the two-publisher
    problem this seam exists to remove.

    The invariant is "whoever sets this WILL publish the commit" -- not,
    as an earlier revision of this docstring stated, "synchronously, in
    this same invocation": `wsc_tail`'s deferred-path backstop (C5,
    docs/plans/2026-08-19-windows-commit-hook-starts-python-once.md) sets
    this for a commit it will publish via a DETACHED child spawned later,
    after steps 5a-5d complete -- still exactly one publisher, just not a
    synchronous one. Neither shape is enforceable here: this function
    cannot see the caller's later control flow. Each is held at its own
    call site -- the synchronous shape by tying the per-call
    `suppress_post_commit_auto_push` argument to `push_mode ==
    PUSH_MODE_SYNC` (pinned by `test_suppression_is_wired_to_sync_mode_
    only`), the deferred shape by `wsc_tail` wrapping steps 5a-5d in
    `deferred_publisher_span()` (pinned by
    `test_deferred_publisher_span_widens_suppression`).

    Returns None when suppression is off (neither the per-call argument NOR
    an active `deferred_publisher_span()`), so the caller passes `env=None`
    and `_git` inherits the parent environment unchanged (never a rebuilt
    copy of `os.environ`, which would be a behaviour change wearing a
    no-op's clothes). `os.environ` itself is never mutated: this repo's
    engine is a cold spawn per invocation today, but the warm engine would
    make a process-global toggle here a cross-request leak -- the reason
    the span above is a `contextvars.ContextVar`, not a second `os.environ`
    write.
    """
    if not (suppress_post_commit_auto_push or _deferred_publisher_active.get()):
        return None
    env = dict(os.environ)
    env[_AUTO_PUSH_SUPPRESS_ENV] = "1"
    return env


def commit_with_message_file(
    cwd: Union[str, Path],
    msg_file: Union[str, Path],
    paths: Sequence[str],
    *,
    suppress_post_commit_auto_push: bool = False,
) -> GitResult:
    """`git commit -F <msg_file> -- <paths>` — explicit-pathspec commit (AC5).

    Never a bare `git commit` / `git commit -m` — a concurrent sibling's staged
    file or deletion outside `paths` is never absorbed (parity assertions d + e).

    Puts the WHOLE `paths` list on argv, so this is unsafe at the
    percolate-publish scale (~2000+ paths) that blows the Windows
    `CreateProcess` 32767-char cap -- `commit_scoped()`'s own agree branch
    uses `commit_with_message_file_pathspec_scoped()` (below) instead, never
    this wrapper, for exactly that reason. Kept here unchanged for every
    other, smaller-batch caller.

    `suppress_post_commit_auto_push` -- see `_sole_publisher_env`. Default
    False: a caller that does NOT push synchronously afterwards must leave the
    hook's own push in place, or the commit never reaches the remote at all.
    """
    return _git(
        ["commit", "-F", str(msg_file), "--", *paths],
        cwd=cwd,
        env=_sole_publisher_env(suppress_post_commit_auto_push),
    )


def _write_pathspec_file(root: Union[str, Path], paths: Sequence[str]) -> Path:
    """Write `paths` NUL-separated to a uniquely-named temp file and return
    its path, for `git ... --pathspec-from-file=<f> --pathspec-file-nul`.

    NEGATIVE SPEC -- the NUL form is mandatory, not a preference, and every
    reader of a file this produces MUST pass `--pathspec-file-nul`. This
    module previously wrote newline-delimited on the premise that no path
    it stages contains a literal newline. That premise was true and beside
    the point: git's default (non-NUL) pathspec-file reader ALSO C-dequotes
    any line beginning with a double quote. Measured 2026-08-26 on git
    2.55.0.windows.4 -- a line `"plain.txt"` stages `plain.txt`, and
    `"pla\151n.txt"` decodes the octal escape and stages `plain.txt` too,
    both at rc=0. rc=0 is what makes it dangerous: a silently mis-resolved
    pathspec is invisible to every downstream check, the post-failure
    residue reconciliation included, so the wrong file is staged and
    committed while the call reports success. Under `--pathspec-file-nul`
    no dequoting happens and the same input fails loud (rc=128, "did not
    match any files").

    Same PID+uuid uniqueness convention `stage_from_patch()`'s own
    `temp_index` uses, for the same reason: two concurrent sessions on this
    shared machine must never collide on one temp filename. No trailing
    separator -- git treats a trailing NUL as an empty pathspec, which
    matches nothing and fails the whole call. Caller owns cleanup
    (`finally: pathspec_file.unlink(missing_ok=True)`), mirroring the
    `-F <msgfile>` flow's own discipline elsewhere in this module.
    """
    pathspec_file = (
        Path(tempfile.gettempdir())
        / f"git-pathspec-{os.getpid()}-{uuid.uuid4().hex}.txt"
    )
    # NUL-separated, and every caller MUST pass `--pathspec-file-nul`.
    # Newline-delimited is NOT safe: git C-dequotes any pathspec-file line
    # beginning with a double quote. Measured 2026-08-26, git
    # 2.55.0.windows.4 -- a line `"plain.txt"` stages `plain.txt`, and
    # `"pla\151n.txt"` decodes the octal escape and also stages
    # `plain.txt`, both at rc=0. A silent mis-stage at rc=0 is invisible to
    # every downstream check, the residue reconciliation included.
    # `--pathspec-file-nul` disables dequoting outright: the same quoted
    # input then fails loud (rc=128, "did not match any files").
    pathspec_file.write_bytes(b"\0".join(p.encode("utf-8") for p in paths))
    return pathspec_file


def add_paths_pathspec_file(cwd: Union[str, Path], paths: Sequence[str]) -> GitResult:
    """`git add --pathspec-from-file=<f>` — the percolate-publish-scale-safe
    pathspec-file primitive. `add_paths()` (above) now delegates here for
    every non-empty batch, and `commit_scoped()`'s own agree branch calls
    it directly (2026-08-15, the last of the five argv-length sites this
    repo's commit path had -- siblings `ef84c2ee9`/`fe0f4eb84`/`25268ed33`/
    `47e8defbb` closed the other four): staging is not chunked here, unlike
    `_diverging_paths_chunked()` above, because `git add
    --pathspec-from-file=<f>` is empirically SUPPORTED (verified against
    this machine's git 2.55.0.windows.3/.4, in a scratch repo -- unlike
    `git diff`, which rejects `--pathspec-from-file` outright) -- one
    pathspec-file call replaces one argv-list call with no chunking needed,
    and no atomicity concern either (staging is not the commit itself).
    """
    root = Path(cwd)
    pathspec_file = _write_pathspec_file(root, paths)
    try:
        return _git(
            ["add", f"--pathspec-from-file={pathspec_file}", "--pathspec-file-nul"],
            cwd=root,
        )
    finally:
        pathspec_file.unlink(missing_ok=True)


#: Env var `coordinator.bin.lib.git_hook_install.ensure_prepare_commit_msg_
#: hook` wires as ITS OWN `skip_env` (C2, docs/dispatch-briefs/2026-08-25-
#: the-engine-commits-without-re-entering-itself/C2.md) -- deliberately a
#: DIFFERENT name from `_AUTO_PUSH_SUPPRESS_ENV` (C1's post-commit sentinel):
#: the two hooks skip on different facts, and folding them into one flag
#: would be the "skip all hooks" generalization `_shim_body`'s own docstring
#: forbids. Set ONLY by `_trailer_sentinel_env()`, below, and ONLY at the one
#: call site inside `commit_scoped`'s agree branch that follows `_apply_
#: trailers` returning with no error -- see that function's docstring for
#: why nowhere else may set this (AC12: the lying-sentinel defence is that
#: this is the sentinel's one and only setter, pinned by
#: `test_trailer_sentinel_has_exactly_one_setter`).
_TRAILERS_ALREADY_APPLIED_ENV = "COORDINATOR_TRAILERS_ALREADY_APPLIED"


def _trailer_sentinel_env() -> Dict[str, str]:
    """Per-call env addition asserting "the trailers are ALREADY on this
    commit message" to the installed `prepare-commit-msg` shim's `skip_env`
    guard (AC2) -- never inferred by the hook re-reading `COMMIT_EDITMSG`
    (the file read this sentinel exists to avoid paying for).

    Callable ONLY from `commit_scoped`'s agree branch, ONLY immediately
    after `_apply_trailers` returns `None` (no error) -- at that point "the
    trailers are on this message" is a fact this code just established, not
    merely intended, which is exactly what AC2 requires the sentinel to
    mean. Tying this to `compose_message` or to pipeline entry would assert
    "the engine ran" instead, the failure AC2 and the anti-scope both name.

    Finding 4 (same plan): an env var set on a `git commit` invocation is
    inherited by every descendant of that commit's process, not only the
    hook it is aimed at. Harmless here -- the sentinel's one consumer
    (`ensure_prepare_commit_msg_hook`'s `skip_env` guard) only ever means
    "do not re-derive trailers I have no reason to recompute"; no descendant
    process attaches correctness-bearing meaning to its presence.

    Merged into the SAME per-call env dict `_sole_publisher_env` builds for
    this commit -- never a second, independent `env=` kwarg passed to `_git`,
    and never an `os.environ` mutation (`_sole_publisher_env`'s own
    docstring names why: a warm engine would turn a process-global write
    into a cross-request leak).
    """
    return {_TRAILERS_ALREADY_APPLIED_ENV: "1"}


def commit_with_message_file_pathspec_scoped(
    cwd: Union[str, Path],
    msg_file: Union[str, Path],
    paths: Sequence[str],
    *,
    suppress_post_commit_auto_push: bool = False,
    extra_env: Optional[Dict[str, str]] = None,
) -> GitResult:
    """`git commit -F <msg_file> --pathspec-from-file=<f>` — the
    percolate-publish-scale-safe sibling of `commit_with_message_file()`
    (which puts the whole `paths` list on argv, the exact defect this
    module's own docstring section describes commit_scoped() closing).

    `commit_scoped()`'s own agree branch is the sole caller. Deliberately
    NEVER chunked, unlike `_diverging_paths_chunked()`/`add_paths_pathspec_
    file()` above -- `commit_scoped()`'s whole contract is that the named
    pathspec lands as EXACTLY ONE commit; splitting one commit into several
    to dodge an argv limit would trade the Windows argv bug for an
    atomicity bug, which is strictly worse (a half-landed multi-commit
    batch on a shared branch, versus a single command that fails cleanly).
    `--pathspec-from-file` sidesteps the argv limit entirely instead:
    empirically verified SUPPORTED for `git commit` (a commit landed, rc 0,
    against this machine's git 2.55.0.windows.3, in a scratch repo) --
    unlike `git diff --cached --name-only --pathspec-from-file=<f>`, which
    is a usage error (see `_diverging_paths_chunked()`'s own docstring for
    why THAT call stays chunked instead).

    `extra_env` (C2) -- an additional per-call env mapping merged into
    whatever `_sole_publisher_env` builds, never a second independent `env=`
    passed to `_git`. `commit_scoped`'s agree branch is the sole caller that
    supplies it, with `_trailer_sentinel_env()` -- see that function's own
    docstring for why nowhere else may.
    """
    root = Path(cwd)
    pathspec_file = _write_pathspec_file(root, paths)
    try:
        env = _sole_publisher_env(suppress_post_commit_auto_push)
        if extra_env:
            env = {**(env if env is not None else os.environ), **extra_env}
        return _git(
            [
                "commit",
                "-F",
                str(msg_file),
                f"--pathspec-from-file={pathspec_file}",
                "--pathspec-file-nul",
            ],
            cwd=root,
            env=env,
        )
    finally:
        pathspec_file.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# commit_scoped -- the computed commit-mechanism selector (C3).
#
# Neither documented commit form is safe alone on a shared working tree:
#   `git commit -- <paths>` reads the WORKTREE, silently discarding
#     deliberately-staged partial-hunk content (claude-klabauter 506748a0).
#   a bare `git commit` (or one whose pathspec is a DIRECTORY, which matches
#     whatever lands inside it AT COMMIT TIME) commits THE INDEX, silently
#     absorbing whatever a peer session staged (DoE-claude 726925b2).
# `commit_scoped()` is the single entrypoint that computes which mechanism
# is safe for a given explicit path set from OBSERVED index/worktree state
# (via `diverging_paths()`), rather than asking the caller to pick.
#
# Spec backlink: docs/plans/2026-07-27-computed-commit-mechanism-selection.md
# chunk C3.
# ---------------------------------------------------------------------------


#: `(mode, sha, path)` -- one `git ls-files -s` row, as fed to
#: `git update-index --cacheinfo`.
_CacheInfoEntry = Tuple[str, str, str]


def _parse_ls_files_cacheinfo(stdout: str) -> List[_CacheInfoEntry]:
    """Parse `git ls-files -s` output into `(mode, sha, path)` triples.

    Format per line: ``"<mode> <sha> <stage>\\t<path>"``. A path with no
    corresponding line (staged deletion, or never staged at all) simply
    yields no entry -- callers must not assume one-entry-per-input-path.
    """
    entries: List[_CacheInfoEntry] = []
    for line in stdout.splitlines():
        if not line:
            continue
        meta, _, path = line.partition("\t")
        mode, sha, _stage = meta.split()
        entries.append((mode, sha, path))
    return entries


#: Sentinel distinguishing "this path's blob could not be READ" (a `git`
#: subprocess failure) from `None` ("this path genuinely has no entry" --
#: untracked, staged-deletion, absent from HEAD). Identity-compared only
#: (never `==`'d against a real sha string) -- see `_index_blobs`/
#: `_head_blobs`'s own docstrings for the missed-refusal this closes
#: (code-reviewer finding, bf7bab8ce37c review, P1 "degraded reads").
_GIT_READ_FAILED = object()

#: Sentinel distinguishing "git's output could not be tied back to this
#: caller's own key string" from `None` ("this path genuinely has no
#: entry"). Primary cause: a case-divergent match on a case-insensitive
#: filesystem (`core.ignorecase` -- the Windows default) -- git's pathspec
#: matching finds the tracked entry under its OWN tracked case, which never
#: equals the caller's differently-cased key even after `_normalize_path_
#: key`'s deliberately-not-case-folding normalization (see that function's
#: own docstring for why case-folding there would be wrong). Left at `None`
#: this reads as "absent" and silently exempts the path from the whole CAS
#: check (PM follow-up on the P1 path-key-mismatch review finding,
#: bf7bab8ce37c: "an unreconciled caller key must REFUSE, not pass").
_GIT_PATH_UNRECONCILED = object()


def _normalize_path_key(path: str) -> str:
    """Canonical lookup key for reconciling a git-PRINTED path back to the
    CALLER's own key string in `_index_blobs`/`_head_blobs`. Strips a
    `./`-prefix and folds backslash separators to forward slashes -- the two
    reconciliation gaps the P1 "path-key mismatch" review finding named
    alongside C-quoting (which `-z` on the git invocation itself already
    eliminates -- see `_parse_git_z_cacheinfo`). Deliberately does NOT case-
    fold: a genuine case divergence (Windows caller case vs. tracked-case)
    is a real ambiguity this function must not paper over by guessing which
    case is "right" -- callers reconcile via this normalized key and, on a
    genuine remaining miss, keep the git-printed path as its own key rather
    than silently dropping the entry (see `_index_blobs`/`_head_blobs`).
    """
    return path.replace("\\", "/").removeprefix("./")


def _parse_git_z_cacheinfo(stdout: str) -> List[Tuple[List[str], str]]:
    """Split NUL-separated `git ls-files -s -z` / `git ls-tree HEAD -z`
    output into `(meta_fields, path)` pairs -- the shared low-level
    primitive behind both `_index_blobs` and `_head_blobs`'s parsing.

    Consolidated (code-reviewer finding, bf7bab8ce37c review, P2) so a
    porcelain-format fix is applied ONCE, not to two independently hand-
    rolled parsers that can silently drift apart (as `_head_blobs`'s
    previous parser had from `_parse_ls_files_cacheinfo`). `-z` (never the
    newline-separated default) is required so a path containing a non-ASCII
    byte, backslash, quote, or tab is never C-quoted under git's default
    `core.quotePath=true` -- an unquoted key is a precondition for
    `_normalize_path_key` reconciliation to ever succeed (code-reviewer
    finding, bf7bab8ce37c review, P1 "path-key mismatch").

    A malformed/short chunk yields `([], path-or-less)`-shaped tuples where
    `meta_fields` simply has the wrong length -- callers check
    `len(meta_fields)` themselves and skip rather than this function
    guessing at their format's field count.
    """
    entries: List[Tuple[List[str], str]] = []
    for chunk in stdout.split("\0"):
        if not chunk:
            continue
        meta, _, path = chunk.partition("\t")
        entries.append((meta.split(), path))
    return entries


def _case_insensitive_rescan(
    root: Path,
    subcommand_args: Sequence[str],
    still_null_paths: Sequence[str],
) -> Optional[List[Tuple[List[str], str]]]:
    """Re-query `still_null_paths` (paths the PRIMARY, case-sensitive
    pathspec query found no entry for) using git's explicit `:(icase)`
    pathspec magic, and return the parsed `(meta_fields, path)` entries --
    or `None` if the rescan itself failed (git error, not "found nothing").

    Empirically verified (2026-08-14, this repo's own git): a bare literal
    pathspec (`git ls-files -- file.txt`) is CASE-SENSITIVE regardless of
    `core.ignorecase` -- a caller-supplied path differing only in case from
    a tracked `File.txt` matches NOTHING in the primary query, so the path
    reads as ordinary `None` (untracked), not as an unreconciled printed
    entry `_mark_unreconciled` can catch. The real hazard lives one layer
    further down: `git add -- file.txt` DOES silently reuse the existing
    `File.txt` index entry (git's own case-insensitive-filesystem collision
    avoidance -- verified empirically, same session) -- so a CAS snapshot
    that reads `None` for `file.txt` on BOTH sides never trips `moved`, yet
    the agree branch's own `git add`/`git commit` a few lines later DOES
    silently touch the tracked `File.txt` entry. This rescan is what makes
    that case visible to the snapshot: `:(icase)` pathspec magic performs
    an explicit case-INSENSITIVE match (independent of `core.ignorecase`,
    verified empirically), so a still-null caller path that in fact
    resolves to a differently-cased tracked entry is surfaced here and fed
    to `_mark_unreconciled` exactly like a quoting/`./`-prefix mismatch --
    PM follow-up on the P1 path-key-mismatch review finding, bf7bab8ce37c.
    """
    if not still_null_paths:
        return []
    icase_pathspecs = [f":(icase){p}" for p in still_null_paths]
    result = _git([*subcommand_args, "--", *icase_pathspecs], cwd=root)
    if not result.ok:
        return None
    return _parse_git_z_cacheinfo(result.stdout)


def _mark_unreconciled(
    blobs: Dict[str, object],
    paths: Sequence[str],
    matched_normalized: Set[str],
    unreconciled_entries: Sequence[Tuple[str, str]],
) -> None:
    """Force every caller path in `paths` that `git`'s printed output could
    not be tied back to (via `_normalize_path_key`) from its unmatched
    `None` default into `_GIT_PATH_UNRECONCILED`, mutating `blobs` in place.

    `unreconciled_entries` is `(git-printed-path, sha)` pairs already parsed
    but whose normalized form matched no caller key -- the primary cause is
    a case-divergent match (see `_GIT_PATH_UNRECONCILED`'s own docstring).
    Case-INSENSITIVE matching is used ONLY here, to attribute a printed
    entry back to the specific caller path it most likely answers for so
    the refusal message can name it -- never to key `blobs` itself (that
    would be the case-folding `_normalize_path_key`'s own docstring already
    rules out). When no caller path case-insensitively matches a given
    printed entry, every still-unmatched caller path is marked instead --
    conservative by construction: refuse rather than guess which one.
    """
    unmatched = {
        _normalize_path_key(p): p for p in paths if _normalize_path_key(p) not in matched_normalized
    }
    if not unmatched:
        return
    for printed_path, _sha in unreconciled_entries:
        printed_cf = _normalize_path_key(printed_path).casefold()
        candidates = [orig for norm, orig in unmatched.items() if norm.casefold() == printed_cf]
        for target in candidates or list(unmatched.values()):
            blobs[target] = _GIT_PATH_UNRECONCILED


def _trailer_value(msg_text: str, prefix: str) -> Optional[str]:
    """Return the (stripped) value of the first line in `msg_text`'s TRAILER
    BLOCK starting with `prefix` (e.g. `"Deliverable-Id:"`), or `None` when
    the block carries no such line. Same block-aware convention as
    `coordinator_core.git.commit_trailers._has_trailer_line` (a plain
    `str.startswith` scoped to git's own last-paragraph trailer block, not a
    `git interpret-trailers --parse` round-trip), widened here to return the
    VALUE rather than a bool -- `commit_scoped`'s precedence ruling (2) needs
    to compare an existing message trailer's value against an explicit
    caller-supplied `deliverable_id`, not merely know a trailer is present.

    Scoped to the block, not the whole message, because a `Deliverable-Id:`
    line sitting in the BODY is prose git never parses: honouring it made
    `_check_deliverable_id_precedence` raise a conflict against a value no
    consumer could ever read, and let `commit_authored_new_file` accept a
    message whose asserted trailer was unjoinable. Spec: cross-repo/inbox/
    2026-08-26-example-retrieval-repo-em-chunk-trailer-misplacement-defeats-presence-
    check.md.
    """
    for line in _extract_trailer_block(msg_text):
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return None


def _apply_trailers(msg_file, trailer_args: Sequence[str], root) -> Optional["GitResult"]:
    """Append `trailer_args` to `msg_file` in place, spawning
    `git interpret-trailers` ONLY when the message falls outside the shape
    `commit_trailers.can_format_trailers_in_process` has been measured
    byte-identical over.

    Returns `None` on success and the failed `GitResult` otherwise, so the
    three call sites keep their existing early-return shape.

    This is C11's third non-write spawn. The peer plan's ratified anti-scope
    forbids a hand-written replacement that GUESSES at trailer semantics and
    names a byte-identity corpus as what legitimises one; that corpus is
    `state/audits/2026-08-25-interpret-trailers-byte-identity-corpus.py`, and
    the in-process side is identical over 4704/4704 in-envelope cases swept
    across 20 fuzz seeds. Out-of-envelope messages still spawn git rather
    than being guessed at -- which is the anti-scope satisfied rather than
    routed around.
    """
    if not trailer_args:
        return None

    raw = Path(msg_file).read_bytes()
    if can_format_trailers_in_process(raw):
        Path(msg_file).write_bytes(
            format_trailers_in_process(raw, trailer_values_from_argv(trailer_args))
        )
        return None

    result = _git(
        ["interpret-trailers", "--no-divider", "--in-place", *trailer_args, str(msg_file)],
        cwd=root,
    )
    return None if result.ok else result


class DeliverableIdAssertionConflictError(RuntimeError):
    """Raised when a caller-supplied `deliverable_id` disagrees with a
    `Deliverable-Id:` trailer already present in the commit message
    (most often stamped by `commit_anchors.py` from staged plan
    frontmatter, ahead of `_check_deliverable_id_precedence`'s call).

    Deliberately a SIBLING of `coordinator_core.ops.deliverable_carry.
    DivergentDeliverableIdError`, not a subclass of it, though the two
    look alike at a glance (both fire on "two sources disagree on a
    deliverable_id"). `baton_assemble.brief`'s typed catch on
    `DivergentDeliverableIdError` converts THAT error into a
    `j-divergent-deliverable-id` judgment point offering keep-plan /
    keep-predecessor -- a question that is nonsense here: this error
    fires on an AUTHORING assertion conflict (one commit call was handed
    two disagreeing ids for the SAME artifact set being staged), not on
    two artifacts in a carry-or-mint cascade whose provenance a human
    could arbitrate by "which came first". Had this stayed a subclass,
    it would reach that handler and produce a nonsense keep-plan/keep-
    predecessor round trip against a commit that isn't a carry cascade
    at all. A sibling class cannot be caught by that typed handler, so
    it can't be routed there by accident.

    2026-08-10 PM ruling ("FAIL LOUD by raising the existing ...
    DivergentDeliverableIdError") governed the carry-or-mint system this
    class splits away from; the 2026-08-19 PM ruling (DR-328) supersedes
    it, naming this split so a commit assertion conflict gets its own
    class instead of borrowing the carry system's. See `docs/decisions/
    DR-328` and this plan's chunk C4 authorization.

    `caller_facing_validation` (DR-328 chunk C7): this class's message
    already names both disagreeing ids and what the caller must do, so it
    opts into `ipc.py`'s existing duck-type marker rather than inventing a
    parallel signal -- that module's `CallerFacingValidationError` docstring
    asks any future validator composing its own caller-facing message to do
    exactly this. Without it, `_handler_exception_error`'s generic fallback
    collapses this to `INTERNAL_ERROR: "Internal error:
    DeliverableIdAssertionConflictError"`, which is indistinguishable on the
    wire from a genuine engine bug and leaves the operator with a business
    refusal wearing a crash's clothes. Setting the marker uses the envelope's
    EXISTING shape (the emitted `code` becomes INVALID_PARAMS) -- it is not
    an envelope-shape change, which C7's own body reserved for a separate
    plan.
    """

    caller_facing_validation = True


def _check_deliverable_id_precedence(
    msg_text: str, deliverable_id: str
) -> bool:
    """PM ruling (2), `docs/plans/2026-08-10-a-commit-trailer-that-names-
    the-session.md` chunk C7a: the precedence between an explicit caller-
    supplied `deliverable_id` and a pre-existing `Deliverable-Id:` trailer
    already present in `msg_text` (most often stamped by `commit_anchors.py`
    from staged plan frontmatter, AHEAD of this call -- a third source
    `commit_authored_content`'s message-first precedence never has to
    consider, which is why that sibling's precedence is NOT mirrored here;
    see `commit_scoped`'s own docstring).

    Returns:
      True  -- (i) no existing `Deliverable-Id:` line -- the caller's
               explicit value should be applied.
      False -- (ii) an existing line AGREES with `deliverable_id` -- already
               correct; the caller applies nothing further (never a
               duplicate line).

    Raises `DeliverableIdAssertionConflictError` -- (iii) an existing line
    DISAGREES. FAIL LOUD, never silently pick either side. Both `commit_
    scoped` branches propagate this raised, uncaught -- a deliberate,
    NAMED exception to this module's usual "every wrapper returns a
    GitResult, never raises" convention. DR-328 (2026-08-19) split this
    off `DivergentDeliverableIdError` as a SIBLING, not a subclass -- see
    `DeliverableIdAssertionConflictError`'s own docstring for why; the
    original 2026-08-10 PM ruling ("FAIL LOUD by raising the existing ...
    DivergentDeliverableIdError") governed the carry system this class
    split away from and is superseded here by DR-328's naming of this
    split.
    """
    existing = _trailer_value(msg_text, "Deliverable-Id:")
    if existing is None:
        return True

    if existing == deliverable_id:
        return False

    raise DeliverableIdAssertionConflictError(
        f"commit_scoped: caller-supplied deliverable_id {deliverable_id!r} "
        f"disagrees with the commit message's own pre-existing Deliverable-Id "
        f"trailer {existing!r} (most likely stamped by commit_anchors.py from "
        "staged plan frontmatter) -- refusing to silently pick either side. "
        "A caller asserting one deliverable while staging another's "
        "already-stamped artifact is an authoring error fixed by splitting "
        "the commit -- see DeliverableIdAssertionConflictError's own "
        "docstring for why this is not DivergentDeliverableIdError."
    )


def _validate_explicit_deliverable_id(deliverable_id: str, root: Path) -> Optional[str]:
    """AC19 -- the enforceable half of the negative spec on an explicit,
    caller-supplied `deliverable_id`. Returns a diagnostic naming the
    rejected value on failure, or `None` when `deliverable_id` clears both
    checks. Same posture/placement as `commit_scoped`'s own empty-path-set
    and directory-pathspec guards a few lines below where this is called:
    FAILS LOUD (`GitResult.ok is False`), never a warning.

    (a) SHAPE -- must start with `dlv-` OR `pln-`. `dlv-` is the convention
        `coordinator_core.frontmatter.schema_validate._cf_deliverable_id_prefix`
        already enforces for a plan/handoff's own frontmatter field (every
        value is minted by `bin/mint-deliverable-id` with this prefix).
        `pln-` was added in C10b (docs/plans/2026-08-13-spec-backlinks-cite-
        a-stable-deliverable-id.md): the citation convention now PREFERS
        citing a plan by its own `pln-` id (§ PM rulings there, and DoE's
        ratified doctrine at their commit fa72d1642), so an author following
        that convention and passing the resulting id to
        `scoped-git-commit --deliverable-id` must not be rejected here for
        doing what the convention instructs. `--deliverable-id` is
        deliberately ALIASED to accept a `pln-` id rather than adding a
        second `--plan-id` flag for the same validation path (decided; see
        that plan's C10b brief). Reproduced here as a bare `str.startswith`
        rather than imported -- that validator runs over a whole frontmatter
        dict as part of a larger schema pass, not a bare string, so
        importing it would pull in that entire pass for one prefix check.

    (b) EXISTENCE -- must resolve to at least one real artifact (a plan,
        handoff, archived handoff stub, or archived spec) whose OWN
        frontmatter carries this EXACT `deliverable_id` -- reuses
        `coordinator_core.ops.deliverable_rollup.
        _scan_artifacts_by_deliverable_id`, the existing four-path corpus
        scanner (`docs/plans`, `state/handoffs`, `archive/handoffs`,
        `archive/specs`), rather than forking a second one. Imported
        lazily (not at module level) to keep this low-level git module from
        taking on `coordinator_core.ops.*`'s own import surface at load
        time -- the same defensive convention `coordinator_core.git.
        commit_trailers._resolve_deliverable_id_from_paths` already uses for
        its own `DivergentDeliverableIdError` import.

        `scan_incomplete` (a scan root could not be fully enumerated --
        permission denied, etc.) is treated as UNRESOLVED, not accepted --
        per that scanner's own docstring instruction ("callers MUST treat
        True as this result may be missing artifacts"), an unverifiable id
        must never be silently trusted just because the scan could not prove
        it wrong.

    Deliberately does NOT require the artifact to be among the commit's own
    `paths` -- the whole point of this chunk (see the plan's "Scope
    addition" paragraph) is admitting `deliverable_id` on ordinary
    code-only commits that carry no frontmatter-capable artifact of their
    own at all; requiring the id to resolve from THIS commit's pathspec
    would defeat that on the very majority case it exists to serve.
    """
    if not (deliverable_id.startswith("dlv-") or deliverable_id.startswith("pln-")):
        return (
            f"commit_scoped: deliverable_id {deliverable_id!r} rejected -- "
            "does not match the 'dlv-' or 'pln-' shape convention (every "
            "deliverable_id is minted by bin/mint-deliverable-id and every "
            "plan_id by bin/mint-plan-id with these prefixes respectively; "
            "see coordinator_core/frontmatter/schema_validate.py's own "
            "deliverable_id-prefix check)"
        )

    from coordinator_core.ops.deliverable_rollup import _scan_artifacts_by_deliverable_id

    matches, scan_incomplete = _scan_artifacts_by_deliverable_id(root, deliverable_id)
    if scan_incomplete or not matches:
        incomplete_note = (
            " (a scan root could not be fully enumerated -- treating as "
            "unresolved rather than trusting an unverifiable id)"
            if scan_incomplete
            else ""
        )
        return (
            f"commit_scoped: deliverable_id {deliverable_id!r} rejected -- does "
            "not resolve to any real artifact (a plan, handoff, archived "
            "handoff stub, or archived spec) carrying this deliverable_id in "
            f"its own frontmatter{incomplete_note}"
        )
    return None


def _index_blobs(root: Path, paths: Sequence[str], *, fresh: bool = False) -> Dict[str, object]:
    """Return `{path: index-blob-sha}` for `paths`, read via
    `coordinator_core.git.git_state.read_index` -- SPAWN-FREE (C2,
    2026-08-21, docs/plans/2026-08-16-one-engine-for-the-whole-box.md;
    state/dispatch-briefs/2026-08-21-the-commit-path-reads-git-state-
    without-spawning-git/C2.md). A path with no index entry (untracked, or
    deleted-from-index) maps to `None`. On a `read_index` failure
    (`IndexParseError` -- a malformed/unsupported/split index, or any
    unmerged entry), EVERY entry maps to `_GIT_READ_FAILED` -- NOT `None`
    -- so a caller comparing against a prior snapshot can never read a
    degraded read as "genuinely absent" (code-reviewer finding,
    bf7bab8ce37c review, P1 "degraded reads": the previous all-`None`
    degrade was claimed refuse-leaning but was not -- see
    `_agree_branch_cas_refusal`'s own use of this sentinel for the two
    directions that claim was false in). `read_index` itself never returns
    a partial/empty result on a parse failure (see its own docstring); a
    genuinely absent `.git/index` (unborn repo) is the one legitimate
    empty-result case and reads as ordinary `None` for every path here,
    same as before.

    Reconciled back to the caller's own key string via `_normalize_path_key`
    (a `./`-prefix or backslash-separator difference no longer leaves the
    caller's key permanently unmatched -- code-reviewer finding, bf7bab8ce37c
    review, P1 "path-key mismatch"). The `git`-PRINTED-output shape of
    `_GIT_PATH_UNRECONCILED` (a printed path this function cannot tie back to
    any caller key) is structurally unreachable here -- there is no git
    output to fail to key back, `read_index` hands back the WHOLE index as a
    plain dict this function looks up directly. Its CASE-DIVERGENCE cause
    remains fully reachable, though, now detected in-memory instead of via a
    second `:(icase)` git spawn: `read_index` already returns every tracked
    path in one call, so a caller key with no exact normalized match is
    checked against the SAME snapshot's casefolded keys before falling back
    to `None` -- deviation from this chunk's own brief aside (which read this
    sentinel as index-side-unreachable outright); dropping it silently
    regressed `test_agree_branch_cas_refuses_case_divergent_caller_path` /
    `test_op_surfaces_case_divergent_cas_refusal_as_a_failure_not_a_noop`
    (test_scoped_git_commit.py) -- the exact "git add silently reuses an
    existing differently-cased index entry" hazard this sentinel exists to
    catch (see `_GIT_PATH_UNRECONCILED`'s own module-level docstring) is an
    INDEX-side fact end to end, so losing it here loses it entirely, not just
    losing one detection route. The sentinel itself is never deleted
    (`_head_blobs`, below, also still reaches it) -- see that function's own
    docstring.

    No `_chunk_paths()` batching here -- unlike the old `git ls-files -s -z`
    spawn, `read_index` takes no per-call argv at all (it parses the whole
    on-disk index file once), so the Windows `CreateProcess` argv-length
    hazard chunking existed to avoid does not apply to this read.

    `fresh` (C2, 2026-08-26,
    docs/plans/2026-08-26-the-close-path-spends-its-last-known-levers.md):
    forwarded verbatim to `read_index`. `_agree_branch_cas_refusal`'s
    CURRENT re-observation passes `fresh=True` -- it exists to observe a
    peer touching the index at a specific instant, so it must never be
    served from `index_read_cache_scope()`'s within-call cache (see that
    function's own docstring and AC3 of the plan above). Every other caller
    leaves this at its default `False` and may be served from the cache
    when one is open.
    """
    if not paths:
        return {}
    try:
        snapshot = read_index(root, fresh=fresh)
    except IndexParseError:
        return {p: _GIT_READ_FAILED for p in paths}
    index_by_normalized = {_normalize_path_key(p): entry for p, entry in snapshot.items()}
    blobs: Dict[str, object] = {}
    unmatched: List[str] = []
    for p in paths:
        entry = index_by_normalized.get(_normalize_path_key(p))
        if entry is not None:
            blobs[p] = entry.sha
        else:
            blobs[p] = None
            unmatched.append(p)
    if unmatched:
        casefolded_keys = {norm.casefold() for norm in index_by_normalized}
        for p in unmatched:
            if _normalize_path_key(p).casefold() in casefolded_keys:
                blobs[p] = _GIT_PATH_UNRECONCILED
    return blobs


def _head_blobs(root: Path, paths: Sequence[str]) -> Dict[str, object]:
    """Return `{path: HEAD-blob-sha}` for `paths`, via
    `coordinator_core.git.git_state.read_tree_spine` -- C3's in-process
    HEAD-tree reader (C11, `state/dispatch-briefs/2026-08-23-the-scoped-
    commit-rebuilt-from-first-principles/C11.md`; census: `docs/plans/
    2026-08-23-the-scoped-commit-rebuilt-from-first-principles.md` "What
    the census established", spawn #1). Walks only the directory spine each
    path needs (never `git ls-tree`), replacing this call site's former
    `git_state.head_blobs` spawn -- that function's own memoised spawn
    remains in place for its OTHER call sites in this module
    (`_reject_stale_index_paths`, `_resolve_content_sources`), which are
    outside this chunk's scope. A path absent from HEAD (new/untracked, or
    an unborn branch with no commits yet -- `read_tree_spine` returns `None`
    for either) maps to `None`, matching this function's pre-C11 fold for
    the same case.

    No reconciliation pass needed here, unlike the spawn-backed version this
    replaces: `read_tree_spine`'s tree-entry names are looked up by DIRECT
    dict key (this function's own `paths`, split on `/`), never round-
    tripped through a git-printed line, so there is no printed-path string
    to diverge from the caller's key and `_GIT_PATH_UNRECONCILED` is
    structurally unreachable from this call site now. This does not weaken
    the case-divergence guard `_agree_branch_cas_refusal` relies on -- as
    that function's own docstring already notes, the hazard is entirely an
    INDEX-side fact, caught by `_index_blobs`'s own `:(icase)` rescan before
    `_head_blobs`'s answer for the same path is ever consulted; a
    case-mismatched key here now simply reads as "absent from HEAD" (`None`),
    the same terminal fold the old reconciliation path produced for it via
    `_GIT_PATH_UNRECONCILED` -> refusal one layer up.

    `_GIT_READ_FAILED` is reachable only for a genuine exception out of
    `read_tree_spine` itself (e.g. the `.git` directory cannot be resolved)
    -- caught here and forced onto every requested path, never silently
    downgraded to `None`. An unresolvable/corrupt HEAD is NOT such a case:
    `read_tree_spine` returns `None` (not a raise) for it, which this
    function folds to ordinary absent-from-HEAD `None` for every path,
    matching the unborn-repo case above.
    """
    if not paths:
        return {}
    blobs: Dict[str, object] = {p: None for p in paths}
    try:
        spine = read_tree_spine(root, paths)
    except Exception:
        return {p: _GIT_READ_FAILED for p in paths}
    if spine is None:
        return blobs
    for p in paths:
        parts = p.split("/")
        dirpath = "/".join(parts[:-1])
        name = parts[-1]
        entries = spine.get(dirpath)
        if entries is None:
            continue
        entry = entries.get(name)
        if entry is not None:
            blobs[p] = entry[1]
    return blobs


#: Shared substring both this module's index/HEAD CAS-refusal diagnostics
#: lead their `stderr` with -- `_agree_branch_cas_refusal` (below) and
#: `_commit_via_head_spine`'s AC11(b) index re-check alike (C1,
#: claude-klabauter-75). A single constant, not independently-typed prose in
#: each site, precisely because independently-typed prose is how
#: `commit_pipeline._classify_commit_scoped_failure_reason`'s marker match
#: diverged from `_commit_via_head_spine`'s wording in the first place --
#: `commit_pipeline.py` imports this constant directly rather than
#: re-typing it, so the two can never drift apart again.
INDEX_HEAD_CAS_MARKER = "compare-and-swap refused"

#: Distinguishing substring of `_commit_via_head_spine`'s own AC11(b)
#: index-`stat_identity` re-check failure (C1, claude-klabauter-75) --
#: deliberately NOT `INDEX_HEAD_CAS_MARKER` above, because that check's
#: `GitResult.stderr` keeps its pre-existing `"compare-and-swap failed"`
#: lead (pinned by `test_commit_scoped_edges.py`/
#: `test_commit_authored_content_edges.py`'s `"compare-and-swap failed"`
#: assertions, which also cover this helper's UNRELATED ref-CAS "HEAD
#: moved" failures -- rewording the lead to match `INDEX_HEAD_CAS_MARKER`
#: would blur those two failure families together at the substring level).
#: This marker is the middle clause unique to the index-changed case, kept
#: as a single named constant (not retyped in `commit_pipeline.py`) for the
#: same reason `INDEX_HEAD_CAS_MARKER` is: independently-typed prose in two
#: files is how the classifier's marker match diverged from this call
#: site's wording in the first place.
INDEX_STAT_CAS_MARKER = "the shared index changed since it was snapshotted for this commit"


def _agree_branch_cas_refusal(
    root: Path,
    path_list: Sequence[str],
    pre_index_blobs: Dict[str, object],
    pre_head_blobs: Dict[str, object],
) -> Optional["GitResult"]:
    """Intra-invocation compare-and-swap for `commit_scoped()`'s AGREE branch
    (2026-08-14, `state/audits/2026-08-14-scoped-commit-partial-stage-
    sweep.md`, "Layer 1"). `commit_scoped()` snapshots each path's index/HEAD
    blob BEFORE its own `diverging_paths()` call; this function re-observes
    both right before the agree branch's `git add` and refuses (rather than
    silently proceeding) if either signal shows the world moved inside THIS
    call's own check-then-act window:

      - the path's INDEX entry no longer matches the snapshot (some
        writer -- a peer's `git add`, a peer's own agree-branch stage, a
        peer's private-index commit that never touches the shared index --
        touched it), or
      - HEAD now carries exactly the blob that was staged at snapshot time,
        while that staged blob differed from HEAD's blob AT snapshot time
        (the incident's own tell: "my staged content is already in
        history -- someone else committed it"). Deliberately conditioned on
        `pre_index != pre_head` at snapshot time -- an ordinary already-
        staged==HEAD path (nothing pending) must never trip this, or every
        ordinary no-op commit attempt would refuse.

    Returns `None` when neither signal fires (the overwhelming-majority
    case -- nothing moved). Returns a failed `GitResult` naming which paths
    tripped which signal otherwise; `commit_scoped()` returns this directly,
    never falling through to `git add`.

    Deliberately session-blind, matching every other predicate in this
    module (PM ruling, `_check_claim_conflicts()` removal, 2026-08-13): this
    is a blob-identity check, not an ownership/claim gate -- it does not ask
    WHO moved the world, only THAT it moved.

    Degraded-read posture (code-reviewer finding, bf7bab8ce37c review, P1
    "degraded reads"): `_index_blobs`/`_head_blobs` map a `git` failure to
    `_GIT_READ_FAILED`, a sentinel distinct from `None` (genuine absence).
    Any path whose PRE or CURRENT index read failed is forced into `moved`
    unconditionally -- a degraded snapshot no longer silently disables the
    absorbed-check for the rest of the call (it forces immediate refusal
    instead of leaving `absorbed_candidates` permanently unable to see it),
    and a degraded-vs-degraded pair no longer reads as `None == None` ->
    "nothing moved" (identity-compared, `_GIT_READ_FAILED != _GIT_READ_FAILED`
    is never reached -- the explicit `is` check above it fires first). A
    degraded CURRENT `_head_blobs` read during the absorbed re-check is
    treated the same way: unable to confirm the path is safe, so it is
    added to `absorbed` (refuse) rather than silently excluded.

    Unkeyable-path posture (PM follow-up on the P1 path-key-mismatch
    finding, bf7bab8ce37c review): `_index_blobs`/`_head_blobs` map a path
    git's output could not be tied back to the caller's own key string
    (case divergence, primarily) to `_GIT_PATH_UNRECONCILED`, distinct from
    both `None` (genuinely absent) and `_GIT_READ_FAILED` (git itself
    failed). Any path whose PRE or CURRENT index read is unreconciled is
    refused immediately and reported by name (`unkeyable`, below), never
    folded into the generic `moved` wording that implies a real content
    change was observed -- there is no observation here, only an inability
    to key the path at all.
    """
    # fresh=True (C2): this call exists to observe the index at THIS
    # instant, never a within-call cache's stale look -- see `_index_blobs`'s
    # own `fresh` docstring and this module's AC3.
    current_index_blobs = _index_blobs(root, path_list, fresh=True)
    moved: List[str] = []
    unkeyable: List[str] = []
    for p in path_list:
        cur = current_index_blobs.get(p)
        pre = pre_index_blobs.get(p)
        if cur is _GIT_PATH_UNRECONCILED or pre is _GIT_PATH_UNRECONCILED:
            unkeyable.append(p)
        elif cur is _GIT_READ_FAILED or pre is _GIT_READ_FAILED or cur != pre:
            moved.append(p)

    excluded = set(moved) | set(unkeyable)
    absorbed_candidates = [
        p
        for p in path_list
        if p not in excluded
        and pre_index_blobs.get(p) is not None
        and pre_index_blobs.get(p) != pre_head_blobs.get(p)
    ]
    absorbed: List[str] = []
    if absorbed_candidates:
        current_head_blobs = _head_blobs(root, absorbed_candidates)
        for p in absorbed_candidates:
            chb = current_head_blobs.get(p)
            if chb is _GIT_READ_FAILED or chb is _GIT_PATH_UNRECONCILED or chb == pre_index_blobs.get(p):
                absorbed.append(p)

    if not moved and not absorbed and not unkeyable:
        return None

    detail: List[str] = []
    if moved:
        detail.append(f"index entry changed since this call's own snapshot: {', '.join(moved)}")
    if unkeyable:
        detail.append(
            "could not be matched to an index/HEAD entry (git's output did not "
            f"reconcile to this caller-supplied path -- likely a case-divergent "
            f"match): {', '.join(unkeyable)}"
        )
    if absorbed:
        detail.append(
            "HEAD now carries this call's own staged blob (a peer committed it "
            f"first): {', '.join(absorbed)}"
        )
    return GitResult(
        returncode=-1,
        stdout="",
        stderr=(
            f"commit_scoped: {INDEX_HEAD_CAS_MARKER} -- "
            + "; ".join(detail)
            + ". Re-run once current state is freshly re-observed; refusing to "
            "restage from the worktree over content that may already be "
            "someone else's committed history "
            "(state/audits/2026-08-14-scoped-commit-partial-stage-sweep.md)."
        ),
    )


#: The three content sources `_resolve_content_sources` can resolve a path
#: to. String constants (not an enum) so they compare/format cheaply and
#: read plainly in a diagnostic -- see that function's own docstring.
_SOURCE_SUPPLIED = "supplied-blob"
_SOURCE_STAGED = "staged-blob"
_SOURCE_WORKTREE = "worktree"

#: Fallback mode for a supplied-blob cacheinfo entry with no prior entry
#: anywhere (a genuinely brand-new file) -- `_resolve_mode_for_paths()`,
#: below, is consulted FIRST for every `supplied_paths` member, so this
#: constant is reached only when a path has neither a real-index nor a
#: HEAD-tree entry to inherit a mode from. Previously (until this fix)
#: applied unconditionally to every supplied-blob path regardless of an
#: existing entry's mode, which would have silently downgraded an
#: already-`100755` path the first time a real `supplied_blobs` producer
#: existed (`stage_from_patch()`, C2, has none yet -- this was latent, not
#: the observed live incident). `100644` (ordinary non-executable file) is
#: still the right default for a path with no prior entry to inherit from.
_SUPPLIED_BLOB_MODE = "100644"


def _resolve_mode_for_paths(root: Path, paths: Sequence[str]) -> Dict[str, str]:
    """Mode string (e.g. `"100644"`/`"100755"`) for each of `paths`,
    preferring the REAL index's currently-staged entry and falling back to
    HEAD's tree entry for a path with no index entry -- the chmod-
    preservation lookup a supplied-blob cacheinfo entry needs so
    `_SUPPLIED_BLOB_MODE`'s hardcoded `100644` is used ONLY for a path with
    no prior entry anywhere (a genuinely brand-new file), never to silently
    downgrade an existing `100755`.

    Read-only against the REAL repo state -- never the private temp index
    `_commit_scoped_private_index` is mid-building when this is called --
    same source the index side now reads via `git_state.read_index()`
    (C2, 2026-08-21 -- re-pointed off the `git ls-files -s` spawn this
    function's own INDEX lookup previously issued; see `_index_blobs`'s
    docstring for the shared rationale), so a supplied-blob path and a
    staged-blob path resolve their mode from the identical vantage point.

    Unchunked -- `supplied_paths` is bounded by the same pathspec every
    other real read in `_commit_scoped_private_index` is scoped to, never
    the whole-repo scale `_diverging_paths_chunked()` exists for. A path
    absent from BOTH the index and HEAD is simply absent from the returned
    dict -- callers fall back to `_SUPPLIED_BLOB_MODE` for it, never a
    guessed entry here. The HEAD-side fallback still goes through
    `git_state.head_blobs()`'s one retained `git ls-tree` spawn -- see
    `_head_blobs`'s own docstring for that call's failure/reconciliation
    posture, mirrored here at a smaller scale (a missing/failed read for a
    path here is simply absent from the returned dict, never a distinct
    sentinel -- this function's own contract, unlike `_index_blobs`/
    `_head_blobs`, was never sentinel-bearing).
    """
    if not paths:
        return {}
    modes: Dict[str, str] = {}
    try:
        snapshot = read_index(root)
    except IndexParseError:
        snapshot = None
    if snapshot is not None:
        index_by_normalized = {_normalize_path_key(p): entry for p, entry in snapshot.items()}
        for p in paths:
            entry = index_by_normalized.get(_normalize_path_key(p))
            if entry is not None:
                modes[p] = f"{entry.mode:06o}"
    missing = [p for p in paths if p not in modes]
    if missing:
        try:
            raw = _git_state_head_blobs(root, missing)
        except Exception:
            raw = {}
        key_by_normalized = {_normalize_path_key(p): p for p in missing}
        for path, (mode, _sha) in raw.items():
            key = key_by_normalized.get(_normalize_path_key(path))
            if key is not None:
                modes[key] = f"{mode:06o}"
    return modes


@dataclass(frozen=True)
class StagePatchResult:
    """Typed result of `stage_from_patch()` -- C2,
    docs/plans/2026-08-14-the-tool-stages-what-it-commits.md.

    Fields:
        ok — True iff the patch applied cleanly under the private index and
            every named path was keyable. False on any refusal (see
            `reason`); on False, `blobs`/`head_blobs` are both `{}` --
            never a partial result a caller could mistake for "some paths
            succeeded" (AC3's atomicity: nothing is committed to
            `_commit_scoped_private_index` on a False result).
        blobs — `{path: blob_sha}` for every path in the bounded pathspec
            the patch actually wrote NEW content for (as read back from the
            private index post-apply). A path in the pathspec the patch did
            not touch is simply absent here -- this is the additive-per-path
            shape AC4 (C3) consumes; `stage_from_patch()` itself makes no
            claim about what happens to an absent path (that is a
            downstream, C3, resolution question via `_resolve_content_
            sources`).
        head_blobs — `{path: HEAD-blob-sha-or-None}` for every path in the
            bounded pathspec, taken via `_head_blobs()` BEFORE the private
            index is touched -- the AC2 base-hole snapshot. A caller
            committing these `blobs` later MUST re-check this snapshot
            against HEAD immediately before committing via
            `stage_from_patch_cas_refusal()` (this module) -- never commit
            straight off a `StagePatchResult` without re-observing HEAD.
        stderr — diagnostic text on a `False` result; "" on success.
        reason — a short machine-readable tag distinguishing refusal causes
            ("apply-failed", "index-infra-failure", "unkeyable-path",
            "empty-pathspec", "directory-pathspec"); "" on success. Callers
            building AC7's distinct exit paths (C3) key off this, not off
            `stderr` text. "apply-failed" is reserved for a genuine bad-hunk
            (the patch's own content does not apply, verified not already-
            applied via the reverse-check below); "index-infra-failure" is
            the private index's own plumbing breaking (`read-tree HEAD`, the
            post-apply `ls-files` read) -- a corrupt/unreadable repo state,
            not a patch/content problem, and worth distinguishing from
            "apply-failed" for anyone debugging off `reason` alone (review:
            coordinator:code-reviewer 1ead6ae2, finding 1).
    """

    ok: bool
    blobs: Dict[str, str]
    head_blobs: Dict[str, object]
    stderr: str = ""
    reason: str = ""


def patch_touched_paths(patch_path: Union[str, Path], cwd: Union[str, Path]) -> Set[str]:
    """AC4 (C3, docs/plans/2026-08-14-the-tool-stages-what-it-commits.md):
    the set of paths `patch_path` touches, WITHOUT applying it or mutating
    anything -- `git apply --numstat` parses the patch text only, spawning
    no read against the repository's own index/worktree/HEAD. Used by the
    now-killed `commit_pipeline.run_commit_pipeline()` to decide, BEFORE
    staging ran, which of the caller's `stage_paths` a `stage_patch` would
    cover (and must therefore never see an ordinary `git add`) vs. which
    fell through to the staged-or-worktree route unchanged (and DID need an
    ordinary `git add` so the dirty-tree gate could attribute them).
    `coordinator_core.git.commit.commit_paths` (C3's repoint target) has no
    `stage_patch` concept and does not call this function.

    Returns an empty set (never raises) on a malformed/unreadable patch --
    the caller is expected to treat "nothing covered" the same as any other
    zero-overlap case; `stage_from_patch()` itself is the one authority that
    FAILS LOUD on a genuinely bad patch, at apply time.
    """
    result = _git(["apply", "--numstat", str(patch_path)], cwd=Path(cwd))
    if not result.ok:
        return set()
    touched: Set[str] = set()
    for line in result.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3:
            touched.add(_normalize_path_key(parts[2].strip()))
    return touched


def stage_from_patch(
    patch_path: Union[str, Path],
    paths: Sequence[str],
    cwd: Union[str, Path],
) -> StagePatchResult:
    """Apply `patch_path` under a process-private temporary index (`GIT_INDEX_
    FILE`, seeded from `read-tree HEAD` -- the SAME idiom `_commit_scoped_
    private_index` already uses, deliberately not reinvented here), bounded
    to `paths`, and return the blob each named path resolved to.

    NEVER writes to the shared repo index. This is the whole load-bearing
    property of the plan (Approach, staff-eng review findings 1/1a/1b): a
    private index is unforgeable by a peer, so a blob this function reports
    is provenance BY CONSTRUCTION, never by asking who a session is. If a
    future edit here reaches for the shared index "for convenience", it
    reintroduces the exact incident this plan exists to close
    (`state/audits/2026-08-14-scoped-commit-partial-stage-sweep.md`).

    AC2's base hole: `head_blobs` on the returned `StagePatchResult` is each
    path's HEAD blob taken via `_head_blobs()` BEFORE the private index is
    touched (git-tree-atomic with the `read-tree HEAD` that seeds the
    private index a few lines later -- both read the same ref). A caller
    that later commits `.blobs` MUST re-check `.head_blobs` against a FRESH
    `_head_blobs()` read immediately before committing
    (`stage_from_patch_cas_refusal`, below) -- if a peer committed to one of
    these paths between this call and that later commit, the mirror-image
    CAS refuses rather than silently reverting the peer's committed hunks.
    This function itself does not re-check at commit time (staging and
    committing are two different moments; the caller owns bridging them in
    one invocation, per the plan's anti-scope: no persisted fingerprint).

    AC3's atomicity: `git apply --cached` against the private temp index is
    all-or-nothing per invocation. A failed apply (bad patch, hunk that does
    not apply) returns `ok=False, reason="apply-failed"` with `blobs={}` --
    the temp index is discarded (`finally: unlink`) and the shared index was
    never touched, so there is no residue to roll back.

    Reuse, not re-derivation: a path this primitive cannot key back to a
    caller-supplied string (`_GIT_PATH_UNRECONCILED`) or whose read failed
    outright (`_GIT_READ_FAILED`) is a REFUSAL of the whole call
    (`reason="unkeyable-path"`), exactly the posture `_agree_branch_cas_
    refusal` already takes for the same two sentinels -- never a silent
    per-path skip that could leave `blobs` looking complete when it is not.
    """
    root = Path(cwd)
    path_list = list(paths)

    if not path_list:
        return StagePatchResult(
            ok=False, blobs={}, head_blobs={},
            stderr="stage_from_patch: empty pathspec refused -- nothing to bound the apply to",
            reason="empty-pathspec",
        )

    dir_paths = directory_pathspecs(root, path_list)
    if dir_paths:
        return StagePatchResult(
            ok=False, blobs={}, head_blobs={},
            stderr=f"stage_from_patch: {directory_pathspec_diagnostic(dir_paths[0])}",
            reason="directory-pathspec",
        )

    # AC2 base-hole snapshot -- taken BEFORE the private index exists, off
    # the SAME HEAD the private index is about to be seeded from a few
    # lines below. Any degraded/unkeyable read here refuses the WHOLE call
    # rather than silently staging over a path this primitive cannot later
    # prove was untouched by a peer commit.
    head_blobs_at_apply = _head_blobs(root, path_list)
    if any(
        v is _GIT_READ_FAILED or v is _GIT_PATH_UNRECONCILED
        for v in head_blobs_at_apply.values()
    ):
        return StagePatchResult(
            ok=False, blobs={}, head_blobs={},
            stderr=(
                "stage_from_patch: refusing -- HEAD blob could not be read/keyed "
                f"for one or more paths: {path_list}"
            ),
            reason="unkeyable-path",
        )

    temp_index = Path(tempfile.gettempdir()) / f"git-index-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        private_env: Dict[str, str] = dict(os.environ)
        private_env["GIT_INDEX_FILE"] = str(temp_index)

        read_tree_result = _git(["read-tree", "HEAD"], cwd=root, env=private_env)
        if not read_tree_result.ok:
            # Infra/plumbing failure, not a patch-content problem -- distinct
            # from the genuine bad-hunk "apply-failed" below (review:
            # coordinator:code-reviewer 1ead6ae2, finding 1).
            return StagePatchResult(
                ok=False, blobs={}, head_blobs={},
                stderr=f"stage_from_patch: read-tree HEAD failed -- {read_tree_result.stderr}",
                reason="index-infra-failure",
            )

        apply_args = [
            "apply", "--cached",
            *[f"--include={p}" for p in path_list],
            str(patch_path),
        ]
        apply_result = _git(apply_args, cwd=root, env=private_env)
        if not apply_result.ok:
            # AC6 (docs/plans/2026-08-14-the-tool-stages-what-it-commits.md,
            # the audit's S5 timeline): a peer's ordinary commit landed the
            # SAME content this patch would produce, between this patch's
            # own computation and this apply -- HEAD (what the private
            # index above was seeded from) already carries the target
            # content, so the literal context match fails even though there
            # is nothing left to change. `git apply --check --reverse`
            # detects exactly this shape (the patch reverses cleanly, i.e.
            # its POST-image is already present) without mutating anything.
            # Only on that confirmation is the forward failure treated as
            # already-satisfied rather than a genuine apply failure -- an
            # ordinary bad-hunk failure does not reverse-apply cleanly
            # either, so this never masks a real `patch-did-not-apply`.
            reverse_check = _git(
                ["apply", "--cached", "--check", "--reverse", *apply_args[2:]],
                cwd=root,
                env=private_env,
            )
            if not reverse_check.ok:
                return StagePatchResult(
                    ok=False, blobs={}, head_blobs={},
                    stderr=f"stage_from_patch: git apply --cached failed -- {apply_result.stderr}",
                    reason="apply-failed",
                )

        ls_files_result = _git(
            ["ls-files", "-s", "-z", "--", *path_list], cwd=root, env=private_env
        )
        if not ls_files_result.ok:
            # Infra/plumbing failure (the apply itself already succeeded
            # above) -- same "index-infra-failure" tag as the read-tree
            # failure, distinct from "apply-failed" (review:
            # coordinator:code-reviewer 1ead6ae2, finding 1).
            return StagePatchResult(
                ok=False, blobs={}, head_blobs={},
                stderr=f"stage_from_patch: post-apply ls-files failed -- {ls_files_result.stderr}",
                reason="index-infra-failure",
            )

        blobs: Dict[str, str] = {}
        key_by_normalized = {_normalize_path_key(p): p for p in path_list}
        matched: Set[str] = set()
        unreconciled: List[Tuple[str, str]] = []
        for parts, printed_path in _parse_git_z_cacheinfo(ls_files_result.stdout):
            if len(parts) != 3:
                continue
            _mode, sha, _stage = parts
            norm = _normalize_path_key(printed_path)
            key = key_by_normalized.get(norm)
            if key is not None:
                blobs[key] = sha
                matched.add(norm)
            else:
                unreconciled.append((printed_path, sha))

        if unreconciled:
            placeholder: Dict[str, object] = {p: (blobs.get(p) or None) for p in path_list}
            _mark_unreconciled(placeholder, path_list, matched, unreconciled)
            unkeyable = [p for p in path_list if placeholder[p] is _GIT_PATH_UNRECONCILED]
            if unkeyable:
                return StagePatchResult(
                    ok=False, blobs={}, head_blobs={},
                    stderr=(
                        "stage_from_patch: refusing -- git's post-apply output could "
                        f"not be tied back to the caller's own path key: {unkeyable}"
                    ),
                    reason="unkeyable-path",
                )

        # AC4 (C3, docs/plans/2026-08-14-the-tool-stages-what-it-commits.md):
        # `git ls-files -s` above reads back EVERY entry the private index
        # holds for `path_list`, not just what THIS apply touched -- a path
        # in `path_list` the patch never mentions still has a pre-existing
        # (HEAD-seeded) cache entry, and would otherwise be reported here as
        # "the patch supplied this blob" when it did not. `patch_touched_
        # paths()` answers "which paths did this patch's own hunks name",
        # parsed from the patch text alone -- restricting `blobs` to that
        # intersection is what makes AC4's additive-per-path split honest:
        # a path absent from the patch is absent from `blobs`, full stop,
        # never merely absent from `matched`/`unreconciled`'s keying
        # bookkeeping above (which is a DIFFERENT question -- whether git
        # could tie a post-apply entry back to a caller key at all).
        touched = patch_touched_paths(patch_path, root)
        blobs = {p: sha for p, sha in blobs.items() if _normalize_path_key(p) in touched}

        return StagePatchResult(
            ok=True, blobs=blobs, head_blobs=head_blobs_at_apply, stderr="", reason="",
        )
    finally:
        temp_index.unlink(missing_ok=True)


def stage_from_patch_cas_refusal(
    root: Union[str, Path],
    path_list: Sequence[str],
    head_blobs_at_apply: Dict[str, object],
) -> Optional["GitResult"]:
    """AC2's base-hole CAS -- the mirror image of `_agree_branch_cas_
    refusal`. A caller about to COMMIT the blobs `stage_from_patch()`
    produced calls this IMMEDIATELY before that commit (no probe-then-act
    gap -- plan anti-scope), passing the SAME `head_blobs` that call
    returned. Re-observes HEAD fresh and refuses (rather than silently
    reverting a peer's committed hunks) if any named path's HEAD blob has
    changed since the apply -- i.e. a peer committed to that path between
    this invocation's `stage_from_patch()` call and the commit this guards.

    Neither the ref-level `update-ref` CAS in `_commit_scoped_private_index`
    nor `_agree_branch_cas_refusal` observes this on their own: the former
    only refuses if the WHOLE branch tip moved (a peer's commit to an
    unrelated path also trips it, and it says nothing about which of ITS
    OWN supplied paths are stale); the latter only ever runs on the
    worktree/staged-blob agree branch, never on a caller-supplied blob.

    Returns `None` when every path's HEAD blob is unchanged (the
    overwhelming-majority case). Returns a failed `GitResult` naming which
    paths moved/could not be re-keyed otherwise -- callers pass this
    straight through as the commit's own failure, never falling through to
    commit anyway.
    """
    root = Path(root)
    current_head_blobs = _head_blobs(root, path_list)
    moved: List[str] = []
    unkeyable: List[str] = []
    for p in path_list:
        cur = current_head_blobs.get(p)
        pre = head_blobs_at_apply.get(p)
        if cur is _GIT_PATH_UNRECONCILED or pre is _GIT_PATH_UNRECONCILED:
            unkeyable.append(p)
        elif cur is _GIT_READ_FAILED or pre is _GIT_READ_FAILED or cur != pre:
            moved.append(p)

    if not moved and not unkeyable:
        return None

    detail: List[str] = []
    if moved:
        detail.append(
            f"HEAD blob changed since stage_from_patch() recorded it -- a peer "
            f"committed to this path first: {', '.join(moved)}"
        )
    if unkeyable:
        detail.append(
            "could not be matched to a HEAD entry (git's output did not "
            f"reconcile to this caller-supplied path): {', '.join(unkeyable)}"
        )
    return GitResult(
        returncode=-1,
        stdout="",
        stderr=(
            "stage_from_patch_cas_refusal: refused -- "
            + "; ".join(detail)
            + ". Re-run stage_from_patch() once current HEAD is freshly re-observed; "
            "refusing to commit a supplied blob over content that may already be "
            "someone else's committed history "
            "(docs/plans/2026-08-14-the-tool-stages-what-it-commits.md)."
        ),
    )


def _resolve_content_sources(
    diverged: Sequence[str],
    non_diverged: Sequence[str],
    supplied_blobs: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Resolve every path in `_commit_scoped_private_index`'s pathspec to
    EXACTLY ONE content source -- `_SOURCE_SUPPLIED` | `_SOURCE_STAGED` |
    `_SOURCE_WORKTREE` -- as a single total function over the union of
    `diverged`, `non_diverged`, and `supplied_blobs`'s keys, computed once.

    Plan backlink: docs/plans/2026-08-14-the-tool-stages-what-it-commits.md
    chunk C1. Replaces the prior scheme, where `_commit_scoped_private_index`
    asked two questions in the right order (is this path in `diverged`? is
    it in `non_diverged`?) and a caller-supplied blob's precedence over both
    survived only because `git add -- non_diverged` happened to run BEFORE
    the cacheinfo loop that would otherwise apply a supplied blob -- nothing
    stated that ordering as a rule. Here the precedence is the function
    itself, not a side effect of statement order in the caller.

    Precedence (stated, not incidental): `supplied_blobs` wins first -- ANY
    path present in `supplied_blobs` resolves to `_SOURCE_SUPPLIED` and can
    NEVER also resolve to `_SOURCE_STAGED` or `_SOURCE_WORKTREE`, regardless
    of whether that same path also appears in `diverged`/`non_diverged`.
    Everything else falls to the existing `diverged`-vs-`non_diverged` split
    unchanged: a path in `diverged` resolves `_SOURCE_STAGED` (its currently
    staged blob is committed verbatim, never re-read from the worktree); a
    path in `non_diverged` resolves `_SOURCE_WORKTREE` (safe to (re-)stage
    from the worktree).

    `supplied_blobs` has no producer yet -- C2 introduces
    `stage_from_patch()`, the first real caller. Defaults to `{}`, which
    makes this function's output IDENTICAL to the prior two-set partition:
    every path in `diverged` resolves staged-blob, every path in
    `non_diverged` resolves worktree, nothing ever resolves supplied-blob.
    Behaviour-preserving by construction, not merely by test coverage.
    """
    supplied_blobs = supplied_blobs or {}
    resolution: Dict[str, str] = {}
    for p in diverged:
        resolution[p] = _SOURCE_SUPPLIED if p in supplied_blobs else _SOURCE_STAGED
    for p in non_diverged:
        resolution[p] = _SOURCE_SUPPLIED if p in supplied_blobs else _SOURCE_WORKTREE
    for p in supplied_blobs:
        resolution.setdefault(p, _SOURCE_SUPPLIED)
    return resolution


#: Value `_assemble_commit_tree_input` maps a resolved path's mode+sha pair
#: to: `(mode_string, blob_sha)`, e.g. `("100644", "<40-hex-sha>")` -- the
#: same two-tuple shape `_parse_ls_files_cacheinfo`'s cacheinfo entries
#: already carry (minus the path, which is the caller's own dict key here).
_TreeEntry = Tuple[str, str]


def _assemble_commit_tree_input(
    resolution: Dict[str, str],
    *,
    index_snapshot: Dict[str, IndexEntry],
    head_spine: Optional[Dict[str, Dict[str, Tuple[int, str]]]],
    worktree_blobs: Optional[Dict[str, str]] = None,
    supplied_blobs: Optional[Dict[str, str]] = None,
    index_file_absent: bool = False,
    worktree_deleted: Optional[Set[str]] = None,
) -> Tuple[Dict[str, _TreeEntry], Set[str]]:
    """Pure function from `_resolve_content_sources`'s output to
    `{path: (mode, sha)}` plus an explicit ABSENT set -- the tree-input
    assembler for the multi-path, in-process (spine-rewrite) commit arm.
    C8a, docs/plans/2026-08-22-a-commit-is-one-spawn-not-eleven.md.

    NO git spawn, NO `read_index`/`read_tree_spine` call of its own, NO
    blob writing -- every read this function needs is a PARAMETER (R3,
    staff-eng review round 2): `index_snapshot` (the caller's single
    `read_index()` result -- never re-read here, preserving AC11(a)'s
    one-snapshot rule) and `head_spine` (the caller's single
    `read_tree_spine()` result, or `None` if the caller had none to give,
    e.g. an unborn HEAD -- treated as "nothing to fall back to", never a
    refusal on its own). `worktree_blobs`/`supplied_blobs` are `{path: sha}`
    for paths `resolution` resolved to `_SOURCE_WORKTREE`/`_SOURCE_SUPPLIED`
    respectively, each blob already written by the caller (`git hash-object
    -w --stdin-paths` for the worktree case, `stage_from_patch()` for the
    supplied case) BEFORE this function runs -- this function never writes
    a blob, only resolves which sha/mode a path's tree entry should carry.

    Mode precedence, per path, is the same ladder `_resolve_mode_for_paths`
    already implements minus both of its own reads: the REAL index entry
    (from `index_snapshot`) first, the HEAD tree spine (`head_spine`)
    second, `_SUPPLIED_BLOB_MODE` ("100644") last -- reached only for a
    path with no prior entry anywhere (a genuinely new file). A
    `_SOURCE_STAGED` path's mode+sha come verbatim from its `index_snapshot`
    entry -- never re-derived from `head_spine` or a supplied/worktree sha,
    preserving the "commit the deliberately-staged blob as-is" property
    `_resolve_content_sources` exists to protect (AC14).

    Staged deletion (the trap this function exists to close): a
    `_SOURCE_STAGED` path absent from `index_snapshot` is a path staged for
    deletion under the real index. A `read-tree HEAD`-seeded private index
    resurrects such a path (today's arm); a spine rewrite has no implicit
    resurrection, so this function puts that path in the returned ABSENT
    set instead of a `(mode, sha)` entry -- the caller must remove it from
    its parent tree explicitly. Silently omitting a path from BOTH the
    tree-input dict and the ABSENT set would make the deletion vanish with
    no spawn count or `git fsck` symptom to catch it.

    `index_file_absent` -- the staged-deletion inference above is sound
    ONLY when `.git/index` actually exists and simply does not list the
    path. When the index FILE is missing entirely (`read_index` returns an
    empty snapshot with `stat_identity is None` -- its one documented
    empty-result case), every `_SOURCE_STAGED` path is absent from the
    snapshot for a reason that has nothing to do with staging, and reading
    that as "the caller staged a deletion" commits a deletion of every
    path the caller asked to COMMIT -- returning rc=0 with the content
    gone (P1 69ce1cdfd). Under this flag a staged path with no index entry
    therefore falls back to its HEAD spine entry (mode AND sha, verbatim),
    which is what the retired `read-tree HEAD`-seeded private index
    resolved for the same path (DR-272 § 3.4 drift-2). A path with no
    entry in the index or HEAD has no committable content anywhere and
    raises rather than resolving to a deletion by default. `False` (the
    default) reproduces prior behaviour exactly.

    `worktree_deleted` -- paths the CALLER has already established are gone
    from the working tree, which resolve to the ABSENT set unconditionally,
    ahead of every index/spine lookup above. The staged-deletion inference
    two paragraphs up reads a MISSING index entry as the deletion signal;
    that is exactly right when the index is the authority, and exactly wrong
    for a path deleted on disk whose index entry still stands. Such a path
    resolves `_SOURCE_STAGED`, finds its stale entry, and is written back
    into the new tree verbatim -- the caller named it in the pathspec to
    commit its removal and gets it resurrected instead, with no spawn count,
    no rc, and no `git fsck` symptom to catch it.

    Live instance (2026-08-27): `commit_pipeline._run_in_plane_archive_sweep`
    moves terminal handoffs with `os.replace` and touches no index, so the
    archival move committed its destination and kept its source -- the record
    landed at BOTH paths. The alternative fix, staging the removal first,
    costs a `git add` spawn that AC-3 ("the archival contribution adds ZERO
    git processes") forbids; this parameter is what makes the same outcome
    reachable without one.

    ONLY `commit_scoped`'s agree branch supplies it, and only for the paths
    its own `exists()` split already put in the deleted half. The diverged
    branch must NEVER pass it: there, a path's staged content IS the caller's
    intent (`_resolve_content_sources`'s AC14 "commit the deliberately-staged
    blob as-is"), and a worktree that happens to lack the file is not a
    deletion request. `None` (the default) reproduces prior behaviour exactly.

    `mode_only_paths` (AC15's "must not appear in the exclusion report")
    is deliberately NOT a parameter here -- every `mode_only_paths` member
    resolves `_SOURCE_STAGED` like any other staged path and is committed
    identically; the exclusion-report carve-out is a reporting concern at
    the caller, not a tree-input-assembly concern here.

    Path lookups are refuse-on-divergence, exactly like `_index_blobs`
    (AC10): a caller path with no exact `_normalize_path_key` match against
    `index_snapshot`/`head_spine` but a CASEFOLD match is a case-divergent
    key this function cannot silently resolve without risking the same
    "git add reuses a differently-cased index entry" hazard
    `_GIT_PATH_UNRECONCILED` exists to catch (see that sentinel's own
    module-level docstring) -- raises `ValueError` rather than guessing or
    dropping the path.
    """
    worktree_blobs = worktree_blobs or {}
    supplied_blobs = supplied_blobs or {}
    head_spine = head_spine or {}

    index_by_normalized = {_normalize_path_key(p): entry for p, entry in index_snapshot.items()}
    index_casefold_keys = {norm.casefold() for norm in index_by_normalized}

    def _refuse(path: str, where: str) -> None:
        raise ValueError(
            f"_assemble_commit_tree_input: refusing case-divergent path {path!r} -- "
            f"no exact match in {where}, only a case-insensitive one "
            "(see _GIT_PATH_UNRECONCILED's module docstring)"
        )

    def _index_entry(path: str) -> Optional[IndexEntry]:
        norm = _normalize_path_key(path)
        entry = index_by_normalized.get(norm)
        if entry is not None:
            return entry
        if norm.casefold() in index_casefold_keys:
            _refuse(path, "index_snapshot")
        return None

    def _spine_entry(path: str) -> Optional[Tuple[int, str]]:
        norm = _normalize_path_key(path)
        if "/" in norm:
            dirpath, name = norm.rsplit("/", 1)
        else:
            dirpath, name = "", norm
        entries = head_spine.get(dirpath)
        if not entries:
            return None
        entry = entries.get(name)
        if entry is not None:
            return entry
        name_cf = name.casefold()
        if any(k.casefold() == name_cf for k in entries):
            _refuse(path, "head_spine")
        return None

    def _spine_mode(path: str) -> Optional[int]:
        entry = _spine_entry(path)
        return None if entry is None else entry[0]

    def _resolved_mode(path: str, index_entry: Optional[IndexEntry]) -> str:
        if index_entry is not None:
            return f"{index_entry.mode:06o}"
        spine_mode = _spine_mode(path)
        if spine_mode is not None:
            return f"{spine_mode:06o}"
        return _SUPPLIED_BLOB_MODE

    tree_input: Dict[str, _TreeEntry] = {}
    absent: Set[str] = set()

    worktree_deleted = worktree_deleted or set()

    for path, source in resolution.items():
        if source == _SOURCE_STAGED:
            if path in worktree_deleted:
                # Checked BEFORE the index lookup, not after: the whole point
                # is that a surviving index entry must not speak for a path
                # the caller already knows is gone from disk.
                absent.add(path)
                continue
            index_entry = _index_entry(path)
            if index_entry is None:
                if index_file_absent:
                    spine_entry = _spine_entry(path)
                    if spine_entry is None:
                        raise ValueError(
                            "_assemble_commit_tree_input: staged-source path "
                            f"{path!r} has no index entry (the index FILE is "
                            "absent) and no HEAD tree entry either -- refusing "
                            "to resolve it to a deletion, which would commit "
                            "rc=0 with the path the caller asked to commit "
                            "removed from the tree"
                        )
                    spine_mode, spine_sha = spine_entry
                    tree_input[path] = (f"{spine_mode:06o}", spine_sha)
                    continue
                absent.add(path)
                continue
            tree_input[path] = (f"{index_entry.mode:06o}", index_entry.sha)
        elif source == _SOURCE_WORKTREE:
            if path not in worktree_blobs:
                raise ValueError(
                    f"_assemble_commit_tree_input: worktree-source path {path!r} has "
                    "no entry in worktree_blobs -- caller must hash-object it before "
                    "calling this function"
                )
            index_entry = _index_entry(path)
            tree_input[path] = (_resolved_mode(path, index_entry), worktree_blobs[path])
        elif source == _SOURCE_SUPPLIED:
            if path not in supplied_blobs:
                raise ValueError(
                    f"_assemble_commit_tree_input: supplied-source path {path!r} has "
                    "no entry in supplied_blobs"
                )
            index_entry = _index_entry(path)
            tree_input[path] = (_resolved_mode(path, index_entry), supplied_blobs[path])
        else:
            raise ValueError(
                f"_assemble_commit_tree_input: unresolved/unknown source {source!r} "
                f"for path {path!r}"
            )

    return tree_input, absent


def _hash_object_stdin_paths(
    paths: Sequence[str],
    *,
    cwd: Union[str, Path],
    timeout: float = _DEFAULT_TIMEOUT_SECS,
) -> GitResult:
    """`git hash-object -w --stdin-paths` -- write a blob per `path` in
    `paths`, reading each path's CONTENT FROM DISK itself (through the same
    clean-filter machinery a plain `git add -- <path>` would apply), in ONE
    subprocess regardless of how many paths are given (AC13, C8b,
    docs/plans/2026-08-22-a-commit-is-one-spawn-not-eleven.md). Replaces the
    `git add -- <worktree_paths>` fan-in `_commit_scoped_private_index` used
    to issue against its private index for every worktree-sourced path in
    one commit -- this call never touches any index, private or shared, at
    all; it only writes loose blob objects.

    Unlike every other batched wrapper in this module (`_chunk_paths()` and
    its callers), this is NEVER chunked against the Windows argv-length cap
    -- the path LIST travels over stdin, not argv, so there is no cap to
    hit regardless of how many paths are given.

    Routed through `_git()` (unlike `_hash_object_stdin_bytes` above, which
    bypasses it deliberately): the data crossing `_git()`'s `text=True` leg
    here is the PATH LIST itself, not file content, so none of that
    function's raw-bytes/non-ASCII-content hazards apply -- git reads each
    path's actual bytes from disk on its own, off the filesystem, never
    through this call's stdin.

    Returns a `GitResult` whose `stdout`, on success, is one 40-hex sha per
    line, in the SAME ORDER `paths` was given in (git's own `--stdin-paths`
    contract) -- callers zip that order back onto `paths` themselves; this
    wrapper does no parsing of its own; on an empty `paths` this short-
    circuits to an `ok` empty result, spawning no subprocess.
    """
    if not paths:
        return GitResult(returncode=0, stdout="", stderr="")
    stdin_data = "\n".join(paths) + "\n"
    return _git(
        ["hash-object", "-w", "--stdin-paths"],
        cwd=cwd,
        input_data=stdin_data,
        timeout=timeout,
    )


def _hash_worktree_blobs(
    paths: Sequence[str],
    *,
    cwd: Union[str, Path],
    timeout: float = _DEFAULT_TIMEOUT_SECS,
) -> GitResult:
    """Write a blob per worktree-sourced `path`, in process where safe,
    falling back to the ONE-spawn `_hash_object_stdin_paths` ladder for
    every path this in-process check cannot safely reproduce. C3,
    docs/plans/2026-08-26-the-commit-op-stops-asking-git-eleven-times.md:
    adopts `commit_authored_new_file`'s already-shipped in-process blob
    write (`git_objects.write_object` behind a clean-pipeline pre-check)
    into this call site, retiring most `git hash-object -w --stdin-paths`
    spawns without narrowing what this branch can commit.

    Per path, read `cwd/path`'s bytes off disk and apply the SAME two
    base refusals `commit_authored_new_file` carries verbatim (carried,
    not re-derived, per this chunk's brief): a repo-local `filter=`
    attribute pattern matching the path refuses it (`_clean_filter_may_
    apply`, path-scoped, not repo-scoped -- an LFS repo stays deliverable
    for a markdown memo), and an unreadable path (already deleted, now a
    directory, permission error) also refuses, since only `git
    hash-object` itself can report that failure the way every existing
    caller of this branch already expects.

    CR-free content is unconditionally written in-process (a fixed point
    of `text`/`core.autocrlf` clean normalization, so its absence makes
    the eol machinery provably a no-op rather than merely unlikely). CR-
    containing content is handled in-process too, as of C3c
    (docs/plans/2026-08-26-the-commit-op-stops-asking-git-eleven-
    times.md), but only when BOTH hold: `core.autocrlf` resolves to
    exactly `true` (`_repo_autocrlf_true`) and no attributes file pins a
    `text`/`-text`/`eol=` disposition for the path (`_text_attribute_
    pinned`) -- git's default `text=auto` disposition, the only one
    C3c's spike measured against real `git hash-object`. Under those two
    conditions `_autocrlf_checkin_normalize` reproduces git's checkin-side
    CRLF normalization byte-for-byte; either condition failing (autocrlf
    not `true`, or the path has an explicit text/eol/binary pin) refuses
    to the spawn ladder rather than guessing.

    A refused path is never dropped: it is queued into the SAME
    `git hash-object -w --stdin-paths` spawn today's ladder already
    issues, so a refused case commits identically to before this chunk
    -- this is an optimisation with an escape hatch, never a narrowing
    of what `_commit_scoped_private_index` can commit. `write_object`
    writes bytes VERBATIM; the safety argument above is exactly what
    makes that byte-identical to what `git hash-object -w` (which runs
    the clean pipeline) would have written for a refused-free path.

    Returns a `GitResult` shaped like `_hash_object_stdin_paths`'s own
    contract: on success, `stdout` carries one 40-hex sha per line, in
    the SAME ORDER `paths` was given in -- callers zip that order back
    onto `paths` themselves, in-process shas and spawned shas
    interleaved transparently.
    """
    if not paths:
        return GitResult(returncode=0, stdout="", stderr="")

    root = Path(cwd)
    in_process_shas: Dict[str, str] = {}
    refused: List[str] = []
    autocrlf_true: Optional[bool] = None
    common_dir = resolve_git_common_dir(root)

    for path in paths:
        try:
            content = (root / path).read_bytes()
        except OSError:
            refused.append(path)
            continue
        normalized = path.replace("\\", "/")
        if _clean_filter_may_apply(root, normalized) is not None:
            refused.append(path)
            continue
        if b"\r" not in content:
            in_process_shas[path] = write_object(common_dir, b"blob", content)
            continue
        if autocrlf_true is None:
            autocrlf_true = _repo_autocrlf_true(root)
        if autocrlf_true and _text_attribute_pinned(root, normalized) is None:
            in_process_shas[path] = write_object(
                common_dir, b"blob", _autocrlf_checkin_normalize(content)
            )
            continue
        refused.append(path)

    if refused:
        spawn_result = _hash_object_stdin_paths(refused, cwd=root, timeout=timeout)
        if not spawn_result.ok:
            return spawn_result
        shas = spawn_result.stdout.splitlines()
        if len(shas) != len(refused):
            return GitResult(
                returncode=-1,
                stdout="",
                stderr=(
                    "_hash_worktree_blobs: `git hash-object --stdin-paths` "
                    f"returned {len(shas)} sha(s) for {len(refused)} refused "
                    "path(s) -- refusing to guess an alignment"
                ),
            )
        spawned_shas = dict(zip(refused, shas))
    else:
        spawned_shas = {}

    ordered = [in_process_shas.get(p, spawned_shas.get(p, "")) for p in paths]
    return GitResult(returncode=0, stdout="\n".join(ordered) + "\n", stderr="")


def _commit_scoped_private_index(
    diverged: Sequence[str],
    non_diverged: Sequence[str],
    msg_file: Union[str, Path],
    cwd: Union[str, Path],
    deliverable_id: Optional[str] = None,
    *,
    supplied_blobs: Optional[Dict[str, str]] = None,
    attributed_session_id: Optional[str] = None,
    mode_only_paths: Optional[Set[str]] = None,
    worktree_deleted: Optional[Set[str]] = None,
) -> GitResult:
    """The PRIVATE-INDEX branch of `commit_scoped()` -- see that function's
    docstring for when this runs and why. Builds a commit tree under a
    throwaway copy of the index (`GIT_INDEX_FILE` redirected to a uniquely
    named temp file), so the shared index is never mutated, then lands the
    result with a compare-and-swap `update-ref` so a concurrent commit on
    the same branch is never silently orphaned.

    `deliverable_id` (C7a, docs/plans/2026-08-10-a-commit-trailer-that-names-
    the-session.md): when truthy, applied to the `Deliverable-Id:` trailer
    per `commit_scoped`'s own precedence ruling (2) -- see
    `_check_deliverable_id_precedence`. `commit_scoped` has already run the
    AC19 existence/shape guard on this value before calling here; this
    function trusts it and does not re-validate.

    `supplied_blobs` (C1 plumbing, C2's real producer) -- OPTIONAL
    `{path: blob_sha}`, defaulting to `{}` (see `_resolve_content_sources`'s
    own docstring for why an empty map leaves this function's behaviour
    unchanged). Every path resolves to exactly one content source via
    `_resolve_content_sources`, computed ONCE, up front -- the committer
    below consumes that resolution rather than re-deriving the same
    precedence from `diverged`/`non_diverged` set membership at each site
    that needs it.

    `attributed_session_id` (state/bug-backlog/2026-08-18-scoped-git-commit-
    stamps-a-foreign-session-id-8d21f0c4e7b9.yaml) -- OPTIONAL, passed
    straight through to `compute_missing_trailer_args`'s own
    `session_id_override`. `None` (the default) reproduces the prior blind
    env-var resolution exactly. See `commit_scoped`'s own docstring for the
    caller-identity split this closes.

    `worktree_deleted` -- OPTIONAL, forwarded verbatim to
    `_assemble_commit_tree_input` (see its own parameter docstring for the
    resurrection trap it closes) and additionally carved out of the
    `worktree_excluded` report below, since a path with no worktree version
    cannot have had a worktree edit dropped. Supplied only by
    `commit_scoped`'s agree branch, never by its diverged branch.

    `mode_only_paths` (mode-preservation fix, this module's own `_mode_
    delta_paths_chunked()`) -- OPTIONAL, the subset of `diverged` that
    landed here PURELY because their staged mode differs from HEAD's while
    their content is unchanged (never because of a real worktree/index
    content divergence). These paths still resolve `_SOURCE_STAGED` and are
    committed via the same staged-blob tree-input route as any other
    `staged_paths` member -- the ONLY thing this changes is the success
    report: a mode-only path has no excluded WORKTREE edit (worktree and
    staged content already agree for it), so it must never appear in the
    `"worktree edits ... were NOT included"` message or `GitResult.
    worktree_excluded`, or every publish round carrying a re-moded path
    would emit a false exclusion warning. `None` (the default) reproduces
    prior behaviour exactly -- every `staged_paths` member is reported,
    same as before this parameter existed.

    C8b rewire (docs/plans/2026-08-22-a-commit-is-one-spawn-not-eleven.md):
    this no longer builds a throwaway private index at all -- `git
    read-tree`/`git add`/the per-path `git update-index --cacheinfo`
    fan-out/`git write-tree`/`git commit-tree`/`git update-ref` are all
    gone. The whole tree is now assembled IN PROCESS: `read_index()` (C2,
    spawn-free) supplies the one real-index snapshot AC11(a) requires,
    `_hash_worktree_blobs()` writes every worktree-sourced blob (C3,
    docs/plans/2026-08-26-the-commit-op-stops-asking-git-eleven-times.md:
    in process, via `write_object`, for every path a clean-pipeline
    pre-check clears; only a refused path falls back to the ONE
    `git hash-object -w --stdin-paths` spawn this call site used to
    issue unconditionally regardless of pathspec length, AC13),
    `_assemble_commit_tree_input`
    (C8a) resolves each path's `(mode, sha)` (or ABSENT, for a staged
    deletion) off that snapshot plus the HEAD tree spine, and
    `_commit_via_head_spine` (C4) rewrites HEAD's tree spine, builds the
    commit object, and lands it via a locked `cas_ref` CAS -- zero further
    git spawns. `_resolve_mode_for_paths()` is retired from this call site
    (its own `read_index`/`ls-tree` reads must not survive the rewire, or
    AC11(a)'s one-snapshot rule silently breaks on any pathspec containing a
    new file) -- the function itself is untouched, still covered by its own
    tests elsewhere, simply no longer called from here.
    """
    root = Path(cwd)
    supplied_blobs = supplied_blobs or {}
    resolution = _resolve_content_sources(diverged, non_diverged, supplied_blobs)
    staged_paths = [p for p in diverged if resolution[p] == _SOURCE_STAGED]
    worktree_paths = [p for p in non_diverged if resolution[p] == _SOURCE_WORKTREE]

    # `old_head` is `None` on an unborn branch (a fresh `git init`, no
    # commits yet -- `_git_state_head_sha` returns `None` for it, same as a
    # genuinely unresolvable symref). This branch used to refuse loud here,
    # unconditionally, on the theory that "no HEAD" always means "nothing
    # to read the parent/spine from" -- that theory is wrong for the unborn
    # case specifically: there IS a well-defined answer (a root commit, no
    # `-p` parent, CAS-create against an absent ref), it just is not the
    # SAME answer as the ordinary case. `None` flows through below instead:
    # `_commit_via_head_spine`'s own precondition (`root_tree_sha is None`)
    # already bails to the ladder for it (see that helper's own docstring),
    # and the ladder below branches explicitly on `old_head is None` at
    # every step that needs a parent (`read-tree HEAD`, `commit-tree -p`,
    # the no-op comparison, `update-ref`'s CAS old-value). A genuinely
    # unresolvable symref (packed-only, corrupt) still reaches a real git
    # failure further down (`update-ref`/`commit-tree` refuse it loud on
    # their own), never a silent wrong commit.
    old_head = _git_state_head_sha(root)

    # AC11(a): the ONE real-index snapshot this whole commit is built from,
    # read fresh, in process, no git spawn -- never re-read below.
    index_snapshot = read_index(root)

    # AC13: ONE `git hash-object -w --stdin-paths` spawn covers every
    # worktree-sourced path in this commit, regardless of pathspec length --
    # replaces the `git add -- <worktree_paths>` fan-in this branch's
    # private index used to take.
    worktree_blobs: Dict[str, str] = {}
    if worktree_paths:
        hash_result = _hash_worktree_blobs(worktree_paths, cwd=root)
        if not hash_result.ok:
            return hash_result
        shas = hash_result.stdout.splitlines()
        if len(shas) != len(worktree_paths):
            return GitResult(
                returncode=-1,
                stdout="",
                stderr=(
                    "_commit_scoped_private_index: `git hash-object --stdin-"
                    f"paths` returned {len(shas)} sha(s) for {len(worktree_paths)} "
                    "requested path(s) -- refusing to guess an alignment"
                ),
            )
        worktree_blobs = dict(zip(worktree_paths, shas))

    # Directory spine off HEAD's tree, scoped to every path this commit
    # touches -- feeds `_assemble_commit_tree_input`'s own mode-precedence
    # fallback (index -> HEAD spine -> `_SUPPLIED_BLOB_MODE`). `None` when
    # unresolvable is passed straight through -- "nothing to fall back to",
    # never a refusal on its own (see that function's own docstring).
    head_spine = read_tree_spine(root, list(resolution.keys()))

    try:
        tree_input, absent = _assemble_commit_tree_input(
            resolution,
            index_snapshot=index_snapshot,
            head_spine=head_spine,
            worktree_blobs=worktree_blobs,
            supplied_blobs=supplied_blobs,
            index_file_absent=index_snapshot.stat_identity is None,
            worktree_deleted=worktree_deleted,
        )
    except ValueError as exc:
        return GitResult(
            returncode=-1,
            stdout="",
            stderr=f"_commit_scoped_private_index: {exc}",
        )

    assembled: Dict[str, Union[Tuple[int, str], object]] = {
        path: (int(mode, 8), sha) for path, (mode, sha) in tree_input.items()
    }
    for path in absent:
        assembled[path] = _ABSENT

    # `commit-tree` is plumbing -- it runs NO git hooks, so the
    # `prepare-commit-msg` hook that stamps Session-Id/Deliverable-Id on
    # every ordinary `git commit` never fires here. Replay its resolution
    # logic explicitly so a commit landed via this branch carries identical
    # trailers to one landed via the agree branch (AC18,
    # docs/plans/2026-07-27-computed-commit-mechanism-selection.md chunk
    # C10-remainder). Mutates `msg_file` in place, BEFORE it is read for the
    # commit object body below, exactly mirroring what the hook would have
    # done to the same file had `git commit` fired normally.
    trailer_args = compute_missing_trailer_args(
        msg_file, root, paths=[*diverged, *non_diverged],
        session_id_override=attributed_session_id,
    )
    # C7a: an explicit caller `deliverable_id` folds into the SAME
    # `interpret-trailers` call above, mirroring `commit_authored_
    # content`'s `_drop_trailer_arg`-then-append shape -- EXCEPT for
    # precedence against a pre-existing message trailer, which does NOT
    # mirror that sibling (its message-first rule is silent about the
    # THIRD source `commit_anchors.py` may have already stamped here; see
    # `commit_scoped`'s own docstring and PM ruling (2)). May raise
    # `DeliverableIdAssertionConflictError` -- see `_check_deliverable_id_
    # precedence`'s own docstring; deliberately uncaught here. Gated on the
    # caller-supplied `deliverable_id` parameter, never on a tier-0-resolved
    # value, so this raise site is opt-in (staff-eng review finding 4).
    if deliverable_id:
        msg_text_before = Path(msg_file).read_text(encoding="utf-8")
        if _check_deliverable_id_precedence(msg_text_before, deliverable_id):
            trailer_args = _drop_trailer_arg(trailer_args, "Deliverable-Id")
            trailer_args = trailer_args + ["--trailer", f"Deliverable-Id: {deliverable_id}"]
    interpret_result = _apply_trailers(msg_file, trailer_args, root)
    if interpret_result is not None:
        return interpret_result

    # Fast path: rewrite HEAD's tree spine in process, build the commit
    # object in process, land it via a locked ref CAS -- zero further git
    # spawns for this leg. `index_stat_identity` is passed (unlike
    # `commit_authored_content`'s single-path caller, which reads no index
    # at all): this branch's tree input is partly built from
    # `index_snapshot` above, so a peer mutating the shared index between
    # that snapshot and this CAS must be caught, never silently committed
    # past. `_commit_via_head_spine` returns `None` -- take the ladder,
    # below, unchanged, no side effect surviving that decision -- whenever
    # a changed path's PARENT DIRECTORY does not already exist in HEAD's
    # tree (a brand-new file under a brand-new subdirectory): a spine
    # rewrite can only re-point existing directory levels, it cannot
    # synthesize a new one that `write-tree` would otherwise happily create
    # from a freshly-populated index.
    # `refuse_noop` gated on the shared index being genuinely PRESENT (P1
    # 69ce1cdfd's own distinguishing signal -- see `substitute`, below,
    # for the same test): when `index_snapshot.stat_identity is None` (the
    # `.git/index` file itself is missing), every `_SOURCE_STAGED` path
    # resolves off HEAD by design (`_assemble_commit_tree_input`'s own
    # `index_file_absent` arm) -- the resulting tree is BYTE-IDENTICAL to
    # HEAD's on purpose, as the safety net against P1 69ce1cdfd's damage
    # class (a vanished index silently reading every staged path as a
    # deletion). That intentional no-op must still land and report the
    # HEAD substitution (`test_absent_shared_index_never_deletes_the_
    # scoped_path_it_was_asked_to_commit`) -- refusing it here would
    # re-open the exact "committing nothing when something was actually
    # asked for" confusion this parameter exists to prevent for the
    # ORDINARY (index-present) no-op case, applied to the wrong case.
    # C6b (docs/plans/2026-08-27-the-commit-op-resolves-one-pass-context.md):
    # `create_missing_dirs=True` -- the ONE precondition-miss shape this
    # branch actually hits at nonzero rate, a brand-new file under a
    # brand-new subdirectory (`_rewrite_head_spine` cannot re-point into a
    # spine that does not carry the new directory at all) -- now routes
    # through `_synthesize_absent_spine_dirs` instead of falling to the
    # ladder below, so that shape reaches AC1's 0-spawn target too. See
    # `_synthesize_absent_spine_dirs`'s own docstring for exactly what it
    # fills in and refuses.
    #
    # The ladder itself (`read-tree HEAD` / per-path `update-index
    # --cacheinfo` / `git rm --pathspec-from-file` / `write-tree` /
    # `commit-tree` / `update-ref`) is DELIBERATELY KEPT, not deleted as
    # this chunk's own dispatch brief originally specified -- the brief's
    # "0 spawns, not 1" instruction did not have this evidence:
    # `test_agree_branch_commits_correctly_immediately_after_pack_refs`
    # (this module's own suite) proves `_resolve_cas_ref_target` genuinely
    # refuses after `git pack-refs --all` (no loose ref file for HEAD's
    # branch until the next `git gc`/ref update touches it), a real,
    # already-tested repo state on ANY worktree that has ever run
    # maintenance, not a hypothetical. Deleting the ladder made that test
    # fail loud instead of falling back and landing the commit correctly --
    # a regression on a preserved invariant this chunk's brief also binds
    # ("An implementation that reaches 0 spawns ... has broken the only
    # thing this branch is for"). Reported as a brief divergence, not
    # silently reconciled -- see this chunk's own run-report sidecar.
    fast_result = _commit_via_head_spine(
        root, assembled, old_head, msg_file,
        index_stat_identity=index_snapshot.stat_identity,
        caller="_commit_scoped_private_index",
        refuse_noop=index_snapshot.stat_identity is not None,
        create_missing_dirs=True,
    )
    if fast_result is not None:
        if not fast_result.ok:
            return fast_result
        new_sha = fast_result.stdout.strip()
    else:
        # ---- ladder: a private index, populated from `tree_input`/
        # `absent` (never re-derived from `ls-files`/`_resolve_mode_for_
        # paths`) ------------------------------------------------------
        temp_index = Path(tempfile.gettempdir()) / f"git-index-{os.getpid()}-{uuid.uuid4().hex}"
        try:
            private_env: Dict[str, str] = dict(os.environ)
            private_env["GIT_INDEX_FILE"] = str(temp_index)

            # Seed the private index straight from HEAD into this FRESH temp
            # index, never via `shutil.copy2` of the shared `.git/index`
            # (DR-272 § 3.4 drift 2) -- same idiom `commit_authored_content`'s
            # own ladder and the pre-rewire private index both used.
            #
            # `old_head is None` -- unborn branch, no HEAD to seed from.
            # `read-tree HEAD` would itself fail loud on an unresolvable
            # HEAD; skip it and let the temp index start genuinely empty
            # (never created on disk until the cacheinfo writes below --
            # `_empty_private_index_refusal`'s own docstring already
            # establishes a MISSING `GIT_INDEX_FILE` is a valid empty index
            # to `write-tree`, not an error).
            if old_head is not None:
                read_tree_result = _git(["read-tree", "HEAD"], cwd=root, env=private_env)
                if not read_tree_result.ok:
                    return read_tree_result

            # Every resolved path lands via an explicit cacheinfo entry --
            # never from the worktree (`git add`) -- so a brand-new
            # subdirectory materializes in the private index exactly like
            # `write-tree` needs, with no dependency on that directory
            # already existing anywhere.
            for path, (mode, sha) in tree_input.items():
                # `mode` here is already the "100644"-shaped STRING
                # `_assemble_commit_tree_input` returns (never the int form
                # `assembled`, above, converts to for the fast path) --
                # `update-index --cacheinfo` takes it verbatim.
                cacheinfo_result = _git(
                    ["update-index", "--add", "--cacheinfo", f"{mode},{sha},{path}"],
                    cwd=root,
                    env=private_env,
                )
                if not cacheinfo_result.ok:
                    return cacheinfo_result

            # `absent` (a staged deletion `_assemble_commit_tree_input`
            # could not resolve against `index_snapshot`) must be removed
            # explicitly -- `read-tree HEAD` above resurrects it into this
            # private index by construction, and this branch never wants
            # that resurrection (see `_assemble_commit_tree_input`'s own
            # docstring on the staged-deletion trap it exists to close).
            # ONE spawn for the whole set, never per-path -- and the pathspec
            # rides a FILE, never argv. This is the SIXTH argv-length site on
            # this commit path, missed by the sweep `add_paths_pathspec_file`'s
            # docstring calls "the last of the five": `absent` is unbounded,
            # and one real publish round to the klabauter mirror carried 4045
            # of them / 333,668 argv characters against Windows' 32767-char
            # CreateProcess cap. It dies as `[WinError 206] The filename or
            # extension is too long`, which reaches the operator as a bare
            # commit-failure -- `_git()` converts OSError to a returncode=-1
            # GitResult whose stderr is the COMMAND, since git never ran and
            # produced no message of its own. Every publish round to that
            # mirror failed this way until those paths were cleared.
            #
            # `--pathspec-from-file=<f>` rather than chunked argv batches, for
            # the same reason `add_paths_pathspec_file` and
            # `commit_with_message_file_pathspec_scoped` use it and
            # `_diverging_paths_chunked` cannot: `git rm` accepts the flag
            # (empirically verified against this machine's git 2.55.0.windows.4,
            # `git rm -h`), so one call still covers the whole set and the
            # one-spawn promise above survives intact. Newline-delimited via
            # the module's own `_write_pathspec_file`, on its stated premise
            # that no path this module commits carries a literal newline.
            if absent:
                pathspec_file = _write_pathspec_file(root, sorted(absent))
                try:
                    rm_result = _git(
                        [
                            "rm",
                            "--cached",
                            "-q",
                            f"--pathspec-from-file={pathspec_file}",
                            "--pathspec-file-nul",
                        ],
                        cwd=root,
                        env=private_env,
                    )
                finally:
                    pathspec_file.unlink(missing_ok=True)
                if not rm_result.ok:
                    return rm_result

            write_tree_result = _git(["write-tree"], cwd=root, env=private_env)
            if not write_tree_result.ok:
                return write_tree_result
            tree_sha = write_tree_result.stdout.strip()
            empty_tree_refusal = _empty_private_index_refusal(
                tree_sha, root=root, caller="_commit_scoped_private_index"
            )
            if empty_tree_refusal is not None:
                return empty_tree_refusal

            # No-op refusal (DEFECT 1 fix, mirrors the fast path's own
            # `refuse_noop` check in `_commit_via_head_spine`): a byte-
            # identical re-commit computes the SAME tree HEAD already
            # points at -- landing it would create a phantom commit with
            # no real content change, exactly the "nothing to commit"
            # no-op `git commit` itself refused for free pre-C3. Only
            # meaningful with a real parent to compare against -- an
            # unborn branch's first commit has no HEAD tree to diff. Also
            # gated on the shared index being genuinely present (see the
            # matching comment on this function's own fast-path call site,
            # above) -- the absent-index HEAD-fallback safety net
            # (P1 69ce1cdfd) intentionally produces this same byte-
            # identical tree and must still land.
            if old_head is not None and index_snapshot.stat_identity is not None:
                parent_tree_sha = _git_state_head_tree_sha(root)
                if parent_tree_sha is not None and tree_sha == parent_tree_sha:
                    return GitResult(returncode=1, stdout="", stderr="")

            msg_text = Path(msg_file).read_text(encoding="utf-8")
            subject_lines = msg_text.splitlines()
            subject = subject_lines[0] if subject_lines else "commit"

            commit_tree_args = ["commit-tree", tree_sha]
            if old_head is not None:
                commit_tree_args += ["-p", old_head]
            commit_tree_args += ["-F", str(msg_file)]
            commit_tree_result = _git(
                commit_tree_args,
                cwd=root,
                env=private_env,
            )
            if not commit_tree_result.ok:
                return commit_tree_result
            new_sha = commit_tree_result.stdout.strip()

            # Compare-and-swap landing -- the 4-argument form fails loud if
            # HEAD moved since `old_head` was captured, rather than silently
            # orphaning a peer commit that landed in the window. NOT
            # env-scoped: `update-ref` moves the real branch ref, which
            # lives in the shared git-dir regardless of which index built
            # the tree being pointed at.
            #
            # `old_head is None` -- unborn branch: git's own CAS convention
            # for "must not already exist" is the EMPTY STRING old-value
            # (never a bare 3-arg omission, which drops the CAS guarantee
            # entirely) -- a concurrent peer racing to create the same
            # branch's root commit still fails loud here rather than
            # silently orphaning it.
            old_head_arg = old_head if old_head is not None else ""
            update_ref_result = _git(
                ["update-ref", "-m", subject, "HEAD", new_sha, old_head_arg],
                cwd=root,
            )
            if not update_ref_result.ok:
                return GitResult(
                    returncode=update_ref_result.returncode,
                    stdout=update_ref_result.stdout,
                    stderr=(
                        "commit_scoped: compare-and-swap failed -- HEAD moved "
                        f"concurrently since {old_head} was captured; refusing "
                        "to retry silently (a retry needs a tree rebuilt "
                        f"against the new HEAD). {update_ref_result.stderr}"
                    ),
                )
        finally:
            temp_index.unlink(missing_ok=True)

    # `staged_paths` is exactly the resolved staged-blob subset of
    # `diverged` -- `diverged` is the set that had unstaged working-tree
    # modifications (`commit_scoped()` only reaches this function when
    # `diverging_paths` returned a non-empty answer, and passes that exact
    # answer through unmodified), and `_resolve_content_sources` carves out
    # any path ALSO present in `supplied_blobs` before it ever reaches
    # `staged_paths` -- so no separate worktree-vs-staged recomputation is
    # needed here; reusing the resolution's own answer is that answer (see
    # the P1 bug backlog entry cited on `GitResult.worktree_excluded` for
    # the incident this closes: this field was previously not populated at
    # all, so a caller had no way to learn its worktree edits were
    # excluded). With `supplied_blobs` empty (this chunk's only exercised
    # shape), `staged_paths == diverged` exactly, so this is byte-identical
    # to the prior behaviour.
    #
    # `mode_only_paths` are excluded from the reported set here (never from
    # `tree_input`/`staged_paths` above, which still commit them via the
    # same staged-blob route) -- a mode-only path has no excluded WORKTREE
    # edit (worktree and staged content already agree for it; only the MODE
    # differs from HEAD), so reporting it as "worktree edits ... were NOT
    # included" would be a false exclusion warning on every ordinary
    # re-mode commit. See `mode_only_paths`' own docstring parameter above.
    #
    # `worktree_deleted` is carved out for the same reason, one step further:
    # such a path has no worktree version at all, so "your worktree edits were
    # dropped in favour of the staged version" is not merely noisy but false
    # in both halves -- nothing was substituted, the path was REMOVED, which
    # is what the caller asked for. Reporting it would make every archival
    # move warn about the deletion it just performed successfully.
    excluded_paths = [
        p for p in staged_paths
        if p not in (mode_only_paths or set()) and p not in (worktree_deleted or set())
    ]
    # Which version replaced the excluded worktree edit is not always the
    # staged one: with the index FILE absent there was no staged version to
    # commit, and `_assemble_commit_tree_input`'s `index_file_absent` arm
    # resolved these paths off HEAD instead. Naming the index there states
    # the opposite of what happened, and reads as reassurance (P1
    # 69ce1cdfd, item 3).
    substitute = "HEAD" if index_snapshot.stat_identity is None else "staged (index)"
    return GitResult(
        returncode=0,
        stdout=new_sha,
        stderr=(
            (
                "commit_scoped: worktree edits to %s were NOT included -- "
                "the %s version was committed instead (private-"
                "index branch; see GitResult.worktree_excluded)"
                % (", ".join(excluded_paths), substitute)
            )
            if excluded_paths
            else ""
        ),
        worktree_excluded=tuple(excluded_paths),
    )


    # Claim-release classification (C3, docs/plans/2026-08-11-claim-release-
    # and-the-gate-that-cannot-clear.md): NOT instrumented here, deliberately.
    # `commit_scoped()` has THREE production callers, not one:
    # `commit_pipeline.commit()` (via `scoped_git_commit.py::_handler`,
    # already the reference release site — releasing again here would
    # double the `git status --porcelain` spawn for that path), plus TWO
    # direct callers this chunk's file list does not own
    # (`ops/ceremony/post_commit_tail.py`, `ops/ceremony/consumed_handoff_
    # stamp.py`). Those two callers are genuinely uninstrumented today and
    # a real gap this chunk did not close — flagged as a finding rather than
    # fixed here, for two reasons: (1) file-scope (neither is in this
    # executor's in-scope list), and (2) `commit_scoped()` itself has no
    # notion of "the committing session's own sid" the way its callers do —
    # `scoped_git_commit.py` resolves it via `_resolve_committing_session_id`
    # (params-aware, not a bare env-var read), which may legitimately differ
    # from a blind `session_core.resolve_session_id(cwd)` call made from
    # inside this shared helper. Wiring a release call in here risks
    # attributing a release to the wrong sid for whichever caller's
    # resolution semantics differ — exactly the self/other boundary this
    # plan's own hard constraints forbid guessing at. Report this to the EM
    # as an open C3 gap for `post_commit_tail.py`/`consumed_handoff_stamp.py`
    # to pick up as their own dispatch, each resolving its own committing
    # sid the way its own call site already knows how to.
def _within_one_index_read(func):
    """Serve every non-`fresh` `read_index()` inside one `func` call from the
    first such read, via `git_state.index_read_cache_scope()`.

    `.git/index` is 5.2MB in this repo and `read_index` parses it in pure
    Python; `commit_scoped` read it four separate times per call
    (`_index_blobs`, `_v2_state_records_chunked`, `_commit_scoped_private_
    index`, plus `git_index.scoped_status`'s own scoped parse), which is
    ~250ms of the ~300ms `memo.send` was measured spending -- for three
    paths. Nothing between those reads mutates the shared index: both
    branches land through `_commit_scoped_private_index`, which builds its
    tree under a redirected `GIT_INDEX_FILE` and never writes the shared
    one, and the single read that must observe a peer's write at a precise
    instant -- `_agree_branch_cas_refusal`'s re-observation -- already asks
    for `fresh=True`, which the scope is defined to bypass.

    Negative-spec: the scope opens and closes with ONE call. It is not a
    process-lifetime cache and never spans two commits -- a second
    `commit_scoped` reads the index afresh, exactly as before.
    """
    @functools.wraps(func)
    def _wrapper(*args, **kwargs):
        with index_read_cache_scope():
            return func(*args, **kwargs)

    return _wrapper


@_within_one_index_read
def commit_scoped(
    paths: Sequence[str],
    msg_file: Union[str, Path],
    cwd: Union[str, Path],
    *,
    known_checked: Optional[Set[str]] = None,
    known_diverged: Optional[Set[str]] = None,
    deliverable_id: Optional[str] = None,
    supplied_blobs: Optional[Dict[str, str]] = None,
    suppress_post_commit_auto_push: bool = False,
    attributed_session_id: Optional[str] = None,
) -> GitResult:
    """Commit exactly `paths`, choosing the safe mechanism from OBSERVED
    index/worktree state -- the computed replacement for hand-picking
    between `git commit -- <paths>` and a bare `git commit` on a shared
    working tree (see the module-section docstring above `commit_scoped`
    for the two incidents this closes).

    `attributed_session_id` (state/bug-backlog/2026-08-18-scoped-git-commit-
    stamps-a-foreign-session-id-8d21f0c4e7b9.yaml) -- OPTIONAL, the
    CALLER's own already-resolved committing-session identity (e.g.
    `scoped_git_commit.py`'s `_resolve_committing_session_id`, params-aware,
    not a bare env-var read), threaded straight through to both branches'
    `compute_missing_trailer_args` call as `session_id_override`. `None`
    (the default) leaves every existing caller's `Session-Id:` resolution
    exactly as it was -- a blind `session_core.resolve_session_id()` read of
    THIS PROCESS's own env, which the module-section comment above already
    names as able to "legitimately differ" from a caller's own resolved
    identity. This does not, by itself, close the note above about
    claim-release classification (a different mechanism) -- it only makes
    the `Session-Id:` trailer track the caller's identity rather than a
    second, independent guess at it.

    Behaviour:
      1. `paths` empty -> FAILS LOUD (`GitResult.ok is False`). Never falls
         through to `git ... -- ` with zero paths -- that form commits the
         WHOLE INDEX, not nothing (verified empirically 2026-07-27).
      2. Any entry of `paths` that is currently a DIRECTORY -> FAILS LOUD.
         A directory pathspec matches whatever lands inside it AT COMMIT
         TIME, including a peer's file added after the caller computed
         `paths` -- the same partial-blanket-add hazard `git add -A` is
         banned for. Callers must pass explicit file paths.
      3. Determines the diverged subset of `paths`, either freshly via
         `diverging_paths(paths, cwd, fail_loud=True)` (shared with Check 13
         / SC-DR-015 -- not re-derived here) or, when `known_checked`/
         `known_diverged` are supplied, by TRUSTING the caller's answer for
         every path already in `known_checked` and freshly checking only the
         "gap" -- paths in `paths` the caller never vetted (see
         `known_checked`/`known_diverged` below). Either way the divergence
         set that drives step 3's branch is fully accurate for every path in
         `paths`, not a partial guess.

         `diverging_paths()` is called with `fail_loud=True` here (never the
         default `fail_loud=False` Check 13 uses) -- a `git diff` failure or
         timeout FAILS LOUD (`GitResult.ok is False`) rather than being
         treated as "no divergence found". This function picks the commit
         MECHANISM from the answer, so an indeterminate result must never be
         silently read as "clean" -- see `DivergenceCheckFailed` in
         `coordinator_core.git.divergence` for the incident this closes.

         The divergence set is then WIDENED (still before either branch
         runs) by `_mode_delta_paths_chunked()`, this module's own -- any
         path whose STAGED mode differs from HEAD's while its content is
         unchanged (the shape `git update-index --chmod=+x` produces under
         `core.fileMode=false`) is added to `diverged`, even though its
         worktree content already agrees with the index and `diverging_
         paths()` alone would answer "clean" for it. Without this widening
         such a path would take the AGREE branch below, whose path-
         restricted `git commit --pathspec-from-file=...` silently discards
         a staged-only mode delta under `core.fileMode=false` (DR-151 -- see
         `commit_with_message_file_pathspec_scoped`'s own docstring). Same
         fail-loud posture on an indeterminate read; same chunked seam, no
         new per-path spawn.
         - No divergence -> AGREE branch: FIRST re-verified by an
           intra-invocation compare-and-swap (`_agree_branch_cas_refusal`,
           Layer 1, state/audits/2026-08-14-scoped-commit-partial-stage-
           sweep.md) against the index/HEAD blob snapshot taken above
           BEFORE `diverging_paths()` ran -- refuses loud rather than
           proceeding if a peer's own commit/stage moved either signal in
           this call's own check-then-act window (the S5 incident shape:
           `diverging_paths()` answers "not diverged" correctly, because a
           peer already absorbed this call's own staged content into HEAD
           moments earlier). Only once the CAS passes does `git add --
           paths` then `git commit -F msg_file -- paths` run. Retains
           SC-DR-008's race protection across the stage->commit window; this
           is the overwhelming-majority path and stays this cheap. This
           branch mutates `msg_file` in place before `git add`/`git commit`
           run, UNCONDITIONALLY -- `compute_missing_trailer_args` +
           `_apply_trailers` are called on every agree-branch commit, not
           only when a caller passes `deliverable_id` (that parameter only
           FOLDS AN EXPLICIT VALUE INTO the already-computed `trailer_args`;
           see the call site's own comment). Prior to C7a (docs/plans/
           2026-08-10-a-commit-trailer-that-names-the-session.md) this branch
           never opened `msg_file` at all and relied entirely on the
           `prepare-commit-msg` hook; AC18 (2026-08-14, cross-repo memo
           `2026-08-14-doe-claude-em-scoped-git-commit-drops-session-id-
           trailer.md`) ended that reliance after a hook non-fire landed a
           commit with no `Session-Id:` at all, silently.

           STALE-PROSE CORRECTION, 2026-08-25: this paragraph described the
           C7a state ("when `deliverable_id` is truthy") for eleven days
           after AC18 made the call unconditional, and a staff-eng reviewer
           read it and concluded the hook is what attaches trailers on this
           branch -- the exact inverted belief AC18 exists to refute. The
           consequence is not academic: a plan was nearly rewritten to keep
           a hook alive that the engine no longer needs. Whoever changes the
           call site changes this paragraph in the same commit.
         - Divergence -> PRIVATE-INDEX branch (`_commit_scoped_private_
           index`): builds the commit tree under a throwaway index copy so
           the shared index is never touched, preserves each diverged
           path's CURRENTLY STAGED content verbatim (never re-read from the
           worktree), and lands via a compare-and-swap `update-ref` that
           fails loud rather than silently orphaning a concurrent commit.

    `deliverable_id` (C7a) -- OPTIONAL, a caller who already knows which
    deliverable this commit belongs to (e.g. an execute-plan chunk-commit
    path holding the plan's own `deliverable_id` frontmatter) should not be
    second-guessed by the session/claimed-plan inference `compute_missing_
    trailer_args` otherwise falls back to. Validated (AC19) BEFORE either
    branch runs -- see `_validate_explicit_deliverable_id` -- then applied
    on WHICHEVER branch this call takes, per the precedence ruling in
    `_check_deliverable_id_precedence`: an explicit value wins when the
    message carries no `Deliverable-Id:` trailer yet; is a no-op when one
    already agrees; and raises `DeliverableIdAssertionConflictError`
    (uncaught, a deliberate exception to this function's own "always
    returns a GitResult" contract -- see that ruling's own docstring, and
    `DeliverableIdAssertionConflictError`'s own docstring for why this is
    a sibling of `DivergentDeliverableIdError`, not that class itself) when
    one disagrees. Advisory-only half (not enforced here, prose guidance only):
    an explicit `deliverable_id` should be PROVENANCE-BEARING -- sourced
    from the plan the caller is ACTUALLY EXECUTING AGAINST, never invented
    or defaulted to satisfy this parameter. Whether the resolvable id is
    truly the plan THIS commit's own work belongs to is genuinely
    uncheckable at this seam; this function does not and cannot enforce
    that half, only that the value is well-shaped and resolves to SOME real
    artifact (AC19's enforceable half).

    `supplied_blobs` (C3, docs/plans/2026-08-14-the-tool-stages-what-it-
    commits.md) -- OPTIONAL `{path: blob_sha}`, defaulting to `{}`/`None`
    (behaviour-preserving: an empty map reproduces the prior two-branch
    selection exactly). When non-empty, ANY path present resolves to
    `_SOURCE_SUPPLIED` (see `_resolve_content_sources`) and this call NEVER
    takes the agree branch, even when `diverged` is empty -- a supplied
    path's provenanced blob must never be exposed to the agree branch's own
    `git add -- path_list`, which reads the SHARED worktree and would
    silently replace it with whatever a peer's ordinary edit left on disk.
    Threaded straight through to `_commit_scoped_private_index`, which
    already resolves `supplied_blobs` ahead of both `diverged`/`non_diverged`
    membership (C1/C2). `_agree_branch_cas_refusal` (the existing index/HEAD
    compare-and-swap) still runs on its own conditions for every OTHER call
    to this function -- a supplied blob answers "whose hunks", not "did the
    world move", and does not exempt any path from that CAS.

    `known_checked`/`known_diverged` -- OPTIONAL dedup seam (docs/plans/
    2026-07-27-computed-commit-mechanism-selection.md § dedup). When both
    are given, `known_checked` is the exact path set a caller already ran
    `diverging_paths()` over, and `known_diverged` is the diverged subset of
    it. This function trusts that answer for `path in known_checked` and
    only spawns a fresh (pathspec-scoped) `diverging_paths()` call for the
    "gap" -- paths in `path_list` NOT in `known_checked` (e.g. a
    swept-rename destination discovered but never passed through the
    caller's own `diverging_paths()` pathspec). In the common case (no
    swept renames, `paths == known_checked`) the gap is empty and this
    function spawns ZERO `git diff` subprocesses of its own. As of
    docs/plans/2026-08-07-excise-the-ceremony-lock.md § C10,
    `commit_pipeline.commit()` no longer passes this pair at all -- there is
    no `ceremony_lock` left to bound its soundness -- so every caller today
    (`commit_pipeline.commit()` included) hits the fresh-computation path;
    the parameters remain on this function for any FUTURE caller that can
    supply a genuinely current answer under the precondition below, never as
    a general-purpose cache.

    Precondition (corrected 2026-08-07 -- there is no lock): sound ONLY when
    NOTHING can have changed the worktree/index between the caller's own
    `diverging_paths()` call and THIS call -- i.e. no intervening steps of
    any kind (gate checks, spawns, other I/O) sit between the two. It is NOT
    "same lock hold" (no caller holds one), and it is never a cache -- a
    `known_checked`/`known_diverged` pair computed even one step earlier,
    let alone across a pass or across concurrent invocations, is answering a
    question that may no longer be live.

    Returns a `GitResult`; on success (`ok is True`) `stdout` carries the
    new commit SHA in the private-index branch (the agree branch's `stdout`
    is whatever `git commit` printed, matching `commit_with_message_file`'s
    existing contract -- callers needing the SHA there already call
    `rev_parse_head()`, unchanged).

    This function requires NO external lock -- confirmed safe (code review
    2026-07-27 structural note), and lock-free is now the only way it is
    ever called (corrected 2026-08-07,
    docs/plans/2026-08-07-excise-the-ceremony-lock.md § C6: the
    `ceremony_lock` mutex was deleted outright by that plan's § C7, so no
    caller can hold one even in principle). Neither
    `wsc_tail`/`commit_pipeline`'s call path nor `coordinator-safe-commit`'s
    `do_scoped` takes any lock, and both are fine BY CONSTRUCTION, for two
    different reasons depending on which branch a given call takes:
      - AGREE branch: safety against absorbing a peer's own staged content
        outside `path_list` comes from the explicit trailing pathspec on
        both `git add` and `git commit` (pre-existing SC-DR-008 protection,
        unrelated to this function or any lock); safety against two
        concurrent writers corrupting the SAME index comes from git's own
        `index.lock` mutual exclusion (a losing racer gets a raw git
        failure, not corruption) -- neither needs `ceremony_lock`.
      - DIVERGED (private-index) branch: never touches the shared index at
        all, and lands via the 4-argument compare-and-swap `update-ref` --
        a losing racer's CAS fails and returns a clear `GitResult` failure
        instead of silently orphaning the peer's commit (proved end-to-end
        against a REAL concurrent commit by
        `test_cas_failure_on_concurrent_head_move_fails_loud`). The CAS
        itself is the concurrency guard here, not an external lock.
    """
    path_list = list(paths)
    if not path_list:
        return GitResult(
            returncode=-1,
            stdout="",
            stderr=(
                "commit_scoped: empty path set refused -- `git ... -- ` with "
                "zero paths commits the WHOLE INDEX, not nothing"
            ),
        )

    root = Path(cwd)
    dir_paths = directory_pathspecs(root, path_list)
    if dir_paths:
        return GitResult(
            returncode=-1,
            stdout="",
            stderr=f"commit_scoped: {directory_pathspec_diagnostic(dir_paths[0])}",
        )

    # AC19 -- same posture as the two guards immediately above: an explicit
    # caller-supplied `deliverable_id` is validated BEFORE either commit
    # mechanism runs, never warned about after the fact. See
    # `_validate_explicit_deliverable_id`'s own docstring for the two checks.
    if deliverable_id:
        rejection = _validate_explicit_deliverable_id(deliverable_id, root)
        if rejection is not None:
            return GitResult(returncode=-1, stdout="", stderr=rejection)

    # The message-authored route into the SAME trailer, which the guard
    # immediately above does not cover: `compute_missing_trailer_args` treats
    # an already-present `Deliverable-Id:` as settled and resolves nothing,
    # so a value the caller typed into `msg_file` itself reaches the commit
    # object unread. Reported by example-retrieval-repo-em (cross-repo/inbox/2026-08-20-
    # example-retrieval-repo-em-wave-commit-deliverable-id-is-per-session.md): a wave
    # commit agent wrote the BRANCH NAME `work/machine-a/2026-08-16to18` into
    # this trailer and the commit exited 0, and `close-out-and-stamp`'s
    # trailer join then silently could not reach the chunk.
    #
    # SHAPE ONLY -- deliberately NOT the existence half
    # `_validate_explicit_deliverable_id` also applies. That half is known
    # over-tight (state/bug-backlog/2026-08-11-scoped-git-commit-rejects-the-
    # deliverabl-4a41eace3946.yaml: its four-path artifact corpus excludes
    # `state/sizings/`, so it refuses ids `coordinator-doc-new` itself
    # mints), and this guard sits on the commit path EVERY concurrent
    # session on this shared worktree runs. Widening a check with an open
    # false-refusal defect onto that surface trades a silent wrong trailer
    # for a loud wrong refusal, fleet-wide. The shape half carries no such
    # risk in either direction: no legitimate commit has ever carried a
    # `Deliverable-Id:` that is neither `dlv-` nor `pln-` prefixed, because
    # no minter produces one.
    #
    # This therefore does NOT catch the second malformed shape that memo
    # reports -- a real but FOREIGN `dlv-` id, which passes shape and
    # existence alike. Only equality against the EXECUTING plan's
    # `deliverable_id` catches that, and this seam does not know which plan
    # is executing; threading it from the emitter is tracked in
    # state/bug-backlog/2026-08-20-workflow-commit-agent-stamps-deliverable-
    # e783825207c3.yaml and is not closed here.
    if not deliverable_id:
        authored = read_trailer_value(msg_file, "Deliverable-Id:")
        if authored is not None and not (
            authored.startswith("dlv-") or authored.startswith("pln-")
        ):
            return GitResult(
                returncode=-1,
                stdout="",
                stderr=(
                    f"commit_scoped: Deliverable-Id trailer {authored!r} in the "
                    "commit message rejected -- does not match the 'dlv-' or "
                    "'pln-' shape convention. Remove the hand-written trailer "
                    "line and let the engine resolve it, or pass a minted id "
                    "as --deliverable-id."
                ),
            )

    # Layer-1 CAS snapshot (2026-08-14, state/audits/2026-08-14-scoped-
    # commit-partial-stage-sweep.md "Recommended fix shape" § Layer 1) --
    # taken BEFORE `diverging_paths()` runs, so it captures each path's
    # index/HEAD blob as of THIS call's own entry. `_agree_branch_cas_
    # refusal()` re-observes both immediately before the agree branch's own
    # `git add`, below, and refuses rather than silently proceeding if
    # either moved in the window between here and there -- see that
    # function's own docstring for the two refusal signals. Intentionally
    # NOT threaded through the private-index branch: that branch already has
    # its own CAS (the 4-argument `update-ref` in `_commit_scoped_private_
    # index`), and never re-reads the worktree the way the agree branch's
    # `git add` does, so it carries none of this hazard.
    pre_index_blobs = _index_blobs(root, path_list)
    pre_head_blobs = _head_blobs(root, path_list)

    # `fail_loud=True` -- an indeterminate `diverging_paths()` answer (a
    # `git diff` failure or timeout) must NEVER collapse to "no divergence"
    # here, unlike Check 13's advisory use of the same predicate: this
    # return value picks the commit MECHANISM. Silently taking the AGREE
    # branch on an indeterminate result would discard deliberately-staged
    # content exactly like the claude-klabauter 506748a0 incident this
    # function exists to prevent -- through the very tool built to close it.
    #
    # Choice on indeterminate: FAIL LOUD (raise -> `GitResult.ok is False`),
    # not "default to the private-index branch". The private-index branch
    # IS safe under either divergence state (it commits exactly each path's
    # currently-staged content and never mutates the shared index), which
    # made "just always take the safe branch" tempting -- but doing that
    # SILENTLY on a `diverging_paths()` failure means a persistently broken
    # `git diff` (bad PATH, corrupt index, a timeout that is too tight for
    # this box) degrades every commit to the slower, private-index path
    # forever with no operator-visible signal. A failed ceremony commit is
    # loud, immediately actionable, and simply re-run once the underlying
    # `git` problem is fixed; a silent perpetual downgrade is not. This is
    # the same "detect-then-fail-loud-on-ambiguity, not detect-then-
    # silently-pick" posture the rest of this module already applies to the
    # empty-path-set and directory-pathspec guards above.
    try:
        # ONE state read for BOTH branch-selection questions below (content
        # divergence and mode delta). They were two chunked spawns over this
        # same `path_list`, back to back, with nothing mutating between them
        # -- and one porcelain-v2 record carries every field both of them
        # read. See `_v2_state_records_chunked()`'s own docstring, including
        # the three reads it deliberately does NOT absorb.
        state_records = _v2_state_records_chunked(
            path_list, cwd=str(root), timeout=_DIVERGENCE_CHECK_TIMEOUT_SECS
        )

        def _diverged_from_records(candidates: Sequence[str]) -> Set[str]:
            """`X != "." and Y != "."` -- identical to the `git diff --cached
            --name-only` / `git diff --name-only` intersection
            `diverging_paths()` documents, read off `state_records`."""
            return {
                p
                for p in candidates
                if (rec := state_records.get(p)) is not None
                and rec.x != "."
                and rec.y != "."
            }

        if known_checked is not None and known_diverged is not None:
            gap = [p for p in path_list if p not in known_checked]
            # Chunked (see `_diverging_paths_chunked()`'s own docstring):
            # `git diff` cannot take a `--pathspec-from-file`, so this is
            # the one of `commit_scoped()`'s three git calls that stays on
            # argv, packed into argv-safe batches instead. `gap` is
            # ordinarily empty/small (the dedup seam's whole point), but a
            # swept-rename destination set can still reach percolate-
            # publish scale.
            gap_diverged = _diverged_from_records(gap) if gap else set()
            diverged = sorted((known_diverged & set(path_list)) | gap_diverged)
        else:
            # Chunked for the same argv-length reason as the `gap` branch
            # above -- this is the common case (`commit_pipeline.commit()`
            # no longer passes `known_checked`/`known_diverged` at all, per
            # `docs/plans/2026-08-07-excise-the-ceremony-lock.md` § C10), so
            # `path_list` here is routinely the full percolate-publish batch
            # (~2000-2700 paths) that blew the raw 32767-char Windows argv
            # cap on one unchunked `git diff --cached --name-only` call
            # (`rc=127`, "divergence indeterminate" -- the live failure this
            # fix closes).
            diverged = sorted(_diverged_from_records(path_list))

        # Mode-preservation fix: union in the subset of `path_list` whose
        # STAGED mode differs from HEAD's while the content is unchanged
        # (`_mode_delta_paths_chunked`'s own docstring) -- ADDITIVE
        # observation only, computed AFTER the content-divergence answer
        # above and unioned into the SAME `diverged` set that drives the
        # branch selection below, never a separate selector. Without this,
        # a pure `update-index --chmod=+x` toggle leaves `diverged` empty
        # (staged content already equals worktree content, by construction
        # of how the chmod was staged -- see `_mode_delta_paths_chunked`'s
        # own docstring), routing to the agree branch's path-restricted
        # `git commit`, which silently discards the mode under `core.
        # fileMode=false` (DR-151). Scoped to `path_list`, chunked
        # identically to the content check just above -- proportional cost,
        # no per-path spawn. `mode_delta_paths` is kept separately (not
        # just folded into `diverged` and discarded) so
        # `_commit_scoped_private_index` can exclude these paths from its
        # `worktree_excluded` reporting -- see that function's own
        # `mode_only_paths` parameter docstring.
        # Read off `state_records` rather than its own `git diff --cached
        # --raw` spawn: `m_head != m_index` with `sha_head == sha_index` is
        # the same "same blob, different mode" predicate
        # `_mode_delta_paths_chunked()` documents, and a path added or
        # deleted (mode `000000`) fails the OID-equality half here exactly as
        # it failed `<oldsha> == <newsha>` there.
        mode_delta_paths = {
            p
            for p in path_list
            if (rec := state_records.get(p)) is not None
            and rec.m_head != rec.m_index
            and rec.sha_head == rec.sha_index
        }
        diverged = sorted(set(diverged) | mode_delta_paths)
    except DivergenceCheckFailed as exc:
        return GitResult(
            returncode=-1,
            stdout="",
            stderr=(
                f"commit_scoped: divergence check indeterminate for {len(path_list)} "
                f"path(s) -- refusing to guess the commit mechanism ({exc}). Re-run once "
                f"the underlying `git diff` problem (timeout, PATH, index) is resolved."
            ),
        )

    # C3 (docs/plans/2026-08-14-the-tool-stages-what-it-commits.md):
    # `supplied_blobs` never reaches the agree branch, regardless of what
    # `diverged` says. A supplied path is always routed to
    # `_commit_scoped_private_index`, which resolves it to `_SOURCE_SUPPLIED`
    # via `_resolve_content_sources` (supplied wins over both `diverged`/
    # `non_diverged` membership) and commits its cacheinfo blob verbatim,
    # never re-reading the worktree.
    #
    # C3's ORIGINAL rationale named "the agree branch's own `git add --
    # path_list`, which reads the SHARED worktree and would silently
    # overwrite a supplied path's provenanced blob". That `git add` no
    # longer exists: the zero-spawn rewrite (verdicts/2026-08-25-the-zero-
    # spawn-git-object-write.md, and the block inside the branch below)
    # retired the agree branch's `git add`/`git commit` pair in favour of
    # the SAME `_commit_scoped_private_index` landing the diverged branch
    # uses. Both branches now build under a private index and land via
    # `cas_ref`; neither stages onto the shared index. The routing above
    # stands on `_resolve_content_sources`'s precedence, not on avoiding a
    # `git add` that is gone.
    #
    # Corrected 2026-08-27 (claude-klabauter-b8) because the stale wording is
    # load-bearing for readers: "the agree branch stages" is what a peer
    # reasoning about absorption risk concludes from, and it is no longer
    # true in either direction -- it does not sweep a peer's staged work,
    # and it does not leave the shared index holding an entry for a path it
    # just committed. That second consequence is the defect in
    # state/bug-backlog/2026-08-27-a-bare-commit-after-memo-send-reverts-
    # the-whole-receipt.yaml: a brand-new file committed through either
    # branch is never added to the shared index, so `git status` reports it
    # `D ` and `??` at once and the next bare commit by any session lands
    # its deletion.
    supplied_blobs = supplied_blobs or {}
    if not diverged and not supplied_blobs:
        # Layer-1 CAS check (see the snapshot comment above `diverging_
        # paths()`): re-observe index/HEAD blobs now, right before this
        # branch's own landing call, and refuse rather than silently
        # restaging from the worktree if the world moved since
        # `pre_index_blobs`/`pre_head_blobs` were captured. FAILS LOUD
        # (`GitResult.ok is False`), same posture as every other guard in
        # this function -- never a warning, never a silent fall-through to
        # the private-index branch (see `_agree_branch_cas_refusal`'s own
        # docstring for why this stays a blob-identity check, not an
        # ownership gate).
        cas_refusal = _agree_branch_cas_refusal(root, path_list, pre_index_blobs, pre_head_blobs)
        if cas_refusal is not None:
            return cas_refusal

        # AC12 (test_hook_shims_portable.py::test_ac12_trailer_sentinel_
        # has_exactly_one_setter): this call site is the sole place in the
        # tree the sentinel may be set from, and it must sit immediately
        # after `_apply_trailers` returns `None` on this, the agree branch.
        # Applying trailers HERE (rather than only inside `_commit_scoped_
        # private_index`, below) is not redundant with that helper's own
        # `compute_missing_trailer_args`/`_apply_trailers` pair -- both only
        # ever ADD a trailer that is still missing, so once this call has
        # written every trailer `msg_file` needs, the helper's own repeat of
        # the same computation below resolves nothing further to add and its
        # `_apply_trailers` becomes a no-op; an explicit `deliverable_id`
        # already folded in here is merely re-observed as already-agreeing
        # by that helper's own `_check_deliverable_id_precedence`, never
        # re-applied or rejected.
        trailer_args = compute_missing_trailer_args(
            msg_file, root, paths=path_list,
            session_id_override=attributed_session_id,
        )
        if deliverable_id:
            msg_text_before = Path(msg_file).read_text(encoding="utf-8")
            if _check_deliverable_id_precedence(msg_text_before, deliverable_id):
                trailer_args = _drop_trailer_arg(trailer_args, "Deliverable-Id")
                trailer_args = trailer_args + ["--trailer", f"Deliverable-Id: {deliverable_id}"]
        interpret_result = _apply_trailers(msg_file, trailer_args, root)
        if interpret_result is not None:
            return interpret_result
        # `_apply_trailers` just returned `None` above -- the trailers ARE
        # on `msg_file` now, a fact this code just established. No spawned
        # `git commit` runs on this branch any more (see below), so no
        # `prepare-commit-msg` hook will ever read this sentinel for THIS
        # call -- it is set anyway, unconditionally, so AC12's single-setter
        # invariant continues to describe a real fact about this call site
        # rather than a landmark from a mechanism that no longer runs here.
        trailer_sentinel_env = _trailer_sentinel_env()
        del trailer_sentinel_env

        # C3 dispatch (state/dispatch-briefs/2026-08-26-the-commit-becomes-
        # a-warm-served-op/C3.md), spike verdict docs/research/spike-
        # verdicts/2026-08-25-the-zero-spawn-git-object-write.md: the agree
        # branch used to pay TWO git spawns here (`git add --pathspec-from-
        # file`, then `git commit -F --pathspec-from-file`). It now reuses
        # `_commit_scoped_private_index` -- the SAME in-process tree-spine-
        # rewrite + `cas_ref` landing (route R3 for the object/ref halves;
        # the spike's R2 update-ref spawn is retired in favour of the
        # hand-rolled lockfile CAS `cas_ref` already implements, including
        # its own loose-ref resolution) already proven for the DIVERGED
        # branch below -- rather than a bespoke agree-branch mechanism.
        #
        # `path_list` is the FINAL, caller-vetted commit pathspec, which
        # legitimately includes paths already fully staged-deleted (e.g.
        # this pipeline's `deleted_paths`, pre-`git rm`'d by the caller
        # before invoking the ceremony) -- absent from BOTH the worktree
        # AND the index already, nothing further to stage. Such a path is
        # routed to `_commit_scoped_private_index`'s STAGED-source arm
        # (`_assemble_commit_tree_input` resolves a `_SOURCE_STAGED` path
        # with no index entry to the ABSENT set, removing it from the tree
        # -- never a `git add` refusal). Every path that still exists on
        # disk is routed to the WORKTREE-source arm, which hashes it via
        # ONE `git hash-object -w --stdin-paths` spawn (through git's own
        # clean-filter/`core.autocrlf` pipeline -- see that helper's own
        # docstring) regardless of how many such paths there are, or ZERO
        # spawns when every path in this call is already gone from the
        # worktree.
        #
        # `_commit_scoped_private_index` computes its own trailers
        # (`compute_missing_trailer_args` + `_apply_trailers`, the explicit
        # `deliverable_id` precedence fold included) and lands via
        # `_commit_via_head_spine`'s fast (zero-spawn) path or its own
        # `commit-tree`/`update-ref` ladder -- neither fires any git hook,
        # so (unlike the old real `git commit` here) there is no
        # `prepare-commit-msg`/`post-commit` to rely on OR to stand down;
        # `suppress_post_commit_auto_push` is honoured below via the same
        # explicit `_replay_post_commit_auto_push` call the diverged branch
        # already uses, not via any flag threaded into the landing call.
        existing = [p for p in path_list if (root / p).exists()]
        deleted = [p for p in path_list if p not in existing]
        # `worktree_deleted` is what makes `deleted` MEAN deleted. Without it
        # these paths take the staged-source arm and resolve to ABSENT only
        # when the index has forgotten them too -- so a path deleted on disk
        # whose index entry still stands is written back into the new tree,
        # and the caller's requested removal silently does not happen (see
        # `_assemble_commit_tree_input`'s own `worktree_deleted` paragraph for
        # the live archival-sweep instance). Passed HERE and never on the
        # diverged branch below, where staged content is the caller's intent.
        result = _commit_scoped_private_index(
            deleted, existing, msg_file, root, deliverable_id,
            supplied_blobs=supplied_blobs,
            attributed_session_id=attributed_session_id,
            worktree_deleted=set(deleted),
        )
        if result.ok and not suppress_post_commit_auto_push:
            _replay_post_commit_auto_push(root, path_list, attributed_session_id)
        return result

    diverged_set = set(diverged)
    non_diverged = [p for p in path_list if p not in diverged_set]
    result = _commit_scoped_private_index(
        diverged, non_diverged, msg_file, root, deliverable_id,
        supplied_blobs=supplied_blobs,
        attributed_session_id=attributed_session_id,
        mode_only_paths=mode_delta_paths,
    )
    # This branch lands via `commit-tree`/`update-ref`, which fire NO hooks --
    # so unlike the agree branch above there is no `post-commit` to stand down
    # OR to rely on, and the flag is meaningless here except as the caller's
    # answer to "am I publishing this myself?".
    #
    # `suppress_post_commit_auto_push=True` means the caller IS the sole
    # publisher (`push_mode="sync"`, which pushes synchronously right after
    # this returns) -- replaying here would make two publishers race one tip,
    # the 2026-07-30 false negative `_sole_publisher_env` exists to prevent.
    #
    # False means the caller is NOT publishing and expects the hook to. On the
    # agree branch that is true for free; here nothing fires, so without this
    # replay the commit lands and is stranded local forever while the op
    # reports `push_state="deferred"` -- "queued for background push" with no
    # queue behind it. `_replay_post_commit_auto_push`'s own docstring names
    # this failure ("a commit landed this way that never replays this hook
    # never pushes"); it was wired for `commit_authored_content` and never
    # for this path, which went unnoticed only because `run_commit_pipeline`
    # used to push synchronously on EVERY mode and covered it incidentally.
    if result.ok and not suppress_post_commit_auto_push:
        _replay_post_commit_auto_push(root, path_list, attributed_session_id)
    return result


# ---------------------------------------------------------------------------
# commit_authored_content -- form 3, DR-272 § 3 ("the hash-object-populated,
# HEAD-seeded private-index commit"). A NEW SIBLING entrypoint alongside
# `commit_scoped` -- NOT a mode on it, and NOT a change to `diverging_paths`
# (AC8: that module and its consumers stay byte-unchanged).
#
# `commit_scoped()` picks a mechanism from OBSERVED worktree/index state for
# a caller that already has real files on disk. This entrypoint is for the
# opposite shape: a caller (`plan_status_transition`, `queue.close`,
# `memo.transition`) that already holds the EXACT bytes it wants committed
# in memory (the `locked_rmw` return value) and must never let ANY worktree
# read on its own path -- staged, unstaged, or foreign -- reach the commit,
# because there is no "diverged vs agree" question to compute: the caller
# IS the sole author of this content by construction.
#
# Spec backlink: pln-writer-side-commit-ownership-c-845b25
# chunk C2. Admitted as DR-211 Invariant 4's THIRD sanctioned commit form by
# docs/decisions/DR-272-inplace-mutation-with-self-commit.md § 3 (deliberately
# NOT declared an instance of form 2 -- see that record's § 3.1 for why one
# stretch of form 2's text might be a reading, but two is a new form wearing
# form 2's coat). § 3.3 there is the six-point bounds list every branch below
# cites by number.
# ---------------------------------------------------------------------------


def _hash_object_stdin_bytes(
    content_bytes: bytes,
    rel_path: str,
    *,
    cwd: Union[str, Path],
    timeout: float = _DEFAULT_TIMEOUT_SECS,
) -> GitResult:
    """`git hash-object -w --path=<rel_path> --stdin`, fed RAW BYTES over a
    bytes-mode subprocess leg -- deliberately NOT routed through `_git()`.

    Two corrections pinned by the spike (DR-272 § 3.3 bound 2, plan C2 body
    items 1-2), both load-bearing and neither optional:

    `--path=<rel_path>` is REQUIRED, not cosmetic -- `--stdin` implies
    `--no-filters` unless `--path` is given, so without it a path under a
    real clean `filter.*.clean` driver gets a blob that diverges from what
    `git add` would have produced (spike-probed: `filter.upper.clean:
    'HELLO'` vs the raw `'hello'` bytes passed here). NOT the gitattributes
    `eol` case -- `eol` acts on the smudge/checkout direction only; the
    clean direction already normalizes to LF, so that case does not
    reproduce and must not be cited in its place.

    `text=False`/raw `bytes` input, no `encoding=`/`newline=` -- `_git()`'s
    `text=True` leg is not merely a Windows newline hazard (CPython
    subprocess docs: stdin `'\\n'` -> `os.linesep`). Under `LC_ALL=C` the
    parent's preferred encoding is US-ASCII and a `text=True` call raises a
    hard `UnicodeEncodeError` on any non-ASCII byte -- reproduced on macOS,
    and that is how cron, CI, minimal containers, and SSH-without-locale-
    forwarding all run. Caller-authored content must never cross that leg.
    """
    full_args = ["git", "hash-object", "-w", f"--path={rel_path}", "--stdin"]
    try:
        result = subprocess.run(
            full_args,
            cwd=str(cwd),
            input=content_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            **no_console_creationflags(),
        )
    except subprocess.TimeoutExpired as exc:
        return GitResult(
            returncode=-1,
            stdout="",
            stderr=f"git hash-object: timed out after {timeout}s ({exc})"[:500],
        )
    except OSError as exc:
        return GitResult(
            returncode=-1,
            stdout="",
            stderr=f"git hash-object: {type(exc).__name__} — {exc}"[:500],
        )
    return GitResult(
        returncode=result.returncode,
        stdout=result.stdout.decode("utf-8", errors="replace"),
        stderr=result.stderr.decode("utf-8", errors="replace"),
    )


def _drop_trailer_arg(trailer_args: List[str], trailer_name: str) -> List[str]:
    """Strip every `--trailer <trailer_name>: ...` pair from a flat
    `interpret-trailers` argv (as `compute_missing_trailer_args` returns it),
    preserving every other pair's order. Used to let an explicit,
    caller-supplied value take precedence over one already resolved into
    the same flat argv, without corrupting the surrounding `--trailer`/value
    pairing.
    """
    kept: List[str] = []
    i = 0
    while i < len(trailer_args):
        flag = trailer_args[i]
        value = trailer_args[i + 1] if i + 1 < len(trailer_args) else ""
        if flag == "--trailer" and value.startswith(f"{trailer_name}:"):
            i += 2
            continue
        kept.append(flag)
        i += 1
    return kept


def _replay_post_commit_auto_push(
    root: Union[str, Path],
    path_list: Optional[Sequence[str]] = None,
    attributed_session_id: Optional[str] = None,
) -> None:
    """Replay `coordinator-auto-push` (`post-commit`) after a successful
    `commit_authored_content()` CAS -- DR-272 § 2.4 Bound 2, "MUST be
    replayed" (unlike `pre-commit`, which that same bound EXEMPTS for this
    entrypoint alone; see the record for the exemption's exact scope).
    `commit-tree`/`update-ref` fire none of `pre-commit`, `prepare-commit-
    msg`, `commit-msg`, `post-commit` -- a commit landed this way that never
    replays this hook never pushes, which is a silent divergence the auto-
    push health signal would report as lag with no attributable cause.

    Invoked exactly as the installed `post-commit` hook itself invokes
    `coordinator-auto-push` -- no extra flags beyond `--repo-root`, so
    `async_mode` resolves to the hook's own default (detached background
    push), not a synchronous one this function waits on.

    Best-effort, mirroring `auto_push.main()`'s own contract: that function
    already never raises (broad internal `except Exception`, always returns
    0), but the import itself is wrapped too, so a stripped-down install
    missing the `hooks` package cannot turn a successful commit into a
    raised exception here.

    Honest limit, stated per DR-272 § 2.4 Bound 2's own instruction: hook
    replay after plumbing-only commit machinery is THIS REPO'S OWN CALL --
    the mechanism spike found no primary source of practitioners doing this.

    `path_list` (C4, docs/plans/2026-08-26-the-commit-op-stops-asking-git-
    eleven-times.md): when given, this call's own caller ALREADY knows which
    paths just landed -- `commit_scoped()`'s two callers below pass their own
    `path_list` straight through. Releasing those claims IN-PROCESS here
    (via `session.scope.release_committed_claims`, the same call
    `auto_push._release_claims_for_head` would otherwise reach) retires the
    `git show --name-only HEAD` that helper spends to relearn the same
    paths, and `--no-claim-release` then tells `auto_push.main()` to skip
    its own redundant `git status --porcelain` clean-check leg too -- see
    that flag's own comment in `auto_push.main` and
    `_release_claims_for_head`'s "CORRECTED 2026-08-26" paragraph, which
    names this exact caller. `attributed_session_id`, when given, is the
    caller's own already-resolved committing-session identity (mirrors
    `commit_scoped`'s own docstring for that parameter) and takes precedence
    over a blind `session_core.resolve_session_id()` read of this process's
    env, for the same reason `commit_scoped` prefers it for its `Session-Id:`
    trailer.

    `path_list=None` (`commit_authored_content`'s call, not migrated by this
    chunk) preserves the prior behavior unchanged: `auto_push.main()` runs
    its own `_release_claims_for_head` fallback, exactly as before.

    Fail-open, like `_release_claims_for_head` itself: any failure in the
    in-process release falls through to the unmigrated path (no
    `--no-claim-release`), so `auto_push.main()`'s own fallback release still
    runs rather than leaving the claim stuck `T` forever.
    """
    argv = ["--repo-root", str(root)]
    if path_list:
        try:
            from coordinator_core.session import core as _session_core
            from coordinator_core.session import scope as _session_scope

            sid = attributed_session_id or _session_core.resolve_session_id(str(root))
            paths = [p for p in path_list if p]
            if sid and paths:
                # Releases only paths that are CLEAN in the worktree, and is
                # structurally incapable of releasing a peer's claim -- see
                # `release_committed_claims`'s own docstring for both
                # properties. Same clean-check `_release_claims_for_head`
                # itself relies on; this call does not relax it.
                _session_scope.release_committed_claims(sid, paths, cwd=str(root))
            argv.append("--no-claim-release")
        except Exception:
            # Never let a diagnostic here become the thing that blocks the
            # commit auto-push replays after -- fall through to the
            # unmigrated leg so `auto_push.main()`'s own fallback release
            # still runs.
            argv = ["--repo-root", str(root)]

    try:
        from coordinator_core.hooks import auto_push

        auto_push.main(argv)
    except Exception:
        pass


#: The tree algebra proper (`_ABSENT`, `_write_tree_level`,
#: `_rewrite_head_spine`, `_synthesize_absent_spine_dirs`) was relocated
#: (2026-08-26, C1 of docs/plans/2026-08-26-the-archival-commit-helper-
#: computes-its-own-tree.md) to `coordinator_core.git.tree_spine` --
#: `_commit_via_head_spine` below stays here (it drags commit policy: CAS
#: landing, identity resolution, trailer handling). Re-exported here under
#: their original names so every existing caller -- including
#: `test_git_native.py::test_rewrite_head_spine_prunes_emptied_dirs_like_git`,
#: which reaches them as `git_native._rewrite_head_spine`/`git_native._ABSENT`
#: -- keeps working unmodified.
from coordinator_core.git.tree_spine import (  # noqa: E402
    _ABSENT,
    _rewrite_head_spine,
    _synthesize_absent_spine_dirs,
    _write_tree_level,
)


def _resolve_cas_ref_target(root: Path) -> Optional[Tuple[Path, str]]:
    """Return `(gitdir, ref_relpath)` -- the physical LOOSE ref file
    `cas_ref()`'s lockfile protocol must act on for the branch HEAD
    currently points at, or `None` -- take the ladder -- when this cannot
    be resolved with confidence.

    A normal (non-detached) checkout: `HEAD` (worktree-private) is a
    symref, `ref: refs/heads/<x>`; the real commit sha -- and the CAS
    target -- lives at `<common_dir>/refs/heads/<x>` (refs are never
    worktree-private; see `git_state.head_sha`'s own docstring). Detached
    HEAD: the CAS target IS `HEAD` itself, in the WORKTREE-PRIVATE gitdir
    (`cas_ref`'s own docstring names this case explicitly).

    Returns `None` when the resolved branch ref is not a loose file (e.g.
    packed only) -- `cas_ref`'s `O_CREAT|O_EXCL` lock protocol assumes a
    loose ref at the target path, and a packed-only ref is rare enough
    (freshly-committed branches are always loose) that reproducing git's
    own pack-then-loose ref resolution here is not worth the risk of
    silently CAS-ing against the wrong file.
    """
    worktree_gitdir = resolve_git_dir(root)
    try:
        head_text = (worktree_gitdir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not head_text.startswith("ref:"):
        return worktree_gitdir, "HEAD"
    ref_rel = head_text[len("ref:") :].strip()
    common_dir = resolve_git_common_dir(root)
    if not (common_dir / ref_rel).is_file():
        return None
    return common_dir, ref_rel


def _read_config_user_section(config_path: Path) -> Tuple[Optional[str], Optional[str]]:
    """Best-effort `[user] name = ...` / `email = ...` read from one git
    config file -- same simple line-scanning shape as `git_objects.
    _log_all_ref_updates`'s `[core]` reader, for the same reason: a full
    git-config parser (multi-file include chains, `includeIf`, quoted
    values with escapes) is out of scope for a fast-path identity lookup
    whose failure just means "take the ladder", never a wrong commit."""
    try:
        text = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, None
    name: Optional[str] = None
    email: Optional[str] = None
    in_user = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_user = stripped.lower().startswith("[user]")
            continue
        if in_user and "=" in stripped:
            key, _, value = stripped.partition("=")
            key = key.strip().lower()
            value = value.strip().strip('"')
            if key == "name" and name is None:
                name = value
            elif key == "email" and email is None:
                email = value
    return name, email


def _resolve_commit_identity(root: Path) -> Optional[Tuple[str, str]]:
    """Resolve `(name, email)` for the commit object's author/committer
    lines, usually without spawning -- env vars first (git's own
    precedence: `GIT_AUTHOR_*`/`GIT_COMMITTER_*` override config), then
    this repo's own `config` (in the COMMON dir -- `user.*` is not a
    worktree-private setting), then the user's global `~/.gitconfig`.

    Negative spec -- a miss here must NOT cost the ladder. These sources
    are narrower than git's own resolution (no `includeIf`, no system
    config), so a correctly-configured identity git would find can still
    miss. Falling back to the ~9-spawn ladder to recover a name and an
    email trades the entire in-process win for one string: the identity
    is not worth that. `_commit_identity_via_git_var` pays ONE spawn to
    get git's full resolution instead. Only if THAT fails -- no identity
    configured anywhere, which is a real refusal condition git itself
    would hit -- does this return `None` and take the ladder.

    A partial identity is never returned: both halves, or nothing. A
    half-resolved pair would silently commit as `"None <None>"` in the
    object body."""
    name = os.environ.get("GIT_AUTHOR_NAME") or os.environ.get("GIT_COMMITTER_NAME")
    email = os.environ.get("GIT_AUTHOR_EMAIL") or os.environ.get("GIT_COMMITTER_EMAIL")
    if name and email:
        return name, email
    for config_path in (
        resolve_git_common_dir(root) / "config",
        Path.home() / ".gitconfig",
    ):
        cfg_name, cfg_email = _read_config_user_section(config_path)
        name = name or cfg_name
        email = email or cfg_email
        if name and email:
            return name, email
    return _commit_identity_via_git_var(root)


def _commit_identity_via_git_var(root: Path) -> Optional[Tuple[str, str]]:
    """One `git var GIT_COMMITTER_IDENT` spawn -- git's OWN full identity
    resolution, `includeIf` and system config included -- for the case the
    cheap sources above miss.

    Purpose: bound the cost of an identity miss at one process instead of
    the ladder's nine. `git var` prints
    `Name <email> <epoch> <±HHMM>`; only the name and email are taken,
    since `_author_stamp` produces the timestamp (git would stamp "now"
    here too, and re-using `git var`'s instant would drift from the
    author line by however long the rest of the commit takes).

    Returns `None` when git itself cannot resolve an identity -- exit
    code 128, the "Please tell me who you are" refusal. That is a genuine
    ladder condition, not a parsing gap: the ladder's `commit-tree` will
    fail the same way and produce git's own diagnostic, which is a better
    error than anything invented here."""
    result = _git(["var", "GIT_COMMITTER_IDENT"], cwd=root)
    if not result.ok:
        return None
    ident = (result.stdout or "").strip()
    open_bracket = ident.rfind(" <")
    close_bracket = ident.rfind("> ")
    if open_bracket == -1 or close_bracket == -1 or close_bracket < open_bracket:
        return None
    name = ident[:open_bracket].strip()
    email = ident[open_bracket + 2 : close_bracket].strip()
    if not name or not email:
        return None
    return name, email


def _author_stamp() -> str:
    """`<epoch-seconds> <±HHMM>` -- git's commit-object timestamp format,
    for the CURRENT instant in the local timezone (matches what a spawned
    `git commit-tree` with no `GIT_AUTHOR_DATE`/`GIT_COMMITTER_DATE`
    override would stamp: "now", local offset)."""
    now = int(time.time())
    is_dst = time.localtime(now).tm_isdst > 0
    offset_seconds = -(time.altzone if is_dst else time.timezone)
    sign = "+" if offset_seconds >= 0 else "-"
    offset_seconds = abs(offset_seconds)
    return f"{now} {sign}{offset_seconds // 3600:02d}{(offset_seconds % 3600) // 60:02d}"


def _commit_via_head_spine(
    root: Path,
    assembled: Dict[str, Union[Tuple[int, str], object]],
    old_head: str,
    msg_file: Union[str, Path],
    *,
    index_stat_identity: Optional[Any] = None,
    create_missing_dirs: bool = False,
    refuse_noop: bool = False,
    caller: str,
) -> Optional[GitResult]:
    """The shared "rewrite HEAD's tree spine -> build the commit object ->
    land it via a locked ref CAS" landing helper (C4 body: "one helper, two
    input assemblers") -- ZERO git spawns of its own. `msg_file` must
    already carry every trailer this commit needs (`interpret-trailers`,
    the caller's own spawn, has already run against it); this helper only
    reads its final bytes for the commit object's message body.

    `refuse_noop` (default `False` -- `commit_authored_content` is
    byte-identical without it, unchanged) -- when `True`, a computed
    `new_tree_sha` equal to the CURRENT HEAD tree (`root_tree_sha`, already
    read above for the same-tree precondition) refuses with a
    `GitResult(returncode=1, stdout="", stderr="")` BEFORE any object is
    written or the ref moved, rather than landing a commit whose tree is
    byte-identical to its own parent's. Opt-in because it changes an
    observable contract (a caller re-committing identical content used to
    get a real, if pointless, commit) -- `_commit_scoped_private_index`
    passes `True` on both its own call sites (see that function's own
    docstring), matching the "nothing to commit" refusal `git commit -F
    ... -- paths` gave for free before C3 rebuilt this branch in process.

    `create_missing_dirs` (default `False` -- every pre-existing caller is
    byte-identical without it) fills in empty spine levels for a CREATION
    under a directory absent from HEAD, which `read_tree_spine` leaves out
    of the spine and `_rewrite_head_spine` therefore refuses. See
    `_synthesize_absent_spine_dirs` for what it refuses to fill and why;
    `commit_authored_new_file` is the caller that needs it (the first memo
    into a peer repo whose `cross-repo/inbox/` does not exist yet).

    `caller` -- the entrypoint's own name (e.g. `"commit_authored_content"`),
    threaded into both diagnostic strings this helper can return (the empty-
    tree refusal and the CAS-lost message below) so a caller reached through
    any of this helper's three callers gets a correctly-attributed failure,
    never one that misnames a different entrypoint.

    `assembled` -- `{path: (mode, sha) | _ABSENT}`, repo-relative,
    `/`-separated paths. `commit_authored_content` assembles exactly one
    entry (its own path, never `_ABSENT` -- that entrypoint is in-place
    mutation of an existing path only); a future multi-path caller (C8b)
    assembles many, deletions included.

    Returns `None` -- take the ladder, UNCHANGED, no side effect from this
    call survives that decision (every write below is a content-addressed
    loose object; an orphaned one is harmless) -- when any PRECONDITION
    fails: `head_tree_sha`/`read_tree_spine` do not resolve, a changed
    path's spine is structurally missing, the CAS ref cannot be resolved
    to one confident loose-file target, that ref is currently lock-held by
    a peer (`cas_ref`'s own protocol: an existing `<ref>.lock` is a
    fall-back, never a wait), or this process cannot resolve a commit
    identity.

    Once every precondition holds, every subsequent problem is returned as
    a REAL failing `GitResult`, never silently downgraded back to a ladder
    fall-back: AC11(b)'s index `stat_identity` re-check (immediately
    before the CAS, when `index_stat_identity` is given -- `commit_
    authored_content` never passes one, since it reads no index at all;
    C8b's multi-path arm does) and a lost ref CAS both refuse loud in the
    `compare-and-swap failed` diagnostic family. A re-read-and-retry here
    would risk committing a peer's newer staged blob (index case) or
    orphaning a peer's own commit (ref case) -- exactly the hazard each
    refusal exists to prevent.
    """
    root_tree_sha = _git_state_head_tree_sha(root)
    if root_tree_sha is None:
        return None

    spine = read_tree_spine(root, list(assembled.keys()))
    if spine is None:
        return None

    if create_missing_dirs:
        synthesized = _synthesize_absent_spine_dirs(spine, assembled)
        if synthesized is None:
            return None

    ref_target = _resolve_cas_ref_target(root)
    if ref_target is None:
        return None
    ref_gitdir, ref_relpath = ref_target
    lock_path = ref_gitdir / (ref_relpath + ".lock")
    if lock_path.exists():
        return None

    identity = _resolve_commit_identity(root)
    if identity is None:
        return None
    name, email = identity

    if index_stat_identity is not None:
        # `fresh=True` (C1, claude-klabauter-75): this re-check exists to
        # observe a peer's write to `.git/index` between comparand capture
        # and this CAS -- it must never be served from
        # `index_read_cache_scope()`'s within-call cache, or the CAS
        # compares the same snapshot its comparand came from and can never
        # fail. See `_agree_branch_cas_refusal`'s identical `fresh=True`
        # re-observation and `_index_blobs`'s own `fresh` docstring above.
        # `read_index_stat_identity` (C6, docs/plans/2026-08-27-the-commit-
        # op-resolves-one-pass-context.md) obtains the same value as
        # `read_index(root, fresh=True).stat_identity` without paying for a
        # full entry parse this call never consulted.
        fresh_identity = read_index_stat_identity(root)
        if fresh_identity != index_stat_identity:
            return GitResult(
                returncode=1,
                stdout="",
                stderr=(
                    f"compare-and-swap failed -- {INDEX_STAT_CAS_MARKER}; "
                    "refusing to retry silently (a retry needs a tree "
                    "rebuilt from a fresh snapshot, and a fall-back here "
                    "would re-read and commit a peer's newer staged blob, "
                    "defeating this detector rather than discharging it)"
                ),
            )

    common_dir = resolve_git_common_dir(root)
    new_tree_sha = _rewrite_head_spine(common_dir, spine, assembled)
    if new_tree_sha is None:
        return None

    empty_tree_refusal = _empty_private_index_refusal(
        new_tree_sha, root=root, caller=caller
    )
    if empty_tree_refusal is not None:
        return empty_tree_refusal

    # `refuse_noop` -- see this function's own docstring paragraph. Checked
    # against `root_tree_sha`, the SAME HEAD-tree read this function's own
    # precondition already took above (never a second read that could race
    # a concurrent mutation between the two).
    # THE EMPTY `stderr` IS THE CONTRACT, NOT AN OMISSION. `returncode=1` with
    # no message is the sentinel a benign no-op is rendered QUIETLY by --
    # `test_pipeline_empty_commit_set_noop_rolls_back_quietly` and
    # `test_commit_failure_bare_exit_code_preserved_for_downstream_quiet_
    # rendering` both pin it. Naming a reason here (tried 2026-08-27, reverted)
    # turns "a peer landed byte-identical content first" into a loud refusal
    # and reds both. Not to be confused with `test_stage_failure_report_never_
    # a_bare_exit_code`, which governs the STAGE report -- a different path.
    if refuse_noop and new_tree_sha == root_tree_sha:
        return GitResult(returncode=1, stdout="", stderr="")

    msg_bytes = Path(msg_file).read_bytes()
    subject_line, _, _ = msg_bytes.decode("utf-8", errors="replace").partition("\n")
    subject = subject_line or "commit"

    stamp = _author_stamp()
    who = f"{name} <{email}> {stamp}"
    header = f"tree {new_tree_sha}\nparent {old_head}\nauthor {who}\ncommitter {who}\n\n".encode(
        "utf-8"
    )
    new_commit_sha = write_object(common_dir, b"commit", header + msg_bytes)

    landed = cas_ref(
        ref_gitdir,
        ref_relpath,
        old_head,
        new_commit_sha,
        reflog_committer=who,
        reflog_message=subject,
        head_gitdir=resolve_git_dir(root),
    )
    if not landed:
        return GitResult(
            returncode=1,
            stdout="",
            stderr=(
                f"{caller}: compare-and-swap failed -- HEAD moved "
                f"concurrently since {old_head} was captured; refusing to retry "
                "silently (a retry needs a tree rebuilt against the new HEAD)."
            ),
        )

    return GitResult(returncode=0, stdout=new_commit_sha, stderr="")


def _head_entry_for(root: Path, normalized: str) -> Optional[Tuple[int, str]]:
    """`(mode, sha)` for `normalized` in HEAD's tree, or `None` when the
    path does not exist there.

    Reads HEAD's tree spine IN PROCESS first (C3's `read_tree_spine`) and
    only falls back to `head_blobs` -- which spawns `git ls-tree` -- when
    the spine is unreadable, which is the same condition that sends the
    commit to the ladder anyway.

    Negative spec -- this is a PROCESS-COUNT reduction, not a routing
    tidy-up, and the distinction is the whole point. `head_blobs` reaches
    git through `run_git`, NOT through this module's `_git()` seam, so a
    spawn counter wrapped around `_git()` cannot see it: AC1 measured at
    that seam read 3 while the leg actually issued 4 processes
    (`ls-tree`, `hash-object`, `interpret-trailers`, `update-index`).
    That is the "a falling seam with a flat op_total means a spawn moved,
    not disappeared" failure this plan's own cited lesson names, caught by
    `test_ledger_leg_spawn_count_is_upper_bound_not_exact` rather than by
    any spawn assertion in this plan.

    Both arms answer the same question of the same source -- HEAD's tree --
    so the "does not exist in HEAD" refusal is identical either way."""
    spine = read_tree_spine(root, [normalized])
    if spine is not None:
        parent, _, leaf = normalized.rpartition("/")
        entry = spine.get(parent, {}).get(leaf)
        return entry
    raw = _git_state_head_blobs(root, [normalized]).get(normalized)
    return raw  # type: ignore[return-value]


def commit_authored_content(
    path: str,
    content: str,
    msg_file: Union[str, Path],
    cwd: Union[str, Path],
    *,
    deliverable_id: Optional[str] = None,
    attributed_session_id: Optional[str] = None,
) -> GitResult:
    """Commit EXACTLY `content` at `path`, reading no worktree state on
    `path` at any point (DR-272 § 3.3 bound 2) -- the sibling entrypoint to
    `commit_scoped()` for a caller that already holds the authored bytes
    in memory (`locked_rmw`'s return value) rather than files staged or
    edited on disk. See the module-section docstring above for the shape
    difference from `commit_scoped()`.

    Bounds enforced (DR-272 § 3.3, numbered to match):

      1. Single path -- `path` is one string, not a sequence; no multi-path
         form exists here.
      2. No worktree read on `path`, ever -- content comes from the `content`
         parameter; the `Deliverable-Id` trailer tier-0 join key comes from
         the `deliverable_id` parameter, never from opening the committed
         file (see the trailer-replay block below).
      3. The private index is seeded by `GIT_INDEX_FILE=<fresh temp> git
         read-tree HEAD` -- never a `shutil.copy2` of the shared
         `.git/index`. `_commit_scoped_private_index` seeds identically
         (`read-tree HEAD` into a fresh temp index, see that function's own
         docstring) -- both call sites now match form 2's own parenthetical
         ("never from the shared worktree index") and form 3 bound 3.
      4. Compare-and-swap `update-ref`, failing loud with a distinctive
         diagnostic when HEAD moved concurrently -- identical shape to
         `_commit_scoped_private_index`'s own CAS.
      5. Hook effects replayed per DR-272 § 2.4 Bound 2 -- trailers (below)
         and post-commit auto-push (`_replay_post_commit_auto_push`, called
         only after a successful CAS). `pre-commit` is EXEMPTED for commits
         issued through this entrypoint, bounded to single-path,
         caller-supplied-content commits exactly as built here -- widening
         this entrypoint's contract (multi-path, worktree-sourced content,
         a trailing pathspec) voids that exemption per the record.
      6. Post-commit shared-index refresh -- a single-path `git update-index
         --add --cacheinfo` against the SHARED (non-private) index after a
         successful CAS, so `git diff --cached -- path` reads empty
         afterward instead of the shared index reading as a staged
         reversion nobody staged (AC11). Recorded honestly, per DR-272 §
         3.3(6): this hazard is INFERRED from documented index mechanics,
         not a citable, named phenomenon -- the mechanism spike found no
         primary source for it. BEST-EFFORT like bound 5's auto-push
         replay -- the commit has already landed via CAS by the time this
         runs, so a refresh failure (e.g. `--cacheinfo` racing a
         concurrent peer's own staged change to the same path between the
         private-index build and this call) must never turn an actually-
         successful commit into a reported failure with no SHA; `--add`
         additionally means a path absent from the shared index (e.g.
         concurrently staged-deleted there) does not itself trip the
         refresh.

    Containment guards (none inherited from `commit_scoped()` -- those live
    on that function, not on the private-index machinery both reuse):
    `path` must be repo-relative and resolve inside `cwd`'s worktree, must
    not currently be a directory on disk, and must exist in `HEAD` (this
    entrypoint is built for in-place mutation of an existing reserved-noun
    file, not for creating a new one -- a caller needing that is out of
    scope for C2 and gets a loud, diagnostic refusal here rather than silent
    best-effort handling). `content` must not be `None`.

    File mode is preserved from `HEAD`'s existing tree entry for `path` --
    this entrypoint never changes a file's executable bit; only its content.

    `attributed_session_id` (state/bug-backlog/2026-08-18-scoped-git-commit-
    stamps-a-foreign-session-id-8d21f0c4e7b9.yaml) -- OPTIONAL, passed
    straight through to `compute_missing_trailer_args`'s own
    `session_id_override`, same contract `commit_scoped`/
    `_commit_scoped_private_index` already carry (see their own docstrings).
    `None` (the default) reproduces the prior blind
    `session_core.resolve_session_id()` env-var resolution byte-for-byte --
    every caller not yet updated to pass its own already-resolved identity
    sees no behaviour change. A caller that has one (e.g. an explicit
    `session_id`/`closed_by` request param) should pass it here rather than
    let this function's own `compute_missing_trailer_args` call re-derive
    the committer's identity a second, independent way -- the same
    disagreeing-copies hazard `commit_scoped`'s own docstring names.

    Returns a `GitResult`; on success `stdout` carries the new commit SHA
    (matching `_commit_scoped_private_index`'s own contract). Failure
    semantics mirror `_commit_scoped_private_index` exactly: a raw failing
    `GitResult` is returned early at the step that failed, and a concurrent
    HEAD move surfaces the same distinctive CAS diagnostic.
    """
    if content is None:
        return GitResult(
            returncode=-1,
            stdout="",
            stderr="commit_authored_content: content is None -- refusing to commit an absent write",
        )

    root = Path(cwd)
    normalized = path.replace("\\", "/")
    if (
        not normalized
        or normalized.startswith("/")
        or ".." in Path(normalized).parts
        or _has_windows_drive(normalized)
    ):
        return GitResult(
            returncode=-1,
            stdout="",
            stderr=(
                f"commit_authored_content: {path!r} is not a repo-relative, "
                "in-worktree path -- refusing an absolute path or a `..` "
                "traversal segment"
            ),
        )
    target = root / normalized
    try:
        target.resolve().relative_to(root.resolve())  # fs-only: containment check, never stringified
    except ValueError:
        return GitResult(
            returncode=-1,
            stdout="",
            stderr=f"commit_authored_content: {path!r} resolves outside the worktree rooted at {root}",
        )
    if target.is_dir():
        return GitResult(
            returncode=-1,
            stdout="",
            stderr=f"commit_authored_content: {directory_pathspec_diagnostic(normalized)}",
        )

    # C3 (docs/plans/2026-08-21-the-commit-path-reads-git-state-without-
    # spawning-git.md): `git rev-parse HEAD` replaced by a direct `.git/HEAD`
    # file read -- zero spawns, not a subprocess swap. `old_head` still
    # serves both roles the single prior read served (commit-tree's `-p`
    # parent AND `update-ref`'s CAS old-value argument below); the atomicity
    # guarantee comes from `update-ref`'s own compare-and-swap against the
    # LIVE ref at call time, not from re-observing HEAD a second time here,
    # so this swap changes nothing about that guarantee.
    old_head = _git_state_head_sha(root)
    if old_head is None:
        return GitResult(
            returncode=-1,
            stdout="",
            stderr=(
                "commit_authored_content: HEAD has no resolvable commit "
                "(unborn branch or a symref with no loose ref and no "
                "packed-refs entry) -- this entrypoint requires an existing "
                "HEAD commit to read the parent and the reserved-noun's "
                "current mode from"
            ),
        )

    head_entry = _head_entry_for(root, normalized)
    if head_entry is None:
        return GitResult(
            returncode=-1,
            stdout="",
            stderr=(
                f"commit_authored_content: {normalized!r} does not exist in HEAD "
                f"({old_head}) -- this entrypoint is built for in-place mutation "
                "of an existing reserved-noun file, not for creating a new one"
            ),
        )
    mode_int, _old_sha = head_entry
    mode = f"{mode_int:06o}"

    hash_result = _hash_object_stdin_bytes(content.encode("utf-8"), normalized, cwd=root)
    if not hash_result.ok:
        return hash_result
    new_sha = hash_result.stdout.strip()

    # Bound 2 -- trailer tier-0 resolves from the CALLER-SUPPLIED
    # `deliverable_id`, never by opening `path` on disk.
    # `compute_missing_trailer_args(..., paths=None)` is the pre-tier-0
    # session-only resolution (Session-Id, plus Deliverable-Id only via
    # the session/claimed-plan tiers, never the artifact-first tier that
    # reads a committed path) -- passing `paths=[normalized]` here would
    # reintroduce exactly the worktree read this bound forbids. Computed
    # ONCE, ahead of the fast/ladder fork below: both arms need `msg_file`
    # to already carry every trailer before they read its final bytes for
    # the commit message body -- trailer computation itself is NOT part of
    # `_commit_via_head_spine`'s shared "spine rewrite -> commit object ->
    # CAS" contract (see that helper's own docstring).
    trailer_args = compute_missing_trailer_args(
        msg_file, root, paths=None,
        session_id_override=attributed_session_id,
    )
    msg_text_before = Path(msg_file).read_text(encoding="utf-8")
    message_already_has_deliverable_trailer = "Deliverable-Id:" in msg_text_before
    if deliverable_id:
        # Bound 2 -- an explicitly-passed `deliverable_id` is the tier-0
        # join key and wins over anything `compute_missing_trailer_args`
        # resolved from the session/claimed-plan tiers; drop that
        # session-resolved trailer pair rather than stacking both.
        trailer_args = _drop_trailer_arg(trailer_args, "Deliverable-Id")
        if not message_already_has_deliverable_trailer:
            trailer_args += ["--trailer", f"Deliverable-Id: {deliverable_id}"]

    interpret_result = _apply_trailers(msg_file, trailer_args, root)
    if interpret_result is not None:
        return interpret_result

    # Fast path: rewrite HEAD's tree spine in process, build the commit
    # object in process, land it via a locked ref CAS -- zero further git
    # spawns. `_commit_via_head_spine` returns `None` (take the ladder,
    # unchanged, below) when any of its own preconditions is unmet; a
    # non-`None` result -- success or a genuine failure (CAS lost, empty
    # tree) -- is this call's own outcome, never re-tried on the ladder.
    fast_result = _commit_via_head_spine(
        root, {normalized: (mode_int, new_sha)}, old_head, msg_file,
        caller="commit_authored_content",
    )
    if fast_result is not None:
        if not fast_result.ok:
            return fast_result
        new_commit_sha = fast_result.stdout.strip()
    else:
        # ---- fall-back: today's ladder, unchanged --------------------
        temp_index = Path(tempfile.gettempdir()) / f"git-index-{os.getpid()}-{uuid.uuid4().hex}"
        try:
            private_env: Dict[str, str] = dict(os.environ)
            private_env["GIT_INDEX_FILE"] = str(temp_index)

            # Bound 3 -- seeded straight from HEAD into a FRESH temp index,
            # never via `shutil.copy2` of the shared `.git/index`.
            read_tree_result = _git(["read-tree", "HEAD"], cwd=root, env=private_env)
            if not read_tree_result.ok:
                return read_tree_result

            cacheinfo_result = _git(
                ["update-index", "--add", "--cacheinfo", f"{mode},{new_sha},{normalized}"],
                cwd=root,
                env=private_env,
            )
            if not cacheinfo_result.ok:
                return cacheinfo_result

            write_tree_result = _git(["write-tree"], cwd=root, env=private_env)
            if not write_tree_result.ok:
                return write_tree_result
            tree_sha = write_tree_result.stdout.strip()
            empty_tree_refusal = _empty_private_index_refusal(
                tree_sha, root=root, caller="commit_authored_content"
            )
            if empty_tree_refusal is not None:
                return empty_tree_refusal

            msg_text = Path(msg_file).read_text(encoding="utf-8")
            subject_lines = msg_text.splitlines()
            subject = subject_lines[0] if subject_lines else "commit"

            commit_tree_result = _git(
                ["commit-tree", tree_sha, "-p", old_head, "-F", str(msg_file)],
                cwd=root,
                env=private_env,
            )
            if not commit_tree_result.ok:
                return commit_tree_result
            new_commit_sha = commit_tree_result.stdout.strip()

            # Bound 4 -- compare-and-swap; a concurrent HEAD move fails loud
            # rather than silently orphaning a peer commit.
            update_ref_result = _git(
                ["update-ref", "-m", subject, "HEAD", new_commit_sha, old_head],
                cwd=root,
            )
            if not update_ref_result.ok:
                return GitResult(
                    returncode=update_ref_result.returncode,
                    stdout=update_ref_result.stdout,
                    stderr=(
                        "commit_authored_content: compare-and-swap failed -- HEAD moved "
                        f"concurrently since {old_head} was captured; refusing to retry "
                        f"silently (a retry needs a tree rebuilt against the new HEAD). "
                        f"{update_ref_result.stderr}"
                    ),
                )
        finally:
            temp_index.unlink(missing_ok=True)

    # Bound 6 -- refresh the SHARED index for this one path only, so it
    # no longer reads as a staged reversion of the commit that just
    # landed (AC11). Inferred hazard, not a cited one -- see this
    # function's own docstring bound 6. Best-effort like bound 5's
    # auto-push replay below: the commit already landed via a
    # successful CAS by the time this runs, so a refresh failure must
    # never be reported as this function's own failure (that would
    # discard `new_commit_sha` for a write that genuinely succeeded and
    # could send a caller into a spurious retry/duplicate commit). Common
    # to both the fast and ladder arms -- the one spawn AC1 retains beyond
    # `hash-object`/`interpret-trailers`.
    _git(
        ["update-index", "--add", "--cacheinfo", f"{mode},{new_sha},{normalized}"],
        cwd=root,
    )

    # Bound 5 -- replay the post-commit auto-push; only after a
    # successful CAS, mirroring the trailer replay's own "replay what
    # the hook would have done" precedent.
    #
    # `path_list` is this entrypoint's own single `normalized` path (DR-344
    # kill-bar work, 2026-08-30): C4 of docs/plans/2026-08-26-the-commit-op-
    # stops-asking-git-eleven-times.md migrated `commit_scoped`'s two callers
    # and left this one on the `path_list=None` leg, where
    # `auto_push._release_claims_for_head` spends a `git show --name-only
    # HEAD` (measured 166ms of a 465ms `memo.transition claim`) relearning the
    # one path this function has held since its first line, plus a
    # `git status --porcelain` clean-check `--no-claim-release` then skips.
    _replay_post_commit_auto_push(
        root, [normalized], attributed_session_id,
    )

    # C11 (state/lessons/2026-08-18-a-ruling-applied-at-one-door-leaves-
    # the-siblings-unswept-7c3e1f9a4d22.yaml): this entrypoint is one of
    # the sibling commit producers C5 left unwired -- last step, after
    # the CAS has already landed, per `apply_base.record_ledger_entry`'s
    # own contract (never fails the commit it accompanies). Local
    # import: `contract.apply_base` transitively imports back into this
    # `ops.ceremony` package (`ops.review_brightline_gate`) -- a
    # module-level import here creates the same partially-initialized-
    # module cycle `commit_ledger.resolve_owner` already documents its
    # own deferred `baton_assemble` import to avoid.
    from coordinator_core.contract.apply_base import record_ledger_entry

    record_ledger_entry(
        root, [normalized], new_commit_sha,
        committer_id_override=attributed_session_id,
    )

    return GitResult(returncode=0, stdout=new_commit_sha, stderr="")


def commit_authored_new_file(
    path: str,
    content: str,
    msg_file: Union[str, Path],
    cwd: Union[str, Path],
    *,
    deliverable_id: Optional[str] = None,
    attributed_session_id: Optional[str] = None,
    record_ledger: bool = False,
    refresh_shared_index: bool = True,
) -> GitResult:
    """Commit EXACTLY `content` at `path`, where `path` is ABSENT from HEAD
    -- the creation sibling of `commit_authored_content`, which refuses an
    absent path by construction ("built for in-place mutation of an
    existing reserved-noun file, not for creating a new one"). Same
    signature, same `GitResult` contract, opposite HEAD precondition.

    **NO git spawn that runs a hook, and that is the contract, not an
    optimization.** The caller this exists for writes into a SIBLING
    repository. A spawn that reached that repo's `git commit` would run its
    `pre-commit` gates against our write and fire its `post-commit`
    auto-push -- the sole publisher in the production default -- as a side
    effect of delivering correspondence, an external-facing action nobody
    asked for. Every refusal below therefore returns a FAILING `GitResult`;
    NONE of them falls through to the spawning ladder
    `commit_authored_content` keeps, and this entrypoint never returns
    `None`.

    The commit itself lands at ZERO spawns on the FAST path. `git
    update-index` (`refresh_shared_index`, below) is the one spawn the fast
    path can issue -- but it is not a hard ceiling: `_head_entry_for` can
    fall back to `_git_state_head_blobs` (spawns `git ls-tree`, when
    `read_tree_spine` returns `None`) and `_resolve_commit_identity` can
    fall back to `_commit_identity_via_git_var` (spawns `git var
    GIT_COMMITTER_IDENT`, when no config-file identity resolves) -- either
    can add one more spawn on the way to landing. Neither fallback runs a
    hook, so the property above survives both intact: the bar is "nothing
    of the destination repo's runs on our behalf", not a spawn count; a
    spawn count is how the fast path is measured, not what the contract
    means.

    How the three spawns of the sibling entrypoint are retired:

      - `git hash-object -w --path=` -> `git_objects.write_object` with a
        `filter.*.clean` PRE-CHECK (see the clean-pipeline section below).
      - `git interpret-trailers` -> NOT RUN. `msg_file` must arrive with
        every trailer already final; its bytes become the commit message
        body verbatim. `deliverable_id`, when given, is therefore
        VALIDATED against those bytes, never injected into them -- an
        injection needs the spawn this entrypoint does not have.
      - `git update-index --add --cacheinfo` (the sibling's bound 6
        shared-index refresh) -> RETAINED, and it is the one spawn. See the
        shared-index section below for why it is not optional.

    Clean pipeline -- the bound that shapes the blob write. `write_object`
    writes bytes verbatim and its own Negative-spec forbids using it for a
    path-attributed blob, precisely because `git add` would first run that
    blob through `filter.*.clean`/`text`/`core.autocrlf`. This function
    discharges that bound by refusing the cases it cannot reproduce rather
    than writing a blob that differs from what git would have stored:

      - `content` containing a CR byte is REFUSED. The clean direction
        normalizes CRLF to LF under `text`/`core.autocrlf`; CR-free content
        is a fixed point of that normalization, so excluding CR makes the
        eol machinery provably a no-op instead of merely unlikely. (`eol=`
        acts on the smudge/checkout direction only -- see
        `_hash_object_stdin_bytes`, which pins that same correction.)
      - a repo-local attributes file whose `filter=` PATTERN MATCHES this
        path is REFUSED (`_clean_filter_may_apply`). Path-scoped, not
        repo-scoped: a repository that keeps `*.uasset` or `*.png` in LFS
        must stay deliverable for a markdown memo.

    Residual, stated rather than hidden: a `filter.*.clean` routed by a
    GLOBAL `core.attributesFile` is not detected -- reading it needs a
    config walk this budget cannot afford. Swept across all 16 registered
    peer repositories on 2026-08-25 (claude-klabauter-1c): not one sets
    `core.attributesFile` or `core.eol`, so nothing on this fleet needs it
    today. The caveat stays for the fleet member who adds one later; a
    caller committing into a repo that uses one must not use this
    entrypoint.

    `core.autocrlf=true`, which most of this fleet DOES set, is not a
    hazard here and must not be "handled": the clean direction normalizes
    CRLF to LF, so for the LF content this entrypoint requires, a verbatim
    blob and a filtered one are the same bytes. Code added to correct for
    it would create the divergence it was meant to prevent.

    `refresh_shared_index` (default `True`) -- `git update-index --add
    --cacheinfo` for this one path after the commit lands, the sibling
    entrypoint's bound 6. This defaults ON, and the default is the
    load-bearing part. There is no in-process index WRITER in
    `coordinator_core/git/` (`git_state.read_index` reads only), so without
    the spawn the committed path is present in HEAD, absent from the shared
    index, and present in the worktree -- which `git status` reports as, in
    claude-klabauter-1c's own reproduction:

        D  memo.md
        ?? memo.md

    A staged deletion of the file alongside an untracked copy of it. That
    is not a cosmetic status quirk: the next person in that repository runs
    a blanket `git add -A` or `git commit -a` and deletes the file we just
    delivered, as their own commit, in their own history. Skipping the
    refresh would write an armed defect into a tree we do not own. One
    hookless ~10ms spawn buys it back, so it is bought.

    Pass `False` ONLY when the caller has already refreshed that index
    itself. `update-index` runs no hook, so this spawn does not weaken the
    no-hooks contract above; a refresh failure is swallowed, never
    converted into a failure of a commit that already landed (the sibling's
    bound 6 reasoning, unchanged).

    `record_ledger` (default `False`, the OPPOSITE of
    `commit_authored_content`'s unconditional write) -- whether to append
    the commit-ledger entry. `record_ledger_entry(repo_root=...)` writes
    the ledger into whichever repo it is handed, so a cross-tree default of
    `True` would deposit our bookkeeping in the destination's `state/`.
    That is not merely unasked-for: DR-214's negative spec confines a memo
    delivery to the receiver's `cross-repo/inbox/` file and nothing else,
    so the ledger write would fall outside the carve-out the delivery is
    sanctioned under. A caller committing into its OWN repo should pass
    `True`.

    Post-commit auto-push is NEVER replayed here -- unconditional, with no
    flag to re-enable it, for the reason stated at the top.

    Returns a `GitResult`; on success `stdout` carries the new commit SHA,
    matching both sibling producers' contract.
    """
    if content is None:
        return GitResult(
            returncode=-1,
            stdout="",
            stderr="commit_authored_new_file: content is None -- refusing to commit an absent write",
        )

    root = Path(cwd)
    normalized = path.replace("\\", "/")
    if (
        not normalized
        or normalized.startswith("/")
        or ".." in Path(normalized).parts
        or _has_windows_drive(normalized)
    ):
        return GitResult(
            returncode=-1,
            stdout="",
            stderr=(
                f"commit_authored_new_file: {path!r} is not a repo-relative, "
                "in-worktree path -- refusing an absolute path or a `..` "
                "traversal segment"
            ),
        )
    target = root / normalized
    try:
        target.resolve().relative_to(root.resolve())  # fs-only: containment check, never stringified
    except ValueError:
        return GitResult(
            returncode=-1,
            stdout="",
            stderr=f"commit_authored_new_file: {path!r} resolves outside the worktree rooted at {root}",
        )
    if target.is_dir():
        return GitResult(
            returncode=-1,
            stdout="",
            stderr=f"commit_authored_new_file: {directory_pathspec_diagnostic(normalized)}",
        )

    if "\r" in content:
        return GitResult(
            returncode=-1,
            stdout="",
            stderr=(
                f"commit_authored_new_file: {normalized!r} content carries a CR byte -- "
                "this entrypoint writes the blob in process and cannot reproduce git's "
                "clean-direction CRLF normalization; normalize to LF before calling"
            ),
        )
    filter_diagnostic = _clean_filter_may_apply(root, normalized)
    if filter_diagnostic is not None:
        return GitResult(
            returncode=-1,
            stdout="",
            stderr=(
                f"commit_authored_new_file: refusing {normalized!r} -- {filter_diagnostic}; "
                "a zero-spawn blob write cannot run a clean filter, and writing the raw "
                "bytes would store a blob `git add` would not have produced"
            ),
        )

    old_head = _git_state_head_sha(root)
    if old_head is None:
        return GitResult(
            returncode=-1,
            stdout="",
            stderr=(
                "commit_authored_new_file: HEAD has no resolvable commit "
                "(unborn branch or a symref with no loose ref and no "
                "packed-refs entry) -- this entrypoint requires an existing "
                "HEAD commit to parent the new commit onto"
            ),
        )

    if _head_entry_for(root, normalized) is not None:
        return GitResult(
            returncode=-1,
            stdout="",
            stderr=(
                f"commit_authored_new_file: {normalized!r} already exists in HEAD "
                f"({old_head}) -- this entrypoint CREATES a path absent from HEAD; "
                "an in-place mutation of an existing file goes through "
                "commit_authored_content"
            ),
        )

    if deliverable_id:
        # Bound: validated, never injected -- injection is
        # `interpret-trailers`, the spawn this entrypoint does not have.
        message_value = _trailer_value(
            Path(msg_file).read_text(encoding="utf-8"), "Deliverable-Id:"
        )
        if message_value != deliverable_id:
            return GitResult(
                returncode=-1,
                stdout="",
                stderr=(
                    f"commit_authored_new_file: msg_file carries Deliverable-Id "
                    f"{message_value!r}, caller asserted {deliverable_id!r} -- this "
                    "entrypoint validates the trailer it is given and never rewrites "
                    "it; finalize msg_file before calling"
                ),
            )

    new_sha = write_object(
        resolve_git_common_dir(root), b"blob", content.encode("utf-8")
    )

    # A new path has no index entry and no HEAD tree entry, so the mode
    # ladder `_assemble_commit_tree_input` implements lands on
    # `_SUPPLIED_BLOB_MODE` by construction -- taken from that constant
    # rather than restated, so the two cannot drift apart.
    mode_int = int(_SUPPLIED_BLOB_MODE, 8)

    landed = _commit_via_head_spine(
        root, {normalized: (mode_int, new_sha)}, old_head, msg_file,
        create_missing_dirs=True,
        caller="commit_authored_new_file",
    )
    if landed is None:
        return GitResult(
            returncode=-1,
            stdout="",
            stderr=(
                "commit_authored_new_file: the in-process commit path declined a "
                f"precondition for {normalized!r} (unreadable HEAD tree spine, an "
                "unresolvable or peer-lock-held CAS ref, or no resolvable commit "
                "identity) -- refusing rather than falling back to the spawning "
                "ladder, which would run this repository's commit hooks"
            ),
        )
    if not landed.ok:
        return landed
    new_commit_sha = landed.stdout.strip()

    if refresh_shared_index:
        # The one spawn, and a hookless one -- see this function's own
        # `refresh_shared_index` section for the `D  path` / `?? path`
        # end state it exists to prevent. Best-effort exactly like the
        # sibling entrypoint's bound 6: the commit has already landed via
        # the CAS, so a refresh failure must never be reported as this
        # function's failure and send a caller into a duplicate commit.
        _git(
            ["update-index", "--add", "--cacheinfo", f"{_SUPPLIED_BLOB_MODE},{new_sha},{normalized}"],
            cwd=root,
        )

    if record_ledger:
        # Local import for the module-cycle reason `commit_authored_content`
        # documents at its own call site.
        from coordinator_core.contract.apply_base import record_ledger_entry

        record_ledger_entry(
            root, [normalized], new_commit_sha,
            committer_id_override=attributed_session_id,
        )

    return GitResult(returncode=0, stdout=new_commit_sha, stderr="")


def rev_parse_head(cwd: Union[str, Path]) -> GitResult:
    """HEAD's commit sha — post-commit SHA capture (Position A, no branch-tip fallback).

    ZERO SPAWNS. `git_state.head_sha()` answers this from `.git/HEAD` plus the
    loose ref or `packed-refs`, which is the same resolution `git rev-parse HEAD`
    performs, and the file reads cost no process. Eight production call sites
    reach this, several of them twice per commit (pre-commit capture, post-commit
    capture, post-push capture), so the spawn was recurring rather than incidental.

    Returns a synthesized `GitResult` rather than a bare string, because every
    caller branches on `.ok`/`.stdout` and this wrapper's contract is the
    envelope, not the mechanism. `stdout` carries the sha WITH a trailing
    newline, exactly as `git rev-parse` emits it -- callers `.strip()` it and one
    that sliced instead would otherwise see a silent off-by-one.

    UNBORN/UNRESOLVABLE HEAD returns `returncode=1`, matching what `git rev-parse
    HEAD` does on a repo with no commits (`ambiguous argument 'HEAD'`), so the
    failure branch every caller already has stays reachable. `head_sha()` returns
    None for exactly that case and never raises on a missing HEAD.

    Deliberately NOT memoised, unlike `git_state.head_blobs`: this function's
    whole job at three of its call sites is to observe that HEAD MOVED. A cache
    keyed on HEAD would be either useless or wrong here, and the read is already
    two small file opens.
    """
    sha = _git_state_head_sha(cwd)
    if not sha:
        return GitResult(
            returncode=1,
            stdout="",
            stderr="rev_parse_head: HEAD does not resolve to a commit",
        )
    return GitResult(returncode=0, stdout=sha + "\n", stderr="")


def log_grep(cwd: Union[str, Path], grep_pattern: str, *, extra_args: Optional[Sequence[str]] = None) -> GitResult:
    """`git log --grep=<pattern> [extra_args]` — Session-Id / trailer history lookups."""
    args = ["log", f"--grep={grep_pattern}", *(extra_args or [])]
    return _git(args, cwd=cwd)


def log_diff_filter(
    cwd: Union[str, Path], diff_filter: str, *, extra_args: Optional[Sequence[str]] = None
) -> GitResult:
    """`git log --diff-filter=<X> [extra_args]` — swept-rename history reconstruction."""
    args = ["log", f"--diff-filter={diff_filter}", *(extra_args or [])]
    return _git(args, cwd=cwd)


def remote(cwd: Union[str, Path]) -> GitResult:
    """`git remote` — list configured remotes (push-retry preflight)."""
    return _git(["remote"], cwd=cwd)


def push(
    cwd: Union[str, Path],
    *,
    remote_name: Optional[str] = None,
    timeout: float = REMOTE_BUDGET_SECS,
) -> GitResult:
    """`git push [<remote_name>]` — push-with-retry pipeline (C4).

    The default is the ONE runaway guard DR-349 names for a genuinely-remote
    leg (`coordinator_core.git.run :: REMOTE_BUDGET_SECS`), not a number of
    this module's own. It was a bare `120` until 2026-08-26, which was a DEAD
    number rather than a lenient one: every caller reaches this through
    `push_with_retry`, and that ladder is itself bounded by
    `ipc._timeout_for`'s dispatch guard at 30s or less, so a 120s per-attempt
    timeout could never fire. It read as a bound and was not one. Callers that
    pass `budget_secs` to `push_with_retry` now size this from their own
    remaining deadline and never reach the default at all."""
    args = ["push"]
    if remote_name:
        args.append(remote_name)
    return _git(args, cwd=cwd, timeout=timeout)


def fetch(
    cwd: Union[str, Path], remote_name: str, *, timeout: float = REMOTE_BUDGET_SECS
) -> GitResult:
    """`git fetch <remote_name>` — reject-detection preflight before rebase --onto.

    Same default, same reasoning as `push` above: DR-349's remote runaway
    guard, never a per-module literal that no caller could ever reach."""
    return _git(["fetch", remote_name], cwd=cwd, timeout=timeout)


def rebase_onto(
    cwd: Union[str, Path], upstream_ref: str, merge_base: str, branch: str = "HEAD"
) -> GitResult:
    """`git rebase --onto <upstream_ref> <merge_base> [<branch>]` — push-retry rebase step.

    Latent-bug fix (C3b, 2026-08-08): the literal `"HEAD"` default used to be
    passed through as git's own `<branch>` positional argument. Per git's own
    semantics that argument, when supplied, is checked out BEFORE the rebase
    runs -- and `git checkout HEAD` detaches, since `HEAD` resolves to a
    commit, not a branch name. Every genuine reject-triggered retry
    (`push_with_retry`'s fetch+rebase+re-push cycle) therefore left the
    worktree in detached-HEAD state after a successful rebase, and the
    re-push that follows failed outright ("You are not currently on a
    branch") -- silently corrupting the one path C3b's pushed-range retry
    logic exists to cover. `branch == "HEAD"` (the sentinel every existing
    caller passes) now omits the positional argument, which is git's own
    2-argument `--onto` form and operates on the current branch WITHOUT
    checking anything out -- the behaviour every caller already assumed.
    Any other explicit branch name still passes through unchanged.
    """
    args = ["rebase", "--onto", upstream_ref, merge_base]
    if branch != "HEAD":
        args.append(branch)
    return _git(args, cwd=cwd)


def rebase_abort(cwd: Union[str, Path]) -> GitResult:
    """`git rebase --abort` — push-retry failure cleanup."""
    return _git(["rebase", "--abort"], cwd=cwd)


def merge_base(cwd: Union[str, Path], ref_a: str, ref_b: str) -> GitResult:
    """`git merge-base <ref_a> <ref_b>` — push-retry rebase-onto preflight."""
    return _git(["merge-base", ref_a, ref_b], cwd=cwd)


def merge_base_is_ancestor(
    cwd: Union[str, Path], ancestor_ref: str, descendant_ref: str
) -> GitResult:
    """`git merge-base --is-ancestor <ancestor_ref> <descendant_ref>` — the
    reachability half of the push ladder's reject recovery.

    THE RETURN CODE IS THE ANSWER, NOT AN ERROR: git exits 0 for "yes,
    reachable", 1 for "no", and 128 (or anything else) for a genuine
    failure -- a bad ref, an unreadable object store. Callers MUST branch on
    all three; treating any non-zero as "no" folds a broken repo into a
    confident negative, which on the push path would send a caller into a
    rebase it did not need. `GitResult.ok` alone cannot express this, so it
    is deliberately not the interface here.
    """
    return _git(["merge-base", "--is-ancestor", ancestor_ref, descendant_ref], cwd=cwd)


def rev_parse_upstream(cwd: Union[str, Path]) -> GitResult:
    """`git rev-parse --abbrev-ref --symbolic-full-name @{u}` — resolve the tracked upstream ref."""
    return _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd=cwd)


def rev_parse(cwd: Union[str, Path], ref: str) -> GitResult:
    """`git rev-parse <ref>` — resolve an arbitrary ref/name to its sha.

    Added for C3b (pushed-range reporting, `commit_pipeline.push_with_retry`):
    `rev_parse_upstream` above only names the upstream ref (e.g.
    `"origin/main"`); this resolves that name to a sha so the pre-push and
    post-push tips can be diffed into a `<old>..<new>` range.
    """
    return _git(["rev-parse", ref], cwd=cwd)


def rev_list_count(cwd: Union[str, Path], range_spec: str) -> GitResult:
    """`git rev-list --count <range_spec>` — commit count for a `<old>..<new>` range.

    Added for C3b (pushed-range reporting): the count half of "what did a
    landed push actually push".
    """
    return _git(["rev-list", "--count", range_spec], cwd=cwd)


def replay_onto_print(
    cwd: Union[str, Path], upstream_ref: str, merge_base_sha: str, branch_ref: str
) -> GitResult:
    """`git replay --ref-action=print --onto <upstream_ref> <merge_base>..<branch_ref>`
    — the worktree-free half of the push ladder's diverged-branch recovery.

    `git rebase --onto` needs a clean worktree because it checks the result
    out; on this fleet's shared worktree a peer always has something
    uncommitted, so that recovery could never run (see
    `push._replay_onto_fetched_ref` for the full account). `git replay`
    computes the replayed chain entirely in the object database — it reads
    neither the index nor the working tree — so a dirty tree is not its
    concern at all.

    `--ref-action=print` is LOAD-BEARING, not cosmetic: git's own default for
    `replay` is `update`, which writes the new tip straight into the branch
    ref and leaves the index and working tree describing the OLD tip — a
    silently desynchronized shared worktree, which on this fleet is the worst
    outcome available. Printing hands the caller `update <ref> <new> <old>`
    on stdout and updates nothing, so the caller can materialize the change
    into the worktree first (`read_tree_merge_update`) and move the ref only
    once that succeeded.

    A git too old to know the subcommand exits non-zero with its own usage
    diagnostic; the caller reads that as "recovery unavailable" and reports
    the push failure it would have reported before this path existed.
    """
    return _git(
        [
            "replay",
            "--ref-action=print",
            "--onto",
            upstream_ref,
            f"{merge_base_sha}..{branch_ref}",
        ],
        cwd=cwd,
    )


def read_tree_merge_update(cwd: Union[str, Path], old_sha: str, new_sha: str) -> GitResult:
    """`git read-tree -m -u <old_sha> <new_sha>` — two-way merge of the
    index and working tree from one commit's tree to another's, WITHOUT the
    clean-tree precondition a checkout imposes.

    THE REFUSAL IS THE FEATURE. Files that differ between the two trees are
    updated; files that do not are left exactly as they are, uncommitted peer
    edits and staged peer entries included. If a path that differs between
    the trees is locally modified, staged, or shadowed by an untracked file,
    git refuses the whole operation (`Entry '<path>' not uptodate. Cannot
    merge.` / `Untracked working tree file '<path>' would be overwritten by
    merge.`) and writes NOTHING — index and worktree are left byte-identical
    to before the call. That is precisely the guarantee that makes this
    usable on a worktree a dozen peer sessions are writing to: the only two
    outcomes are "landed, nobody else's work touched" and "declined, nothing
    touched at all".

    Callers MUST move the branch ref (`update_ref`) immediately after a
    successful call and roll this back if that fails — between the two, the
    index describes `new_sha` while HEAD still names `old_sha`, which reads
    as a large staged diff to anything that looks at the tree.
    """
    return _git(["read-tree", "-m", "-u", old_sha, new_sha], cwd=cwd)


def update_ref(
    cwd: Union[str, Path], ref: str, new_sha: str, old_sha: str
) -> GitResult:
    """`git update-ref <ref> <new_sha> <old_sha>` — compare-and-swap ref move.

    `old_sha` is git's own expected-current-value argument, never optional
    here: on a shared worktree a peer can commit between the moment a caller
    read the tip and the moment it writes one, and an unconditional ref write
    would silently discard that commit. Supplying it makes git refuse the
    update instead.
    """
    return _git(["update-ref", ref, new_sha, old_sha], cwd=cwd)
