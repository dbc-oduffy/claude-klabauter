"""
coordinator_core.ops.ceremony.git_native — Windows-safe shared git-subprocess helper.

Purpose: every native `git` subprocess invocation in the `wsc_tail` rebuild routes
through the single `_git()` helper in this module, so the Windows-safe subprocess
flags are carried exactly once instead of being re-typed (and re-forgotten) at each
of the ~13 call sites the OLD `wsc_commit.py` scattered them across with none of
these flags present (the portability gap this chunk closes).

Flags carried by every invocation (AC3):
    creationflags=CREATE_NO_WINDOW — the `commit_anchors.py` idiom (Windows-only;
        no-ops elsewhere via `getattr(subprocess, "CREATE_NO_WINDOW", 0)`). Suppresses
        the focus-stealing console popup a bare `subprocess.run` spawns per-invocation
        on Windows under a headless parent process.
    stdin=subprocess.DEVNULL — NOT carried by `commit_anchors.py` today. Load-bearing
        for the Windows non-hang: a git subprocess with inherited stdin can block
        indefinitely on an interactive prompt (credential helper, merge conflict editor,
        pager) — exactly the class of >120s wedge this rebuild targets. Must never be
        dropped from any wrapper added here.
    capture_output=True, text=True — every wrapper returns decoded stdout/stderr;
        callers never need to touch raw bytes or re-invoke with different capture flags.

Spec backlink: docs/plans/2026-07-16-wsc-pure-python-tail-rebuild.md § C1 (AC3 foundation).

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
    try:
        result = subprocess.run(
            full_args,
            cwd=str(cwd),
            text=True,
            timeout=timeout,
            **no_console_creationflags(),
            env=env,
            **run_kwargs,
            **stdin_kwargs,
        )
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
    # example-doctrine-repo's bash-on-windows-gotchas.md § 11). The contention win here is
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
    """
    return _git(["commit", "-F", str(msg_file), "--", *paths], cwd=cwd)


# ---------------------------------------------------------------------------
# commit_scoped -- the computed commit-mechanism selector (C3).
#
# Neither documented commit form is safe alone on a shared working tree:
#   `git commit -- <paths>` reads the WORKTREE, silently discarding
#     deliberately-staged partial-hunk content (claude-klabauter 506748a0).
#   a bare `git commit` (or one whose pathspec is a DIRECTORY, which matches
#     whatever lands inside it AT COMMIT TIME) commits THE INDEX, silently
#     absorbing whatever a peer session staged (example-doctrine-repo 726925b2).
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


