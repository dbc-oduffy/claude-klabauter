"""
coordinator_core.ops.ceremony.git_native — Windows-safe shared git-subprocess helper.

Purpose: every native `git` subprocess invocation in the `wsc_tail` rebuild routes
through the single `_git()` helper in this module, so the Windows-safe subprocess
flags are carried exactly once instead of being re-typed (and re-forgotten) at each
of the ~13 call sites the OLD `wsc_commit.py` scattered them across with none of
these flags present (the portability gap this chunk closes).

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
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union

from coordinator_core.git.commit_trailers import compute_missing_trailer_args
from coordinator_core.git.divergence import DivergenceCheckFailed, diverging_paths
from coordinator_core.git_lock_retry import run_with_lock_retry
from coordinator_core.win_portability import no_console_creationflags

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

#: Character budget for the pathspec batch handed to a single `diverging_
#: paths()` `git diff` subprocess call from `commit_scoped()`'s own
#: divergence check, sized well under the Windows `CreateProcess` command-
#: line cap (32767 UTF-16 code units) with generous headroom for the
#: `git.exe` path itself, the fixed `diff --cached --name-only --` argv
#: prefix, per-argument quoting overhead around any path containing a
#: space, and the second, unfiltered `git diff --name-only --` call
#: `diverging_paths()` also issues against the same pathspec. Same value
#: and reasoning as `commit_pipeline._DIVERGENCE_CHECK_ARGV_BUDGET_CHARS`
#: (a percolate-publish batch, ~2000-2700 paths, blows the raw 32767 cap
#: outright on one argv -- `rc=127`, "divergence indeterminate") --
#: promoted here (2026-08-15) as the shared home for `_chunk_paths()`
#: itself, so `commit_pipeline.py` imports both rather than growing a
#: second, independently-drifting copy. See `_chunk_paths()`'s own
#: docstring for why this module, not `commit_pipeline.py`, is the shared
#: home: `commit_pipeline.py` already imports `git_native` (this module),
#: so the reverse import direction would be circular.
_DIVERGENCE_CHECK_ARGV_BUDGET_CHARS = 6000


def _chunk_paths(
    paths: Sequence[str], *, budget_chars: int = _DIVERGENCE_CHECK_ARGV_BUDGET_CHARS
) -> List[List[str]]:
    """Pack `paths` into argv-safe chunks, each bounded by `budget_chars`.

    Promoted here (2026-08-15) from `commit_pipeline.py` (where it was
    originally extracted from `_diverging_paths_chunked`, that module's
    first caller of this packing shape) so `commit_scoped()`'s own chunked
    `diverging_paths()` divergence check (below) can reuse the identical
    packer and budget instead of forking a second, subtly-different copy.
    `commit_pipeline.py` cannot host the shared copy itself: it already
    imports this module (`from coordinator_core.ops.ceremony import
    git_native`), so a `git_native` -> `commit_pipeline` import back would
    be circular. Behaviour is unchanged from the original -- this is a
    promotion, not a rewrite; `commit_pipeline.py` now imports `_chunk_
    paths`/`_DIVERGENCE_CHECK_ARGV_BUDGET_CHARS` from here instead of
    defining its own.

    Packing is greedy and order-preserving: a path never crosses a chunk
    boundary, and no chunk exceeds `budget_chars` (a single path longer
    than the budget still gets its own one-path chunk rather than being
    dropped or truncated -- callers must not assume every chunk is
    non-trivially sized). Returns `[]` for an empty `paths`, never a
    single empty chunk.
    """
    if not paths:
        return []

    chunks: List[List[str]] = []
    chunk: List[str] = []
    chunk_chars = 0
    for p in paths:
        added = len(p) + 1
        if chunk and chunk_chars + added > budget_chars:
            chunks.append(chunk)
            chunk = []
            chunk_chars = 0
        chunk.append(p)
        chunk_chars += added
    if chunk:
        chunks.append(chunk)
    return chunks


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


# ---------------------------------------------------------------------------
# Thin typed wrappers — one per git subcommand the wsc_tail rebuild needs.
# Each routes through `_git()`; none constructs its own subprocess.run call.
# ---------------------------------------------------------------------------


def status_porcelain(cwd: Union[str, Path]) -> GitResult:
    """`git status --porcelain` — dirty-tree gate classification (C3).

    `--no-optional-locks` (pre-subcommand, per `git`'s placement rule)
    suppresses the opportunistic stat-cache write-back a bare `git status`
    takes `.git/index.lock` for — contention noise on this shared worktree.
    Read-only call; never applied to a writing invocation."""
    return _git(["--no-optional-locks", "status", "--porcelain"], cwd=cwd)


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
    stdin_bytes = ("\n".join(objects) + "\n").encode("utf-8")
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "cat-file", "--batch"],
        input=stdin_bytes,
        capture_output=True,
        **no_console_creationflags(),
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
    """`git add -- <paths>` — explicit-pathspec stage (never a bare `git add -A`/`.`).

    Also the correct staging call for a DELETION: per `git-add(1)`, an
    explicit pathspec naming a tracked file that has been removed from the
    working tree stages the removal (records the deletion in the index) —
    this has been git's default behaviour for an explicit path since older
    Git's `--no-all`-required era ended; only a fileglob/directory pathspec
    ever needed `-u`/`-A` to pick up removals. Callers do not need a
    separate "stage this deletion" primitive.
    """
    return _git(["add", "--", *paths], cwd=cwd)


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
    refusal AND `commit_pipeline.run_commit_pipeline()`'s pre-stage guard
    (session fb5fa766, 2026-07-31 incident: a directory pathspec reached
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


def commit_with_message_file(
    cwd: Union[str, Path], msg_file: Union[str, Path], paths: Sequence[str]
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
    """
    return _git(["commit", "-F", str(msg_file), "--", *paths], cwd=cwd)


def _write_pathspec_file(root: Union[str, Path], paths: Sequence[str]) -> Path:
    """Write `paths` to a uniquely-named temp file, one per line, and return
    its path -- the newline-delimited input `git ... --pathspec-from-file=<f>`
    expects by default (git also offers `--pathspec-file-nul` for a NUL-
    delimited feed; this module picks newline-delimited throughout, both
    here and in every reader of a file this function produces, since no
    path this module ever commits/stages contains a literal newline byte
    and the plain-text form is easier to inspect if a call ever needs
    debugging). Same PID+uuid uniqueness convention `stage_from_patch()`'s
    own `temp_index` already uses, for the same reason: two concurrent
    sessions on this shared machine must never collide on the same temp
    filename. UTF-8, trailing newline after the last path (matches git's
    own `--pathspec-from-file=-` stdin convention). Caller owns cleanup
    (`finally: pathspec_file.unlink(missing_ok=True)`), mirroring the
    `-F <msgfile>` flow's own discipline elsewhere in this module.
    """
    pathspec_file = (
        Path(tempfile.gettempdir())
        / f"git-pathspec-{os.getpid()}-{uuid.uuid4().hex}.txt"
    )
    pathspec_file.write_text("\n".join(paths) + "\n", encoding="utf-8")
    return pathspec_file


def add_paths_pathspec_file(cwd: Union[str, Path], paths: Sequence[str]) -> GitResult:
    """`git add --pathspec-from-file=<f>` — the percolate-publish-scale-safe
    sibling of `add_paths()` (which puts the whole `paths` list on argv).

    `commit_scoped()`'s own agree branch is the sole caller (2026-08-15,
    the last of the five argv-length sites this repo's commit path had --
    siblings `ef84c2ee9`/`fe0f4eb84`/`25268ed33`/`47e8defbb` closed the
    other four): staging is not chunked here, unlike `_diverging_paths_
    chunked()` above, because `git add --pathspec-from-file=<f>` is
    empirically SUPPORTED (verified against this machine's git
    2.55.0.windows.3, in a scratch repo -- unlike `git diff`, which
    rejects `--pathspec-from-file` outright) -- one pathspec-file call
    replaces one argv-list call with no chunking needed, and no atomicity
    concern either (staging is not the commit itself).
    """
    root = Path(cwd)
    pathspec_file = _write_pathspec_file(root, paths)
    try:
        return _git(["add", f"--pathspec-from-file={pathspec_file}"], cwd=root)
    finally:
        pathspec_file.unlink(missing_ok=True)


def commit_with_message_file_pathspec_scoped(
    cwd: Union[str, Path], msg_file: Union[str, Path], paths: Sequence[str]
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
    """
    root = Path(cwd)
    pathspec_file = _write_pathspec_file(root, paths)
    try:
        return _git(
            ["commit", "-F", str(msg_file), f"--pathspec-from-file={pathspec_file}"],
            cwd=root,
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
    """Return the (stripped) value of the first line in `msg_text` starting
    with `prefix` (e.g. `"Deliverable-Id:"`), or `None` if no such line
    exists. Same prefix-match convention as `coordinator_core.git.
    commit_trailers._has_trailer_line` (a plain `str.startswith`, not a
    `git interpret-trailers --parse` round-trip), widened here to return the
    VALUE rather than a bool -- `commit_scoped`'s precedence ruling (2) needs
    to compare an existing message trailer's value against an explicit
    caller-supplied `deliverable_id`, not merely know a trailer is present.
    """
    for line in msg_text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return None


def _check_deliverable_id_precedence(
    msg_text: str, deliverable_id: str, equivalence_map: Optional[dict] = None
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

    Raises `coordinator_core.ops.deliverable_carry.DivergentDeliverableIdError`
    -- (iii) an existing line DISAGREES. FAIL LOUD, never silently pick
    either side (reuses the existing exception verbatim, per that class's
    own negative-spec against forking a second copy). Both `commit_scoped`
    branches propagate this raised, uncaught -- a deliberate, NAMED
    exception to this module's usual "every wrapper returns a GitResult,
    never raises" convention (see the PM ruling text itself: "FAIL LOUD by
    raising the existing ... DivergentDeliverableIdError").
    """
    existing = _trailer_value(msg_text, "Deliverable-Id:")
    if existing is None:
        return True

    from coordinator_core.ops.deliverable_equivalence import canonicalize

    equivalence_map = equivalence_map or {}
    if canonicalize(existing, equivalence_map) == canonicalize(deliverable_id, equivalence_map):
        return False

    from coordinator_core.ops.deliverable_carry import DivergentDeliverableIdError

    raise DivergentDeliverableIdError(
        f"commit_scoped: caller-supplied deliverable_id {deliverable_id!r} "
        f"disagrees with the commit message's own pre-existing Deliverable-Id "
        f"trailer {existing!r} (most likely stamped by commit_anchors.py from "
        "staged plan frontmatter) -- refusing to silently pick either side. "
        "A caller asserting one deliverable while staging another's "
        "already-stamped artifact is an authoring error fixed by splitting "
        "the commit, per DR-207 DD#1's earliest-artifact-wins ruling (see "
        "DivergentDeliverableIdError's own docstring)."
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


def _index_blobs(root: Path, paths: Sequence[str]) -> Dict[str, object]:
    """Return `{path: index-blob-sha}` for `paths`, via `git ls-files -s -z`.
    A path with no index entry (untracked, or deleted-from-index) maps to
    `None`. On a `git` failure, EVERY entry maps to `_GIT_READ_FAILED` --
    NOT `None` -- so a caller comparing against a prior snapshot can never
    read a degraded read as "genuinely absent" (code-reviewer finding,
    bf7bab8ce37c review, P1 "degraded reads": the previous all-`None`
    degrade was claimed refuse-leaning but was not -- see
    `_agree_branch_cas_refusal`'s own use of this sentinel for the two
    directions that claim was false in).

    `-z` (never the newline-separated default) means git prints each path
    UNQUOTED, and the result is reconciled back to the caller's own key
    string via `_normalize_path_key` (a `./`-prefix or backslash-separator
    difference no longer leaves the caller's key permanently unmatched --
    code-reviewer finding, bf7bab8ce37c review, P1 "path-key mismatch"). A
    printed path that still cannot be reconciled to any caller key is kept
    under its own git-printed key AS DIAGNOSTICS (visible, never dropped),
    and every caller path it could plausibly answer for is forced to
    `_GIT_PATH_UNRECONCILED` (never left at `None`) -- see
    `_GIT_PATH_UNRECONCILED`'s own docstring for the case-divergence hazard
    this closes (PM follow-up, bf7bab8ce37c review P1).

    Chunked (2026-08-15, `_chunk_paths()` -- same packer/budget `_diverging_
    paths_chunked()` uses, promoted here from `commit_pipeline.py`): this
    function's own `pre_index_blobs`/`current_index_blobs` snapshot reads,
    taken by `commit_scoped()`'s Layer-1 CAS (`_agree_branch_cas_refusal`)
    BEFORE and immediately before the agree branch's own `git add`, put the
    WHOLE `paths` list on `git ls-files -s -z --` argv -- at percolate-
    publish scale this blows the same 32767-char Windows `CreateProcess` cap
    the divergence check does, and a `git` failure here degrades EVERY path
    to `_GIT_READ_FAILED`, which `_agree_branch_cas_refusal` reads as
    "moved" -- so an unchunked call here would make the agree branch refuse
    EVERY commit at this scale, not just fail to detect a real race.
    Chunking is per-chunk-failure, not whole-batch: a `git` failure in one
    chunk marks only that chunk's paths `_GIT_READ_FAILED` (mirrors
    `commit_pipeline._ls_files_deleted_chunked`'s own established per-chunk-
    failure idiom), so an argv-length artifact in a later chunk no longer
    taints paths a healthy earlier chunk already answered for. The `:(icase)`
    rescan (`still_null`, below) is chunked identically, for the same reason.
    """
    if not paths:
        return {}
    blobs: Dict[str, object] = {p: None for p in paths}
    key_by_normalized = {_normalize_path_key(p): p for p in paths}
    matched: Set[str] = set()
    unreconciled: List[Tuple[str, str]] = []
    for chunk in _chunk_paths(list(paths)):
        result = _git(["ls-files", "-s", "-z", "--", *chunk], cwd=root)
        if not result.ok:
            for p in chunk:
                blobs[p] = _GIT_READ_FAILED
            continue
        for parts, path in _parse_git_z_cacheinfo(result.stdout):
            if len(parts) != 3:
                continue
            mode, sha, _stage = parts
            norm = _normalize_path_key(path)
            key = key_by_normalized.get(norm)
            if key is not None:
                blobs[key] = sha
                matched.add(norm)
            else:
                blobs[path] = sha
                unreconciled.append((path, sha))

    still_null = [p for p in paths if blobs[p] is None]
    for rescan_chunk in _chunk_paths(still_null):
        rescanned = _case_insensitive_rescan(root, ["ls-files", "-s", "-z"], rescan_chunk)
        if rescanned is None:
            for p in rescan_chunk:
                blobs[p] = _GIT_READ_FAILED
        else:
            for parts, path in rescanned:
                if len(parts) != 3:
                    continue
                unreconciled.append((path, parts[1]))

    if unreconciled:
        _mark_unreconciled(blobs, paths, matched, unreconciled)
    return blobs


def _head_blobs(root: Path, paths: Sequence[str]) -> Dict[str, object]:
    """Return `{path: HEAD-blob-sha}` for `paths`, via `git ls-tree HEAD -z`.
    A path absent from HEAD (new/untracked) maps to `None`. Same
    `_GIT_READ_FAILED`-on-failure, `-z`+`_normalize_path_key`-reconciliation
    posture as `_index_blobs` above -- see that function's own docstring;
    both route through the shared `_parse_git_z_cacheinfo` primitive so a
    porcelain-format fix never has to be applied to one and re-forgotten on
    the other (code-reviewer finding, bf7bab8ce37c review, P2).

    One legitimate non-`git`-failure cause of `not result.ok` here: an
    UNBORN branch (no commit has ever landed on this ref yet -- the very
    first `commit_scoped()` call in a fresh repo, exercised by this repo's
    own `test_op_schema_accepts_string_deliverable_id`). Git's stable
    diagnostic for that case is `"fatal: Not a valid object name HEAD"`
    (verified empirically against this repo's own git, 2026-08-14) --
    matched here and treated as the ordinary "every path absent from HEAD"
    answer (`None`, not `_GIT_READ_FAILED`), since there genuinely is no
    HEAD tree to have failed to read. Any OTHER non-zero exit (git missing,
    corrupt repo, timeout, permission error) still maps to
    `_GIT_READ_FAILED`.

    Deliberately has NO `:(icase)` rescan of its own, unlike `_index_blobs`
    -- `git ls-tree` REJECTS that pathspec magic outright (`fatal: pathspec
    magic not supported by this command: 'icase'`, verified empirically,
    2026-08-14; an earlier version of this fix tried it here and broke
    every ordinary new-file commit, since the rescan's own git failure was
    then indistinguishable from a real `_GIT_READ_FAILED`). Not a gap: the
    case-divergence hazard this exists to catch (`git add` silently reusing
    an EXISTING INDEX entry under a different case) is entirely an
    index-side fact -- `_index_blobs`'s own `:(icase)` rescan already forces
    `_GIT_PATH_UNRECONCILED` into `pre_index_blobs`/`current_index_blobs`
    for that path, which `_agree_branch_cas_refusal` already refuses on
    before `_head_blobs`'s answer for that same path is ever consulted.

    Chunked (2026-08-15) for the identical percolate-publish-scale argv-
    length reason `_index_blobs()`'s own docstring documents -- same
    `_chunk_paths()` packer, same per-chunk-failure posture (a `git`
    failure in one chunk marks only that chunk's paths `_GIT_READ_FAILED`,
    never the whole batch). The unborn-HEAD case (`"not a valid object name
    head"`) is checked PER CHUNK, not once up front: an unborn branch fails
    identically on every chunk's own `git ls-tree HEAD` call, so this is
    behaviour-preserving (every path still resolves to the ordinary
    "absent from HEAD" `None`, never `_GIT_READ_FAILED`) while staying
    chunk-local like every other failure branch here.
    """
    if not paths:
        return {}
    blobs: Dict[str, object] = {p: None for p in paths}
    key_by_normalized = {_normalize_path_key(p): p for p in paths}
    matched: Set[str] = set()
    unreconciled: List[Tuple[str, str]] = []
    for chunk in _chunk_paths(list(paths)):
        result = _git(["ls-tree", "HEAD", "-z", "--", *chunk], cwd=root)
        if not result.ok:
            if "not a valid object name head" in result.stderr.strip().lower():
                continue
            for p in chunk:
                blobs[p] = _GIT_READ_FAILED
            continue
        for parts, path in _parse_git_z_cacheinfo(result.stdout):
            if len(parts) != 3:
                continue
            _mode, _type, sha = parts
            norm = _normalize_path_key(path)
            key = key_by_normalized.get(norm)
            if key is not None:
                blobs[key] = sha
                matched.add(norm)
            else:
                blobs[path] = sha
                unreconciled.append((path, sha))
    if unreconciled:
        _mark_unreconciled(blobs, paths, matched, unreconciled)
    return blobs


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
    current_index_blobs = _index_blobs(root, path_list)
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
            "commit_scoped: compare-and-swap refused -- "
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

#: Mode recorded for a supplied-blob cacheinfo entry until a real producer
#: exists. C2 (docs/plans/2026-08-14-the-tool-stages-what-it-commits.md)
#: introduces `stage_from_patch()`, the first caller of `supplied_blobs`;
#: this chunk only wires the resolution's plumbing through with an empty
#: default, so no real mode has ever been observed here yet. `100644`
#: (ordinary non-executable file) matches every path this repo's own
#: `--stage-patch` use case targets; C2 owns deciding whether a supplied
#: blob ever needs to preserve an executable bit.
_SUPPLIED_BLOB_MODE = "100644"


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
    no read against the repository's own index/worktree/HEAD. Used by
    `commit_pipeline.run_commit_pipeline()` to decide, BEFORE staging runs,
    which of the caller's `stage_paths` a `stage_patch` will cover (and must
    therefore never see an ordinary `git add`) vs. which fall through to
    today's staged-or-worktree route unchanged (and DO need an ordinary
    `git add` so the dirty-tree gate can attribute them).

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


def _commit_scoped_private_index(
    diverged: Sequence[str],
    non_diverged: Sequence[str],
    msg_file: Union[str, Path],
    cwd: Union[str, Path],
    deliverable_id: Optional[str] = None,
    *,
    supplied_blobs: Optional[Dict[str, str]] = None,
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
    """
    root = Path(cwd)
    supplied_blobs = supplied_blobs or {}
    resolution = _resolve_content_sources(diverged, non_diverged, supplied_blobs)
    staged_paths = [p for p in diverged if resolution[p] == _SOURCE_STAGED]
    worktree_paths = [p for p in non_diverged if resolution[p] == _SOURCE_WORKTREE]
    supplied_paths = [p for p in resolution if resolution[p] == _SOURCE_SUPPLIED]

    old_head_result = _git(["rev-parse", "HEAD"], cwd=root)
    if not old_head_result.ok:
        return old_head_result
    old_head = old_head_result.stdout.strip()

    # Capture the CURRENTLY STAGED blob for each staged-blob-source path
    # (`staged_paths`, resolved above) from the REAL index, before any
    # mutation -- this is the deliberately-staged content (partial hunk,
    # etc.) that must survive VERBATIM, never re-derived from the worktree
    # (that would reproduce the exact bug this selector exists to close).
    cacheinfo_entries: List[_CacheInfoEntry] = []
    if staged_paths:
        ls_files_result = _git(["ls-files", "-s", "--", *staged_paths], cwd=root)
        if not ls_files_result.ok:
            return ls_files_result
        cacheinfo_entries = _parse_ls_files_cacheinfo(ls_files_result.stdout)

    # Supplied-blob-source paths (`supplied_paths`, empty until C2's
    # `stage_from_patch()` populates `supplied_blobs`) never touch `git
    # ls-files` at all -- the blob was already written by the caller, and
    # the resolution above guarantees a supplied path can never ALSO be a
    # `staged_paths`/`worktree_paths` member, so appending here is safe
    # regardless of statement order (see `_resolve_content_sources`'s own
    # docstring for why this is now a stated property, not an ordering
    # accident).
    for p in supplied_paths:
        # Negative spec: a non-empty `supplied_paths` with no `supplied_blobs`
        # is a broken resolution, not a recoverable state -- KeyError here is
        # the loud failure. Never append a None sha: `update-index
        # --cacheinfo` would take it as a literal and stage garbage.
        blob = (supplied_blobs or {})[p]
        cacheinfo_entries.append((_SUPPLIED_BLOB_MODE, blob, p))

    temp_index = Path(tempfile.gettempdir()) / f"git-index-{os.getpid()}-{uuid.uuid4().hex}"

    try:
        private_env: Dict[str, str] = dict(os.environ)
        private_env["GIT_INDEX_FILE"] = str(temp_index)

        # Seed the private index straight from HEAD into this FRESH temp
        # index, never via `shutil.copy2` of the shared `.git/index` --
        # DR-272 § 3.4 (drift 2): the shared-index copy this function used
        # to do was a genuine violation of form 2's own parenthetical
        # ("initialized from HEAD (never from the shared worktree index)"),
        # corrected here to match the idiom `commit_authored_content`
        # already uses. This also removes the prior `reset -q HEAD -- .`
        # cwd-relative subdirectory hazard and the uncaught
        # `FileNotFoundError` when `.git/index` did not yet exist.
        read_tree_result = _git(["read-tree", "HEAD"], cwd=root, env=private_env)
        if not read_tree_result.ok:
            return read_tree_result

        # Worktree-source paths (`worktree_paths` -- staged==worktree, or
        # newly-added) are safe to (re-)stage straight from the worktree. A
        # staged-deletion path is covered too WITHOUT `-A`: the `reset -q
        # HEAD` above resurrects it in THIS PRIVATE copy (HEAD still has
        # it), so it exists in the private index even though the worktree
        # lacks it -- `git add -- path` stages a removal for a path present
        # in the index but missing from the worktree (verified empirically;
        # `-A` is only needed when a path is untracked/gone from BOTH,
        # which is not this shape post-reset).
        if worktree_paths:
            add_result = _git(["add", "--", *worktree_paths], cwd=root, env=private_env)
            if not add_result.ok:
                return add_result

        # Staged-blob- and supplied-blob-source paths are restored from
        # `cacheinfo_entries`, never from the worktree.
        for mode, sha, path in cacheinfo_entries:
            cacheinfo_result = _git(
                ["update-index", "--add", "--cacheinfo", f"{mode},{sha},{path}"],
                cwd=root,
                env=private_env,
            )
            if not cacheinfo_result.ok:
                return cacheinfo_result

        write_tree_result = _git(["write-tree"], cwd=root, env=private_env)
        if not write_tree_result.ok:
            return write_tree_result
        tree_sha = write_tree_result.stdout.strip()

        # `commit-tree` is plumbing -- it runs NO git hooks, so the
        # `prepare-commit-msg` hook that stamps Session-Id/Deliverable-Id on
        # every ordinary `git commit` never fires here. Replay its
        # resolution logic explicitly so a commit landed via this branch
        # carries identical trailers to one landed via the agree branch
        # (AC18, docs/plans/2026-07-27-computed-commit-mechanism-selection.md
        # chunk C10-remainder). Mutates `msg_file` in place, BEFORE it is
        # read for `-F` below, exactly mirroring what the hook would have
        # done to the same file had `git commit` fired normally.
        trailer_args = compute_missing_trailer_args(
            msg_file, root, paths=[*diverged, *non_diverged]
        )
        # C7a: an explicit caller `deliverable_id` folds into the SAME
        # `interpret-trailers` call above, mirroring `commit_authored_
        # content`'s `_drop_trailer_arg`-then-append shape -- EXCEPT for
        # precedence against a pre-existing message trailer, which does NOT
        # mirror that sibling (its message-first rule is silent about the
        # THIRD source `commit_anchors.py` may have already stamped here;
        # see `commit_scoped`'s own docstring and PM ruling (2)). May raise
        # `DivergentDeliverableIdError` -- see `_check_deliverable_id_
        # precedence`'s own docstring; deliberately uncaught here.
        if deliverable_id:
            from coordinator_core.ops.deliverable_equivalence import load_equivalence_map

            msg_text_before = Path(msg_file).read_text(encoding="utf-8")
            if _check_deliverable_id_precedence(
                msg_text_before, deliverable_id, equivalence_map=load_equivalence_map(root)
            ):
                trailer_args = _drop_trailer_arg(trailer_args, "Deliverable-Id")
                trailer_args = trailer_args + ["--trailer", f"Deliverable-Id: {deliverable_id}"]
        if trailer_args:
            interpret_result = _git(
                ["interpret-trailers", "--no-divider", "--in-place", *trailer_args, str(msg_file)],
                cwd=root,
            )
            if not interpret_result.ok:
                return interpret_result

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
        new_sha = commit_tree_result.stdout.strip()

        # Compare-and-swap landing -- the 4-argument form fails loud if HEAD
        # moved since `old_head` was captured, rather than silently
        # orphaning a peer commit that landed in the window. This call is
        # NOT env-scoped: `update-ref` moves the real branch ref, which
        # lives in the shared git-dir regardless of which index built the
        # tree being pointed at.
        update_ref_result = _git(
            ["update-ref", "-m", subject, "HEAD", new_sha, old_head],
            cwd=root,
        )
        if not update_ref_result.ok:
            return GitResult(
                returncode=update_ref_result.returncode,
                stdout=update_ref_result.stdout,
                stderr=(
                    "commit_scoped: compare-and-swap failed -- HEAD moved "
                    f"concurrently since {old_head} was captured; refusing to "
                    f"retry silently (a retry needs a tree rebuilt against the "
                    f"new HEAD). {update_ref_result.stderr}"
                ),
            )
        # `staged_paths` is exactly the resolved staged-blob subset of
        # `diverged` -- `diverged` is the set that had unstaged
        # working-tree modifications (`commit_scoped()` only reaches this
        # function when `diverging_paths` returned a non-empty answer, and
        # passes that exact answer through unmodified), and
        # `_resolve_content_sources` carves out any path ALSO present in
        # `supplied_blobs` before it ever reaches `staged_paths` -- so no
        # separate worktree-vs-staged recomputation is needed here; reusing
        # the resolution's own answer is that answer (see the P1 bug
        # backlog entry cited on `GitResult.worktree_excluded` for the
        # incident this closes: this field was previously not populated at
        # all, so a caller had no way to learn its worktree edits were
        # excluded). With `supplied_blobs` empty (this chunk's only
        # exercised shape), `staged_paths == diverged` exactly, so this is
        # byte-identical to the prior behaviour.
        return GitResult(
            returncode=0,
            stdout=new_sha,
            stderr=(
                "commit_scoped: worktree edits to %s were NOT included -- "
                "the staged (index) version was committed instead (private-"
                "index branch; see GitResult.worktree_excluded)"
                % (", ".join(staged_paths),)
            ),
            worktree_excluded=tuple(staged_paths),
        )
    finally:
        temp_index.unlink(missing_ok=True)


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
def commit_scoped(
    paths: Sequence[str],
    msg_file: Union[str, Path],
    cwd: Union[str, Path],
    *,
    known_checked: Optional[Set[str]] = None,
    known_diverged: Optional[Set[str]] = None,
    deliverable_id: Optional[str] = None,
    supplied_blobs: Optional[Dict[str, str]] = None,
) -> GitResult:
    """Commit exactly `paths`, choosing the safe mechanism from OBSERVED
    index/worktree state -- the computed replacement for hand-picking
    between `git commit -- <paths>` and a bare `git commit` on a shared
    working tree (see the module-section docstring above `commit_scoped`
    for the two incidents this closes).

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
           is the overwhelming-majority path and stays this cheap. When
           `deliverable_id` is truthy, this branch NOW mutates `msg_file` in
           place before `git add`/`git commit` run (see `deliverable_id`
           below) -- prior to C7a (docs/plans/2026-08-10-a-commit-trailer-
           that-names-the-session.md) this branch never opened or mutated
           `msg_file` at all, relying entirely on the `prepare-commit-msg`
           hook that fires on `git commit` to compute trailers on its own.
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
    already agrees; and raises `DivergentDeliverableIdError` (uncaught, a
    deliberate exception to this function's own "always returns a
    GitResult" contract -- see that ruling's own docstring) when one
    disagrees. Advisory-only half (not enforced here, prose guidance only):
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
        if known_checked is not None and known_diverged is not None:
            gap = [p for p in path_list if p not in known_checked]
            # Chunked (see `_diverging_paths_chunked()`'s own docstring):
            # `git diff` cannot take a `--pathspec-from-file`, so this is
            # the one of `commit_scoped()`'s three git calls that stays on
            # argv, packed into argv-safe batches instead. `gap` is
            # ordinarily empty/small (the dedup seam's whole point), but a
            # swept-rename destination set can still reach percolate-
            # publish scale.
            gap_diverged = (
                _diverging_paths_chunked(gap, cwd=str(root), timeout=_DIVERGENCE_CHECK_TIMEOUT_SECS)
                if gap
                else set()
            )
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
            diverged = sorted(
                _diverging_paths_chunked(
                    path_list, cwd=str(root), timeout=_DIVERGENCE_CHECK_TIMEOUT_SECS
                )
            )
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
    # `diverged` says. The agree branch's own `git add -- path_list` reads
    # the SHARED worktree and would silently overwrite a supplied path's
    # provenanced blob with whatever a peer's ordinary edit left on disk --
    # exactly the incident class this plan closes. A supplied path is
    # therefore always routed to `_commit_scoped_private_index`, which
    # resolves it to `_SOURCE_SUPPLIED` via `_resolve_content_sources`
    # (supplied wins over both `diverged`/`non_diverged` membership) and
    # commits its cacheinfo blob verbatim, never re-reading the worktree.
    supplied_blobs = supplied_blobs or {}
    if not diverged and not supplied_blobs:
        # Layer-1 CAS check (see the snapshot comment above `diverging_
        # paths()`): re-observe index/HEAD blobs now, right before this
        # branch's own `git add`/`git commit`, and refuse rather than
        # silently restaging from the worktree if the world moved since
        # `pre_index_blobs`/`pre_head_blobs` were captured. FAILS LOUD
        # (`GitResult.ok is False`), same posture as every other guard in
        # this function -- never a warning, never a silent fall-through to
        # the private-index branch (see `_agree_branch_cas_refusal`'s own
        # docstring for why this stays a blob-identity check, not an
        # ownership gate).
        cas_refusal = _agree_branch_cas_refusal(root, path_list, pre_index_blobs, pre_head_blobs)
        if cas_refusal is not None:
            return cas_refusal

        # `path_list` is the FINAL, caller-vetted commit pathspec, which
        # legitimately includes paths already fully staged-deleted (e.g.
        # this pipeline's `deleted_paths`, pre-`git rm`'d by the caller
        # before invoking the ceremony) -- absent from BOTH the worktree
        # AND the index already, nothing further to stage. `git add --
        # <path>` FAILS LOUD (`pathspec ... did not match any files`) on
        # such a path (verified empirically -- `-A` does not change this;
        # a path gone from both worktree and index matches nothing for
        # either form), so only the subset that still exists on disk is
        # (re-)added; an already-staged deletion needs no further `git
        # add` call to land correctly in the commit below.
        # C7a (1b): the AGREE branch never opened or mutated `msg_file`
        # before this chunk landed -- the `prepare-commit-msg` hook that
        # fires on the `git commit` below computed trailers entirely on its
        # own, with no knowledge of any caller parameter. An explicit
        # `deliverable_id` requires mutating `msg_file` HERE, before that
        # commit runs, so the hook's own idempotency check
        # (`coordinator/bin/coordinator-prepare-commit-msg::main`,
        # `need_deliverable_id_check = not _has_trailer_line(commit_msg_file,
        # "Deliverable-Id:")`) sees the line already present and skips its
        # own Deliverable-Id leg entirely -- LOAD-BEARING, not incidental:
        # without this, the hook would independently infer (and stamp) its
        # own session/claimed-plan-derived value, silently overriding the
        # caller's explicit one. May raise `DivergentDeliverableIdError` --
        # see `_check_deliverable_id_precedence`'s own docstring; deliberately
        # uncaught here, before any staging happens.
        #
        # 2026-08-14 (cross-repo memo, `2026-08-14-doe-claude-em-scoped-git-
        # commit-drops-session-id-trailer.md`; docs/plans/2026-07-27-computed-
        # commit-mechanism-selection.md chunk C10-remainder, AC18): this
        # branch relied ENTIRELY on the installed `prepare-commit-msg` hook
        # firing to stamp `Session-Id:` -- unlike `_commit_scoped_private_
        # index` a few lines below, which never trusts hooks (plumbing runs
        # none) and instead replays the hook's resolution logic itself via
        # `compute_missing_trailer_args`. A hook non-fire (missing shim
        # script, no python on PATH, `core.hooksPath` override, non-
        # executable hook, a future rename) landed an agree-branch commit
        # with NO Session-Id trailer at all, silently -- the incident this
        # call closes. Same call, same `paths=path_list` shape as the
        # private-index branch's own use a few lines below; only the
        # trailers still MISSING from `msg_file` are returned (omit-rather-
        # than-guess -- see that function's own docstring), so this is a
        # pure addition when the hook already ran (or will run) and the
        # sole source of the trailer when it does not.
        trailer_args = compute_missing_trailer_args(msg_file, root, paths=path_list)

        # NOT `git interpret-trailers --if-exists replaceAll` as originally
        # proposed: a live probe against this repo's own git (this chunk's
        # own report) found that form silently APPENDS a duplicate trailer
        # line, rather than replacing or failing, on a message with no blank
        # line separating its subject from an already-present `Deliverable-
        # Id:` line -- git's own trailer-block detection never recognizes
        # the existing line as a trailer to replace. This function instead
        # only ever asks git to ADD (never replace): an explicit caller
        # `deliverable_id` is folded into `trailer_args` -- dropping any
        # computed entry for it first (mirroring `_commit_scoped_private_
        # index`'s own `_drop_trailer_arg`-then-append shape) -- ONLY once
        # `_check_deliverable_id_precedence` has confirmed via a plain-text
        # scan that no disagreeing `Deliverable-Id:` line exists yet, so the
        # single `interpret-trailers` call below never sees a trailer name
        # it would need to replace.
        if deliverable_id:
            from coordinator_core.ops.deliverable_equivalence import load_equivalence_map

            msg_text_before = Path(msg_file).read_text(encoding="utf-8")
            if _check_deliverable_id_precedence(
                msg_text_before, deliverable_id, equivalence_map=load_equivalence_map(root)
            ):
                trailer_args = _drop_trailer_arg(trailer_args, "Deliverable-Id")
                trailer_args = trailer_args + ["--trailer", f"Deliverable-Id: {deliverable_id}"]
        if trailer_args:
            interpret_result = _git(
                ["interpret-trailers", "--no-divider", "--in-place", *trailer_args, str(msg_file)],
                cwd=root,
            )
            if not interpret_result.ok:
                return interpret_result

        # `add_paths_pathspec_file()`/`commit_with_message_file_pathspec_
        # scoped()`, NOT `add_paths()`/`commit_with_message_file()` -- this
        # is the agree branch's own percolate-publish-scale argv-length fix
        # (see those two wrappers' own docstrings, and `_diverging_paths_
        # chunked()`'s above for why the divergence CHECK a few lines up
        # takes the opposite (chunked, not pathspec-file) treatment).
        existing = [p for p in path_list if (root / p).exists()]
        if existing:
            add_result = add_paths_pathspec_file(root, existing)
            if not add_result.ok:
                return add_result
        return commit_with_message_file_pathspec_scoped(root, msg_file, path_list)

    diverged_set = set(diverged)
    non_diverged = [p for p in path_list if p not in diverged_set]
    return _commit_scoped_private_index(
        diverged, non_diverged, msg_file, root, deliverable_id, supplied_blobs=supplied_blobs
    )


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


def _replay_post_commit_auto_push(root: Union[str, Path]) -> None:
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
    """
    try:
        from coordinator_core.hooks import auto_push

        auto_push.main(["--repo-root", str(root)])
    except Exception:
        pass


def commit_authored_content(
    path: str,
    content: str,
    msg_file: Union[str, Path],
    cwd: Union[str, Path],
    *,
    deliverable_id: Optional[str] = None,
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
    if not normalized or normalized.startswith("/") or ".." in Path(normalized).parts:
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

    old_head_result = _git(["rev-parse", "HEAD"], cwd=root)
    if not old_head_result.ok:
        return old_head_result
    old_head = old_head_result.stdout.strip()

    ls_tree_result = _git(["ls-tree", "HEAD", "--", normalized], cwd=root)
    if not ls_tree_result.ok:
        return ls_tree_result
    ls_tree_line = ls_tree_result.stdout.strip()
    if not ls_tree_line:
        return GitResult(
            returncode=-1,
            stdout="",
            stderr=(
                f"commit_authored_content: {normalized!r} does not exist in HEAD "
                f"({old_head}) -- this entrypoint is built for in-place mutation "
                "of an existing reserved-noun file, not for creating a new one"
            ),
        )
    ls_tree_meta, _, _ls_tree_path = ls_tree_line.splitlines()[0].partition("\t")
    mode, _obj_type, _old_sha = ls_tree_meta.split()

    hash_result = _hash_object_stdin_bytes(content.encode("utf-8"), normalized, cwd=root)
    if not hash_result.ok:
        return hash_result
    new_sha = hash_result.stdout.strip()

    temp_index = Path(tempfile.gettempdir()) / f"git-index-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        private_env: Dict[str, str] = dict(os.environ)
        private_env["GIT_INDEX_FILE"] = str(temp_index)

        # Bound 3 -- seeded straight from HEAD into a FRESH temp index, never
        # via `shutil.copy2` of the shared `.git/index`.
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

        # Bound 2 -- trailer tier-0 resolves from the CALLER-SUPPLIED
        # `deliverable_id`, never by opening `path` on disk.
        # `compute_missing_trailer_args(..., paths=None)` is the pre-tier-0
        # session-only resolution (Session-Id, plus Deliverable-Id only via
        # the session/claimed-plan tiers, never the artifact-first tier that
        # reads a committed path) -- passing `paths=[normalized]` here would
        # reintroduce exactly the worktree read this bound forbids.
        trailer_args = compute_missing_trailer_args(msg_file, root, paths=None)
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

        if trailer_args:
            interpret_result = _git(
                ["interpret-trailers", "--no-divider", "--in-place", *trailer_args, str(msg_file)],
                cwd=root,
            )
            if not interpret_result.ok:
                return interpret_result

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

        # Bound 6 -- refresh the SHARED index for this one path only, so it
        # no longer reads as a staged reversion of the commit that just
        # landed (AC11). Inferred hazard, not a cited one -- see this
        # function's own docstring bound 6. Best-effort like bound 5's
        # auto-push replay below: the commit already landed via a
        # successful CAS by the time this runs, so a refresh failure must
        # never be reported as this function's own failure (that would
        # discard `new_commit_sha` for a write that genuinely succeeded and
        # could send a caller into a spurious retry/duplicate commit).
        _git(
            ["update-index", "--add", "--cacheinfo", f"{mode},{new_sha},{normalized}"],
            cwd=root,
        )

        # Bound 5 -- replay the post-commit auto-push; only after a
        # successful CAS, mirroring the trailer replay's own "replay what
        # the hook would have done" precedent.
        _replay_post_commit_auto_push(root)

        return GitResult(returncode=0, stdout=new_commit_sha, stderr="")
    finally:
        temp_index.unlink(missing_ok=True)


def rev_parse_head(cwd: Union[str, Path]) -> GitResult:
    """`git rev-parse HEAD` — post-commit SHA capture (Position A, no branch-tip fallback)."""
    return _git(["rev-parse", "HEAD"], cwd=cwd)


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


def push(cwd: Union[str, Path], *, remote_name: Optional[str] = None, timeout: float = 120) -> GitResult:
    """`git push [<remote_name>]` — push-with-retry pipeline (C4)."""
    args = ["push"]
    if remote_name:
        args.append(remote_name)
    return _git(args, cwd=cwd, timeout=timeout)


def fetch(cwd: Union[str, Path], remote_name: str, *, timeout: float = 120) -> GitResult:
    """`git fetch <remote_name>` — reject-detection preflight before rebase --onto."""
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
