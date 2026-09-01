"""
coordinator_core.ops.push_failure_verdict — JSON-RPC "git.push_failure_verdict"
operation.

Purpose: classify a non-fast-forward push failure into one of five states so
DoE-claude's Stop-hook auto-push advisory (`coordinator/hooks/scripts/
runtime-tripwire-em-check.py`, THEIR repo, not touched here) renders a verdict
we computed instead of re-deriving one from a regex. The classification is
the value this op sells; the advisory is a pure renderer over it.

Origin memos (read for full context, not reproduced here):
    cross-repo/inbox/2026-08-06-doe-claude-em-autopush-advisory-worktree-contention.md
    cross-repo/inbox/2026-08-06-doe-claude-em-autopush-advisory-yes-build-the-verdict-op.md

Op-key / contract:
    git.push_failure_verdict
    params:   {} (no required params — see handler docstring for repo_root
               resolution)
    response: {
        verdict: "peer_staged" | "half_applied_merge" | "simple_lag"
                 | "resolved_since" | "indeterminate",
        evidence: {
            staged_count: int,
            staged_sample: list[str]           (first 20, sorted),
            unstaged_local_count: int,
            incoming_count: int | None,         (None when upstream unresolvable)
            staged_incoming_overlap: int | None,
            staged_unstaged_overlap: int | None,
            ahead: int | None,
            behind: int | None,
            merge_head_present: bool,
            upstream_resolved: bool,
            push_failures_log_count: int,
            push_failures_log_newest: str | None,   (raw bracketed timestamp)
        },
        remedy_hint: str,
    }

Scope-verdict `show_top`: every signal this op reads — staged/unstaged diff,
MERGE_HEAD, upstream ahead/behind — is per-WORKTREE state (linked worktrees
each carry their own index, private gitdir, and upstream tracking config),
never the shared git COMMON dir; matches `coverage.gate`'s and `merge.
quiet_activity_gate`'s own `show_top` rationale in `op_scopes.py`. The one
exception is `push-failures.log` itself, which the writer (`hooks/auto_push.
py`) keys off the git COMMON dir via `resolve_git_common_dir` — this op
resolves that path the same way, independent of the `show_top` scope key
that governs `repo_root` injection.

Classification order (five states, checked top-down; see each branch's own
comment for the discriminator):
    1. Upstream unresolvable (no tracking branch, detached HEAD, not a repo)
       -> indeterminate.
    2. Staged set non-empty:
       - Incoming diff unavailable -> indeterminate (no discriminator data
         to confirm either staged state; guessing here is exactly the
         "always picks something" erosion the origin memo warns against).
       - High overlap(staged, incoming) AND ~zero overlap(staged, unstaged)
         -> half_applied_merge.
       - Otherwise -> peer_staged.
    3. Staged set empty:
       - push-failures.log has entries AND tree is fully in sync (ahead==0,
         behind==0, no MERGE_HEAD) -> resolved_since.
       - ahead>0 or behind>0 -> simple_lag.
       - Otherwise (fully clean, no log entries) -> indeterminate (nothing
         pathological to classify — the caller invoked this with no signal).

Negative-spec:
    - Read-only. Never stages, merges, resets, or pushes anything.
    - Never raises on git-availability/state degradation (missing log,
      no upstream, detached HEAD, empty repo, non-repo path) — every
      degradation collapses to a `GitResult`-shaped failure at the specific
      subprocess call, which this module reads as "signal absent" and folds
      into `indeterminate`, never a traceback.
    - Does NOT add a "best guess" tiebreak to drain `indeterminate` — see
      classification order step 2's "incoming diff unavailable" branch,
      which is deliberately NOT collapsed into `peer_staged` despite that
      being the operationally "safer" guess; the origin memo names this
      explicitly as a state that must stay genuinely reachable.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, NamedTuple, Optional

from coordinator_core.git.git_dir import resolve_git_common_dir, resolve_git_dir
from coordinator_core.git.git_index import IndexParseError as _ScopedIndexParseError
from coordinator_core.git.git_index import IndexV4Unsupported as _ScopedIndexV4Unsupported
from coordinator_core.git.git_index import scoped_status as _scoped_status
from coordinator_core.git.git_state import IndexParseError as _FullIndexParseError
from coordinator_core.git.git_state import head_blobs as _head_blobs
from coordinator_core.git.git_state import read_index as _read_index
from coordinator_core.ipc import register_op
from coordinator_core.ops.ceremony.git_native import GitResult, _git

#: Overlap threshold (staged ∩ incoming) / len(staged) at/above which staged
#: content reads as "mostly the incoming commit's own files" rather than an
#: unrelated peer's WIP — the origin memo's live incident measured 58/63
#: (~0.92); set comfortably below that so a smaller but still-dominant
#: overlap still classifies, while a genuinely unrelated peer batch (typically
#: near-zero overlap with the remote diff) does not.
_HALF_APPLIED_OVERLAP_RATIO = 0.5

#: `staged_unstaged_overlap` at/below which "our own failed merge" reads as
#: confirmed rather than merely plausible — the origin memo's incident was
#: exactly zero; kept as a hard `== 0` rather than a ratio since a nonzero
#: overlap with the caller's OWN unstaged edits is itself evidence some of
#: the staged content is genuinely this session's WIP, not the merge's.
_HALF_APPLIED_MAX_UNSTAGED_OVERLAP = 0

#: Matches the auto_push.py writer's bracketed-UTC-stamp line prefix,
#: mirroring `workday_surface_auto_push_failure_stats._TS_RE` (kept as a
#: private duplicate rather than a cross-module import — this op only needs
#: the newest raw line, not that module's 24h-window aggregation).
_TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\]")

_STAGED_SAMPLE_LIMIT = 20


class _StatusProbe(NamedTuple):
    """Every index/worktree/upstream signal `classify` needs, parsed from one
    `git status --porcelain=v2 --branch` spawn and returned by value once
    `_status_probe` has finished parsing it.

    C5 (2026-09-01, state/dispatch-briefs/2026-09-01-a-guard-that-cannot-
    reach-warmth-still-r/C5.md): this reader is now the FALLBACK, not the
    default. `classify` only reaches it when `_merge_head_present` is True
    (an in-flight conflict) or `_inprocess_staged_unstaged` declines (a
    corrupt/split/v4 index, or one carrying an unmerged stage — `git_state.
    read_index` refuses to guess which stage of a conflicted path is
    "staged", by design; a plain `git status` still answers that question
    correctly). Kept verbatim (measured at 221.9ms on its own, over the
    200ms per-process ceiling on this box) because the fallback is
    genuinely the CORRECT answer for the shapes it is reserved for, not
    merely a convenient one -- see `classify`'s own docstring section for
    the spawn-count split this produces.

    NEGATIVE SPEC — why one spawn and not the four this ORIGINALLY
    replaced. Each of `diff --cached --name-only`, `diff --name-only`,
    `rev-parse --git-dir` and `rev-list --left-right --count @{u}...HEAD`
    cost a process, and on Windows process creation (not the query) is the
    bill: measured here at 36-162ms each, 546ms for the set, against a
    500ms brightline and a 200ms per-process ceiling. `--porcelain=v2
    --branch` answers staged, unstaged, ahead, behind and
    upstream-resolved from a single machine-readable stream, and
    MERGE_HEAD is a file test the private gitdir already answers without
    git. Do not split these FOUR back apart on the fallback leg; the
    fallback's own spawn count IS still the contract for the shapes that
    reach it.
    """

    staged: Optional[List[str]]
    unstaged: Optional[List[str]]
    ahead: Optional[int]
    behind: Optional[int]
    upstream_resolved: bool


def _status_probe(repo_root: Path) -> _StatusProbe:
    """Parse `git status --porcelain=v2 --branch` into a `_StatusProbe`.

    A git failure (not a repo, unreadable tree) returns `staged`/`unstaged`
    as None and `upstream_resolved` False — exactly the shape the previous
    per-probe `None` returns folded into, so `classify`'s indeterminate
    legs are unchanged.

    Line shapes consumed (git's own porcelain-v2 grammar):
      `# branch.ab +<ahead> -<behind>`  — emitted ONLY with an upstream, so
          its presence IS the upstream-resolved signal.
      `1 <XY> ...<path>`               — ordinary change.
      `2 <XY> ...<path>	<origPath>`   — rename/copy; the NEW path is taken,
          matching `diff --name-only`'s own reporting of a rename.
      `u <XY> ...<path>`               — unmerged; counted as staged, which
          is what `diff --cached --name-only` did with it.
      `? `/`! `                        — untracked/ignored. Never emitted:
          `--untracked-files=no` suppresses the untracked scan outright,
          which is both faster (it is the expensive half of `status` on a
          large tree) and exactly right — the `diff` probes this replaced
          never saw untracked paths either.

    `X` is the index status and `Y` the worktree status; `.` means clean.
    """
    result = _git(
        ["status", "--porcelain=v2", "--branch", "--untracked-files=no"],
        cwd=repo_root,
    )
    if not result.ok:
        return _StatusProbe(
            staged=None, unstaged=None, ahead=None, behind=None, upstream_resolved=False
        )

    staged: List[str] = []
    unstaged: List[str] = []
    ahead: Optional[int] = None
    behind: Optional[int] = None
    upstream_resolved = False
    for line in result.stdout.splitlines():
        if not line:
            continue
        if line.startswith("# branch.ab "):
            parts = line[len("# branch.ab ") :].split()
            if len(parts) == 2:
                try:
                    ahead = int(parts[0])
                    behind = -int(parts[1])
                except ValueError:
                    continue
                else:
                    upstream_resolved = True
            continue
        marker = line[:2]
        if marker not in ("1 ", "2 ", "u "):
            continue
        fields = line.split(" ", 2)
        if len(fields) < 3:
            continue
        xy = fields[1]
        if len(xy) != 2:
            continue
        rest = fields[2]
        # Path extraction is bounded by the EXACT fixed-field count per row
        # kind (git's own porcelain-v2 grammar), never a delimiter search --
        # git does NOT quote a path merely for containing a space, so an
        # unbounded `rsplit(" ", 1)` silently truncated "my file.txt" to
        # "file.txt". Field counts verified against `git status
        # --porcelain=v2` output generated in a throwaway repo, not taken
        # on trust from documentation.
        if marker == "1 ":
            # sub, mH, mI, mW, hH, hI -- 6 fixed fields before the path.
            parts = rest.split(" ", 6)
            if len(parts) < 7:
                continue
            path = parts[6]
        elif marker == "u ":
            # sub, m1, m2, m3, mW, h1, h2, h3 -- 8 fixed fields before path.
            parts = rest.split(" ", 8)
            if len(parts) < 9:
                continue
            path = parts[8]
        else:  # marker == "2 "
            # sub, mH, mI, mW, hH, hI, X<score> -- 7 fixed fields, then
            # "<path>\t<origPath>"; only the NEW path is taken, matching
            # `diff --name-only`'s own reporting of a rename.
            parts = rest.split(" ", 7)
            if len(parts) < 8:
                continue
            path = parts[7].split("	", 1)[0]
        if not path:
            continue
        if marker == "u ":
            # An unmerged/conflicted row is counted as staged ONLY, never
            # unstaged -- `merge_head_present` is the separate, correct
            # conflict signal; a conflicted path landing in BOTH sets would
            # defeat `_HALF_APPLIED_MAX_UNSTAGED_OVERLAP == 0` and could
            # suppress a genuine half_applied_merge verdict.
            staged.append(path)
            continue
        if xy[0] != ".":
            staged.append(path)
        if xy[1] != ".":
            unstaged.append(path)

    return _StatusProbe(
        staged=staged, unstaged=unstaged, ahead=ahead, behind=behind,
        upstream_resolved=upstream_resolved,
    )


def _inprocess_staged_unstaged(
    repo_root: Path,
) -> Optional[tuple]:
    """The zero-spawn replacement for `_status_probe`'s staged/unstaged
    halves, for the ordinary (no in-flight conflict) case -- C5, 2026-09-01
    (state/dispatch-briefs/2026-09-01-a-guard-that-cannot-reach-warmth-
    still-r/C5.md). Both axes are FILE-PARSE questions, not graph walks
    (`coordinator_core.git.commit_walk`'s own measured rule: "convert
    file-parse questions; never convert graph walks" -- a graph walk loses
    to a single spawn past roughly a dozen commits, but an index parse
    never spawns at all), so this reads `.git/index` directly instead of
    spawning `git status`:

      staged   -- `git_state.read_index` (full v2/v3/v4 index identity)
                  compared against `git_state.head_blobs` for the same
                  paths, mirroring `git_index.diff_index_name_status`'s own
                  `(mode, sha)` comparison but over EVERY index path rather
                  than a caller-supplied pathspec.
      unstaged -- `git_index.scoped_status`'s stat fast path (git's own
                  `ce_match_stat`), same paths, any non-`"clean"` verdict
                  counted as unstaged. A stat MISMATCH is counted as
                  unstaged directly (a `"candidate"`), never settled by a
                  content hash -- `classify` only ever consumes `unstaged`
                  to intersect it against `staged` for the half-applied-
                  merge overlap check, and a false-positive "candidate"
                  there is the conservative direction (it can only turn a
                  `half_applied_merge` verdict into a `peer_staged` one,
                  never the reverse silent-guess direction the module's
                  own negative-spec forbids).

    Returns `None` -- take the `_status_probe` fallback -- on any
    `IndexParseError`/`IndexV4Unsupported` from either reader: a corrupt or
    split index, an index v4 this scoped reader refuses, or (the case that
    matters most here) any entry at merge stage > 0, which `git_state.
    read_index` raises on by design rather than guess which stage of a
    conflicted path is "staged". `classify` also skips this reader
    entirely (does not even call it) whenever `_merge_head_present` is
    True, for the same reason plus one more: MERGE_HEAD makes a fallback to
    `git status` correct as a matter of course, not merely as an escape
    hatch.

    NEGATIVE SPEC -- staged DELETIONS (`git rm --cached`, no re-add) are
    NOT detected here. A path removed from the index entirely is, by
    construction, absent from `read_index`'s own key set, so it is never a
    candidate for this function's `staged` list -- finding it would need a
    full recursive walk of HEAD's tree to notice a path that EXISTS there
    and does NOT exist in the index, which is the one shape `git_state.
    read_tree_spine` deliberately does not provide (it walks only the
    directory spine a caller's OWN path list needs, never the whole tree).
    No test in this suite stages a bare deletion without a compensating
    edit, and no known production caller of `git.push_failure_verdict` does
    either (its callers are always reacting to a REJECTED PUSH, where the
    staged content is a peer's WIP or a half-applied merge, not a bare
    `git rm --cached`) -- flagged here, and in the C5 review addendum memo,
    as the one shape this reader cannot answer without a spawn genuinely
    scoped to a graph question, not a narrowing of what it happens to be
    asked today.
    """
    try:
        index = _read_index(repo_root)
    except _FullIndexParseError:
        return None

    paths = list(index.keys())
    if not paths:
        return [], []

    try:
        head_map = _head_blobs(repo_root, paths)
        verdicts = _scoped_status(repo_root, paths)
    except (_FullIndexParseError, _ScopedIndexParseError, _ScopedIndexV4Unsupported):
        return None

    staged = [p for p in paths if head_map.get(p) != (index[p].mode, index[p].sha)]
    unstaged = [p for p in paths if verdicts.get(p, "untracked") != "clean"]
    return staged, unstaged


def _ahead_behind_spawn(repo_root: Path) -> tuple:
    """`(ahead, behind, upstream_resolved)` via ONE narrow spawn --
    `git rev-list --left-right --count @{u}...HEAD` -- for the one fact
    `classify`'s Step 3 needs that genuinely requires git: ahead/behind is a
    commit-GRAPH question, not a file-parse one, and `coordinator_core.git.
    commit_walk`'s own measured finding ("convert file-parse questions;
    never convert graph walks" -- a 630-commit in-process walk measured 52x
    SLOWER than the spawn it would replace, at exact parity) is the reason
    this is not attempted in-process. Left/right counts map to
    behind/ahead in that order, matching `--left-right`'s own `<left>
    <right>` output order for `@{u}...HEAD` (left = only-in-`@{u}` =
    behind, right = only-in-`HEAD` = ahead).

    Paid ONLY when `classify` reaches Step 3 (staged set empty) via the
    in-process staged/unstaged reader -- the staged-nonempty leg never
    needs ahead/behind at all (see `classify`'s own docstring), and the
    `_status_probe` fallback leg already carries this answer for free from
    its one spawn.

    Any spawn failure (no upstream configured, detached HEAD, `@{u}`
    unresolvable, or unparseable output) returns `(None, None, False)` --
    `upstream_resolved=False` folds into the SAME Step-1 `indeterminate`
    leg the missing-`# branch.ab` line produced on the `_status_probe`
    path; never raises.
    """
    result = _git(["rev-list", "--left-right", "--count", "@{u}...HEAD"], cwd=repo_root)
    if not result.ok:
        return None, None, False
    parts = result.stdout.split()
    if len(parts) != 2:
        return None, None, False
    try:
        behind = int(parts[0])
        ahead = int(parts[1])
    except ValueError:
        return None, None, False
    return ahead, behind, True


def _merge_head_present(repo_root: Path) -> bool:
    """Whether MERGE_HEAD exists in THIS worktree's private gitdir.

    Resolved via `resolve_git_dir` (the PRIVATE per-worktree gitdir, not
    `resolve_git_common_dir`) — MERGE_HEAD is worktree-local state, present
    only in the gitdir of the worktree where the merge was attempted,
    matching this op's `show_top` scope-verdict for every other signal it
    reads. `resolve_git_dir` returns what `git rev-parse --git-dir` reports
    without spawning it (see its own docstring), so this file test costs no
    process — the spawn it replaces was ~95ms of the op's old 546ms.
    """
    try:
        return (resolve_git_dir(repo_root) / "MERGE_HEAD").exists()
    except (OSError, ValueError):
        # `resolve_git_dir` reads the `.git` pointer file as UTF-8; a
        # non-UTF-8 pointer file raises `UnicodeDecodeError` (a `ValueError`
        # subclass), not `OSError` -- caught here too so this module's
        # "never raises" negative-spec holds for that degradation as well.
        return False


def _incoming_files(repo_root: Path) -> Optional[List[str]]:
    """Files the incoming (upstream) commits touch relative to the current
    merge-base — `git diff --name-only HEAD...@{u}` (triple-dot: diff
    against the merge-base of HEAD and upstream, i.e. exactly what a
    successful merge/rebase would bring in). None on any git failure
    (no upstream, detached HEAD, not a repo)."""
    result = _git(["diff", "--name-only", "HEAD...@{u}"], cwd=repo_root)
    if not result.ok:
        return None
    return [line for line in result.stdout.splitlines() if line.strip()]


def _push_failures_log_evidence(repo_root: Path) -> tuple:
    """Return (count, newest_raw_timestamp_or_None) over `push-failures.log`,
    keyed at the git COMMON dir (writer's own keying — see module docstring).
    Absent log -> (0, None), never an error (mirrors `workday_surface_
    auto_push_failure_stats`'s own absent-log contract)."""
    log_path = resolve_git_common_dir(repo_root) / "push-failures.log"
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, NotADirectoryError):
        return 0, None
    lines = [line for line in text.splitlines() if line.strip()]
    newest: Optional[str] = None
    for line in reversed(lines):
        match = _TS_RE.match(line)
        if match is not None:
            newest = match.group(1)
            break
    return len(lines), newest


def classify(repo_root: Path) -> dict:
    """Compute the five-state verdict + evidence for `repo_root` (a resolved
    worktree root). Pure read; see module docstring for the classification
    order and each state's discriminator.

    SPAWN COUNT, C5 (2026-09-01, state/dispatch-briefs/2026-09-01-a-guard-
    that-cannot-reach-warmth-still-r/C5.md). The ordinary (no in-flight
    merge conflict, no corrupt/split/v4 index) case now costs:

        staged set empty   -> 1 spawn  (`_ahead_behind_spawn`, Step 3's own
                                        graph question -- see that function)
        staged set nonempty -> 1 spawn  (`_incoming_files` only; ahead/behind
                                        is never consulted by Step 2 at all,
                                        so it is never paid for on this leg
                                        -- down from 2 before this chunk)

    The `_status_probe` fallback (merge conflict in progress, or an index
    this reader declines) is unchanged: 1 spawn when staged is empty, 2
    when it is not -- see `_StatusProbe`'s own docstring for why that leg
    keeps the original combined reader rather than mixing readers.
    """
    merge_head_present = _merge_head_present(repo_root)
    log_count, log_newest = _push_failures_log_evidence(repo_root)

    fast = None if merge_head_present else _inprocess_staged_unstaged(repo_root)

    staged: Optional[List[str]]
    unstaged: Optional[List[str]]
    ahead: Optional[int] = None
    behind: Optional[int] = None
    upstream_resolved = False

    if fast is not None:
        staged, unstaged = fast
    else:
        probe = _status_probe(repo_root)
        staged = probe.staged
        unstaged = probe.unstaged
        ahead = probe.ahead
        behind = probe.behind
        upstream_resolved = probe.upstream_resolved

    # `incoming` is read ONLY inside the `if staged:` leg below, so an EMPTY
    # staged set must not pay for it: that spawn was ~162ms bought to fill an
    # evidence field no clean-index verdict consults. `staged is not None`
    # (the prior guard) was true for `[]` and spent it every time.
    incoming: Optional[List[str]] = None
    staged_incoming_overlap: Optional[int] = None
    staged_unstaged_overlap: Optional[int] = None
    if staged:
        if fast is not None:
            # The in-process reader never learns upstream_resolved (Step
            # 3's own fact, and this leg is Step 2) -- `_incoming_files`'s
            # own success/failure already tells classify everything Step 1
            # would have for THIS shape: a resolvable `@{u}` is exactly the
            # precondition `git diff HEAD...@{u}` needs to succeed.
            incoming = _incoming_files(repo_root)
            upstream_resolved = incoming is not None
        elif upstream_resolved:
            incoming = _incoming_files(repo_root)
        if incoming is not None:
            staged_set = set(staged)
            unstaged_set = set(unstaged) if unstaged is not None else set()
            staged_incoming_overlap = len(staged_set & set(incoming))
            staged_unstaged_overlap = len(staged_set & unstaged_set)
    elif fast is not None:
        # Step 3's own fact, and the one Step 3 cannot answer without it --
        # see `_ahead_behind_spawn`'s own docstring for why this is a
        # genuine (not narrowed-away) git dependency.
        ahead, behind, upstream_resolved = _ahead_behind_spawn(repo_root)

    evidence = {
        "staged_count": len(staged) if staged is not None else 0,
        "staged_sample": sorted(staged)[:_STAGED_SAMPLE_LIMIT] if staged else [],
        "unstaged_local_count": len(unstaged) if unstaged is not None else 0,
        "incoming_count": len(incoming) if incoming is not None else None,
        "staged_incoming_overlap": staged_incoming_overlap,
        "staged_unstaged_overlap": staged_unstaged_overlap,
        "ahead": ahead,
        "behind": behind,
        "merge_head_present": merge_head_present,
        "upstream_resolved": upstream_resolved,
        "push_failures_log_count": log_count,
        "push_failures_log_newest": log_newest,
    }

    # Step 1 -- upstream unresolvable: no tracking branch, detached HEAD, or
    # a git-invocation failure so basic no signal to reason about is safe.
    if not upstream_resolved:
        return {
            "verdict": "indeterminate",
            "evidence": evidence,
            "remedy_hint": (
                "no upstream tracking branch resolvable (detached HEAD, "
                "unconfigured upstream, or a git failure) -- cannot classify "
                "without ahead/behind data"
            ),
        }

    # Step 2 -- staged set non-empty.
    if staged:
        if incoming is None:
            return {
                "verdict": "indeterminate",
                "evidence": evidence,
                "remedy_hint": (
                    f"{len(staged)} file(s) staged but the incoming-commit "
                    "diff could not be computed -- insufficient data to tell "
                    "a peer's staged WIP from our own half-applied merge; "
                    "stand off rather than guess"
                ),
            }
        overlap_ratio = (
            staged_incoming_overlap / len(staged) if staged_incoming_overlap else 0.0
        )
        if (
            overlap_ratio >= _HALF_APPLIED_OVERLAP_RATIO
            and (staged_unstaged_overlap or 0) <= _HALF_APPLIED_MAX_UNSTAGED_OVERLAP
        ):
            return {
                "verdict": "half_applied_merge",
                "evidence": evidence,
                "remedy_hint": (
                    f"{staged_incoming_overlap} of {len(incoming)} incoming files "
                    f"staged, {staged_unstaged_overlap or 0} overlap with local "
                    "modifications -- this is our own failed merge's partial "
                    "index: git reset (mixed), scoped-commit the blockers, re-merge"
                ),
            }
        return {
            "verdict": "peer_staged",
            "evidence": evidence,
            "remedy_hint": (
                f"{len(staged)} file(s) staged do not read as the incoming "
                "commit's own content -- likely another session's "
                "work-in-progress; stand off, touch nothing"
            ),
        }

    # Step 3 -- staged set empty.
    if log_count > 0 and (ahead or 0) == 0 and (behind or 0) == 0 and not merge_head_present:
        return {
            "verdict": "resolved_since",
            "evidence": evidence,
            "remedy_hint": (
                f"{log_count} push-failures.log entr{'y' if log_count == 1 else 'ies'} "
                "on record, but the tree is now fully in sync -- the failure was "
                "real when written and has since been superseded (most likely a "
                "peer reconciled and pushed); nothing to push"
            ),
        }

    if (ahead or 0) > 0 or (behind or 0) > 0:
        return {
            "verdict": "simple_lag",
            "evidence": evidence,
            "remedy_hint": (
                f"clean index, {ahead} ahead / {behind} behind upstream -- "
                "git push (or pull-then-push) is genuinely the remedy"
            ),
        }

    return {
        "verdict": "indeterminate",
        "evidence": evidence,
        "remedy_hint": (
            "clean index, fully in sync with upstream, no push-failures.log "
            "entries -- no pathological signal to classify"
        ),
    }


@register_op("git.push_failure_verdict")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC 'git.push_failure_verdict' handler.

    `repo_root` is the dispatch-supplied CALLING worktree (scope-verdict
    `show_top`) — this op takes no params of its own (frozen contract has
    none); falls back to `Path.cwd()` only for a standalone invocation with
    no injected `repo_root` (mirrors `merge.quiet_activity_gate`'s own
    handler convention).
    """
    root = Path(repo_root) if repo_root is not None else Path.cwd()
    return classify(root)