def _check_deliverable_id_precedence(msg_text: str, deliverable_id: str) -> bool:
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
    if existing == deliverable_id:
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

    (a) SHAPE -- must start with `dlv-`, the convention `coordinator_core.
        frontmatter.schema_validate._cf_deliverable_id_prefix` already
        enforces for a plan/handoff's own frontmatter field (every value is
        minted by `bin/mint-deliverable-id` with this prefix). Reproduced
        here as a bare `str.startswith` rather than imported -- that
        validator runs over a whole frontmatter dict as part of a larger
        schema pass, not a bare string, so importing it would pull in that
        entire pass for one prefix check.

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
    if not deliverable_id.startswith("dlv-"):
        return (
            f"commit_scoped: deliverable_id {deliverable_id!r} rejected -- "
            "does not match the 'dlv-' shape convention (every deliverable_id "
            "is minted by bin/mint-deliverable-id with this prefix; see "
            "coordinator_core/frontmatter/schema_validate.py's own "
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


def _commit_scoped_private_index(
    diverged: Sequence[str],
    non_diverged: Sequence[str],
    msg_file: Union[str, Path],
    cwd: Union[str, Path],
    deliverable_id: Optional[str] = None,
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
    """
    root = Path(cwd)

    old_head_result = _git(["rev-parse", "HEAD"], cwd=root)
    if not old_head_result.ok:
        return old_head_result
    old_head = old_head_result.stdout.strip()

    # Capture the CURRENTLY STAGED blob for each diverged path from the REAL
    # index, before any mutation -- this is the deliberately-staged content
    # (partial hunk, etc.) that must survive VERBATIM, never re-derived from
    # the worktree (that would reproduce the exact bug this selector exists
    # to close).
    cacheinfo_entries: List[_CacheInfoEntry] = []
    if diverged:
        ls_files_result = _git(["ls-files", "-s", "--", *diverged], cwd=root)
        if not ls_files_result.ok:
            return ls_files_result
        cacheinfo_entries = _parse_ls_files_cacheinfo(ls_files_result.stdout)

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

        # Non-diverged paths (staged==worktree, or newly-added) are safe to
        # (re-)stage straight from the worktree. A staged-deletion path is
        # covered too WITHOUT `-A`: the `reset -q HEAD` above resurrects it
        # in THIS PRIVATE copy (HEAD still has it), so it exists in the
        # private index even though the worktree lacks it -- `git add --
        # path` stages a removal for a path present in the index but
        # missing from the worktree (verified empirically; `-A` is only
        # needed when a path is untracked/gone from BOTH, which is not this
        # shape post-reset).
        if non_diverged:
            add_result = _git(["add", "--", *non_diverged], cwd=root, env=private_env)
            if not add_result.ok:
                return add_result

        # Diverged paths are restored from the captured staged blob, never
        # from the worktree.
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
            msg_text_before = Path(msg_file).read_text(encoding="utf-8")
            if _check_deliverable_id_precedence(msg_text_before, deliverable_id):
                trailer_args = _drop_trailer_arg(trailer_args, "Deliverable-Id")
                trailer_args = trailer_args + ["--trailer", f"Deliverable-Id: {deliverable_id}"]
        if trailer_args:
            interpret_result = _git(
                ["interpret-trailers", "--in-place", *trailer_args, str(msg_file)],
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
        # `diverged` is exactly the set this branch was called with because
        # it IS the set that had unstaged working-tree modifications --
        # `commit_scoped()` only reaches this function when `diverging_paths`
        # (staged-vs-HEAD differs AND worktree-vs-staged differs, per that
        # function's own docstring) returned a non-empty answer, and passes
        # that exact answer through unmodified as `diverged` -- so no
        # separate worktree-vs-staged recomputation is needed here; reusing
        # the caller's own answer is that answer (see the P1 bug backlog
        # entry cited on `GitResult.worktree_excluded` for the incident this
        # closes: this field was previously not populated at all, so a
        # caller had no way to learn its worktree edits were excluded).
        return GitResult(
            returncode=0,
            stdout=new_sha,
            stderr=(
                "commit_scoped: worktree edits to %s were NOT included -- "
                "the staged (index) version was committed instead (private-"
                "index branch; see GitResult.worktree_excluded)"
                % (", ".join(diverged),)
            ),
            worktree_excluded=tuple(diverged),
        )
    finally:
        temp_index.unlink(missing_ok=True)


def commit_scoped(
    paths: Sequence[str],
    msg_file: Union[str, Path],
    cwd: Union[str, Path],
    *,
    known_checked: Optional[Set[str]] = None,
    known_diverged: Optional[Set[str]] = None,
    deliverable_id: Optional[str] = None,
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
         - No divergence -> AGREE branch: `git add -- paths` then
           `git commit -F msg_file -- paths`. Retains SC-DR-008's race
           protection across the stage->commit window; this is the
           overwhelming-majority path and stays this cheap. When
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
            gap_diverged = (
                diverging_paths(
                    gap, cwd=str(root), timeout=_DIVERGENCE_CHECK_TIMEOUT_SECS, fail_loud=True
                )
                if gap
                else []
            )
            diverged = sorted((known_diverged & set(path_list)) | set(gap_diverged))
        else:
            diverged = diverging_paths(
                path_list, cwd=str(root), timeout=_DIVERGENCE_CHECK_TIMEOUT_SECS, fail_loud=True
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

    if not diverged:
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
        # NOT `git interpret-trailers --if-exists replaceAll` as originally
        # proposed: a live probe against this repo's own git (this chunk's
        # own report) found that form silently APPENDS a duplicate trailer
        # line, rather than replacing or failing, on a message with no blank
        # line separating its subject from an already-present `Deliverable-
        # Id:` line -- git's own trailer-block detection never recognizes
        # the existing line as a trailer to replace. This function instead
        # only ever asks git to ADD (never replace), and only once
        # `_check_deliverable_id_precedence` has confirmed via a plain-text
        # scan that no `Deliverable-Id:` line exists yet -- sidestepping
        # that hazard rather than trusting every future caller's message
        # shape to avoid it.
        if deliverable_id:
            msg_text_before = Path(msg_file).read_text(encoding="utf-8")
            if _check_deliverable_id_precedence(msg_text_before, deliverable_id):
                interpret_result = _git(
                    [
                        "interpret-trailers",
                        "--trailer", f"Deliverable-Id: {deliverable_id}",
                        "--in-place", str(msg_file),
                    ],
                    cwd=root,
                )
                if not interpret_result.ok:
                    return interpret_result

        existing = [p for p in path_list if (root / p).exists()]
        if existing:
            add_result = add_paths(root, existing)
            if not add_result.ok:
                return add_result
        return commit_with_message_file(root, msg_file, path_list)

    diverged_set = set(diverged)
    non_diverged = [p for p in path_list if p not in diverged_set]
    return _commit_scoped_private_index(diverged, non_diverged, msg_file, root, deliverable_id)


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
# Spec backlink: docs/plans/2026-08-06-writer-side-commit-ownership-lock-gap.md
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
                ["interpret-trailers", "--in-place", *trailer_args, str(msg_file)],
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
