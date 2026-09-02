"""coordinator_core.git.commit -- ONE commit path, zero spawns.

Written from first principles, not refactored out of `git_native.commit_scoped`.
That op was deleted at the kill bar (>500ms process time) after four rounds of
spawn-shaving left it at 1513ms / 31 procs; a fifth round of the same is not
what follows a delete. What follows is the requirement, restated:

    Commit exactly these paths, on a worktree ~50 sessions share, taking the
    bytes the caller meant, without silently clobbering a peer, and leave
    `git status` telling the truth afterwards.

Nothing in that requirement mentions git, an index, or a mechanism. A commit
of a known pathspec is four object writes and a ref swap:

    blobs -> trees -> commit object -> compare-and-swap the ref

`.git/index` appears nowhere in it. The index is git's answer to a question a
HUMAN has -- *which of my changes go in this commit?* -- and this function is
handed that answer as an argument. So it reads the index for exactly one
reason (below) and writes it for exactly one reason (below), and never to
decide anything.

WHY THERE IS NO SECOND BRANCH. The old op chose between `git commit -- <paths>`
(worktree bytes) and a throwaway private index (staged bytes), and the whole
`diverging_paths`/`_mode_delta_paths_chunked` apparatus existed to pick. Once
blobs are resolved per path in process, "which bytes" is a dict lookup. The
question is deleted rather than made cheaper.

THE TWO INVARIANTS, which survive the rewrite because they are the point:

1. **Staged-content fidelity, DECLARED.** A caller that deliberately staged
   something else (a partial hunk, a `--chmod`) names those paths in
   `prefer_staged`, and their staged blob is committed rather than the
   worktree's. It is NOT inferred from "the index differs from the worktree":
   that is equally true of an ordinary unstaged edit, which is the common
   case and whose worktree bytes are exactly what the caller means. Inferring
   it committed a stale blob and left the worktree dirty -- caught by this
   module's own shape-B test.
2. **The CAS ref landing.** The ref moves only if it still points where it did
   when the tree was built, so a concurrent commit is never silently orphaned.

AND THE ONE THE OLD OP GOT FOR FREE FROM `git add`:

3. **The shared index stays honest.** `git add` was the only `.git/index`
   write in a pass. Landing a commit without it leaves HEAD ahead of the
   index, and every peer's `git status` then reports fiction -- a
   newly-committed path reads `D ` (in HEAD, absent from index) plus `??`,
   an edited one reads `MM`. Measured, all three shapes. So this function
   splices the index itself, after the ref lands, via `index_write`.

THE GUARDED SEAM (`coordinator_core.git.action_guard`, C2). This module used
to consult nothing at all where `block_subagent_commit`'s dispatched-
committer guard consults an ownership-scope predicate on the Bash-tool
path -- verified at C2's own HEAD: no ownership check, no `dispatch_checks`
import, no claim this seam existed. `commit_paths` now calls `action_guard.
assert_pathspec_shape_permitted` unconditionally on every commit's pathspec
(see the DEFAULT-PATH SHAPE CHECK comment below) -- the sweeping/orphan/
out-of-repo legs, which need no caller identity. The ownership leg this op
route cannot reach (C2a, measured: zero occurrences of `agent_id`/
`session_id`/`subagent` in this module or `ops/ceremony/commit_v2.py`) has
no parameter here to invoke it with -- see `action_guard.assert_
noncooperative_identity_available`'s own docstring for why a parameter
accepting a caller-supplied substitute would be exactly the fail-open shape
this seam exists to refuse, and why "this route can never resolve identity"
belongs in that docstring and the DR/plan prose, not in a `commit_paths`
argument (review: overengineering-reviewer Finding 2, 2026-08-30).
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable, Dict, Mapping, NamedTuple, Optional, Sequence, Tuple, Union

from coordinator_core.git import index_write
from coordinator_core.git.git_dir import resolve_git_dir, resolve_git_common_dir
from coordinator_core.git import checkin_attrs
from coordinator_core.git.content_hash import (
    _autocrlf_checkin_normalize,
    _clean_filter_may_apply,
    _repo_autocrlf_true,
    _text_attribute_pinned,
)
from coordinator_core.git.git_index import parse_index_identity
from coordinator_core.git.git_objects import cas_ref, read_packed_ref, write_object
from coordinator_core.git.git_state import head_sha, head_tree_sha, read_tree_spine
from coordinator_core.git.tree_spine import (
    _ABSENT,
    _rewrite_head_spine,
    _synthesize_absent_spine_dirs,
)

_DEFAULT_MODE = 0o100644
_EXEC_MODE = 0o100755


def _mode_for(path: Path) -> int:
    """`git add`'s own mode decision: exec bit set -> 100755, else 100644.
    Hardcoding 100644 silently dropped the exec bit off every newly-added
    script."""
    if os.name == "nt":
        # Windows reports the exec bit set on every file, so `st_mode` cannot
        # answer this -- deriving from it marked ordinary `.cmd` files 100755.
        # Git's own answer here is the index entry's recorded mode, which the
        # caller supplies; absent that, 100644.
        return _DEFAULT_MODE
    try:
        return _EXEC_MODE if (path.stat().st_mode & 0o111) else _DEFAULT_MODE
    except OSError:
        return _DEFAULT_MODE


class CommitRefused(Exception):
    """The commit did not land and nothing was written. Always safe to
    retry after reconciling -- no object, no ref move, no index write."""


class CommitOutcome(NamedTuple):
    sha: str
    #: Paths whose STAGED bytes were committed in preference to differing
    #: worktree bytes (invariant 1). The caller reports these; a silent
    #: substitution is how a deliberate partial stage gets lost.
    staged_preferred: Tuple[str, ...]
    #: The counterpart, and the one that can LOSE something: paths whose
    #: worktree bytes were committed while the index held DIFFERENT bytes the
    #: caller never declared via `prefer_staged`.
    #:
    #: Reporting only `staged_preferred` had the field pointing the safe way.
    #: A declared preference is safe by construction -- the caller asked for
    #: it. The undeclared divergence is the lossy direction, and it is exactly
    #: what changes hands at the `commit_scoped` -> `commit_paths` cutover:
    #: for the same pathspec, `commit_scoped` INFERS a partial stage from
    #: divergence and commits the index blob, while this function commits the
    #: worktree. Both cannot be right, and the disagreement was invisible in
    #: the outcome of either.
    #:
    #: This is a REPORT, never a refusal. Divergence does not identify intent
    #: (see invariant 1 below): the common case is an ordinary unstaged edit
    #: whose worktree bytes are precisely what the caller meant, and refusing
    #: those would make the safe default unusable. Free to compute -- the
    #: staged identity and the worktree blob are both already in hand.
    worktree_over_staged: Tuple[str, ...] = ()
    #: Declared paths that contributed NOTHING to this commit -- their bytes
    #: already matched HEAD (or, for a `deleted_paths` member, HEAD already
    #: lacked them). The commit is real and legitimate; these paths are not
    #: in it because there was nothing of theirs to put there.
    #:
    #: The k-of-N counterpart to `NothingToCommit`'s N-of-N. A caller that
    #: names five paths and gets four has lost the same thing an empty commit
    #: loses -- a path it believes it delivered -- and the usual cause is a
    #: hook or a peer having already committed that path moments earlier
    #: (DoE-claude's `874cf35dd`, where the plan `.md` the commit existed for
    #: was the missing one). NOT a refusal: a partial commit is legitimate and
    #: refusing it would break every ordinary scoped commit over a pathspec
    #: that is mostly unchanged.
    #:
    #: A FIELD IS NOT A SIGNAL (`commit_v2`'s own note). This one is only
    #: honest because `ceremony.commit_v2` raises it into `warnings` and
    #: `coordinator-safe-commit` prints it beside the sha -- a caller that
    #: must already suspect the bug to know to read the field reproduces the
    #: bug.
    no_delta: Tuple[str, ...] = ()
    #: The subset of `no_delta` that was declared DELETED and that HEAD did
    #: not have. Separated because the two halves of `no_delta` are not the
    #: same fact and only one of them is benign.
    #:
    #: A path whose bytes already match HEAD contributed nothing and nothing
    #: was owed. A path declared deleted that HEAD never carried is a
    #: DECLARATION THE CALLER COULD NOT HAVE MEANT -- there was no such file
    #: to delete -- and it is what an untracked path looks like after
    #: `coordinator-safe-commit :: _split_paths_for_commit_v2` misclassifies
    #: it from the wrong cwd: the new file the caller wanted committed is
    #: silently skipped instead (`state/audits/2026-08-31-committer-p0-*`).
    #:
    #: Kept OUT of `no_delta`'s own membership, deliberately: `NothingToCommit`
    #: and every existing reader key on that tuple, and narrowing it to fix a
    #: message would change refusal behaviour. This is additive.
    declared_absent_from_head: Tuple[str, ...] = ()


class NothingToCommit(CommitRefused):
    """The assembled tree is byte-identical to HEAD's -- this commit would
    change zero files. Refused rather than landed, and it is `CommitRefused`
    so nothing was written: no object, no ref move, no index write.

    WHY A REFUSAL AND NOT A FIELD. The success line is the only signal most
    callers have, and a zero-delta commit reported as `committed sha=<x>`
    reads as delivery to every one of them. That cost a real review pass:
    `ffcebec80` in DoE-claude reported an applied twelve-finding
    review-integration that had not landed, and it had to be re-authored from
    context. A new `CommitOutcome` field would have been just as invisible --
    a caller must already suspect the bug to know to read it, which is the
    bug. `git commit` itself refuses this without `--allow-empty`; this route
    now agrees. Deliberate marker commits opt in via `allow_empty=True`."""


class CommitDeniedByActionGuard(CommitRefused):
    """A pathspec-shape deny from `action_guard.assert_pathspec_shape_permitted`.

    `action_guard` raises THIS class directly, via a function-body lazy
    import (the same dodge it already uses twice to reach
    `block_subagent_commit`) -- collapsing the two-exception-types-for-one-
    deny shape (review: overengineering-reviewer Finding 7, 2026-08-30) that
    existed only to avoid `action_guard` importing this module at load time.
    That collapse became safe once Finding 1 deleted the two never-called
    `action_guard` functions whose standalone-importability was the shape's
    only stated reason to exist; `commit_paths` now calls `action_guard.
    assert_pathspec_shape_permitted` with no try/except of its own.

    A SUBCLASS of `CommitRefused`, so a guard deny is catchable BOTH ways at
    once: generically, by every existing `commit_paths` caller's
    `except (CommitRefused, FilterUnsupported)` (it IS a `CommitRefused`, so
    those five call sites need no edit), AND specifically, by the falsifier's
    behavioural probe (and any future caller) that needs to tell "the shape
    guard fired" apart from "some other check refused first" without
    string-matching a message."""


class FilterUnsupported(CommitRefused):
    """This path's bytes go through a filter this module does not reproduce
    (an LFS/`filter.*.clean` driver, or an explicit `text`/`eol` attribute
    pin). Refused rather than guessed: writing the RAW bytes would produce a
    blob sha git disagrees with, and the commit would look fine while the
    path reads permanently modified to every peer."""


def _worktree_blob(gitdir: Path, root: Path, rel: str, data: bytes) -> str:
    """The blob sha `git add` would have produced for `rel`, in process.

    `git add` does not record raw bytes -- it runs git's checkin filters, and
    on this box `core.autocrlf=true` means a CRLF worktree file hashes to its
    LF-normalized form. Writing the raw bytes instead yields a DIFFERENT sha,
    so the commit lands correct-looking while `git status` reports the path
    modified forever. This is the second face of a hazard that already bit
    this deliverable once: an LF-only fixture corpus kept a normalizer that
    misclassified 81% of real files on this box fully green.

    `_autocrlf_checkin_normalize` is used rather than reimplemented -- it was
    proven byte-identical to real `git hash-object` over 14 shapes.

    REFUSES, never guesses, for the two surfaces it cannot reproduce: a
    `filter.*.clean` driver (git-lfs, which replaces content with a pointer)
    and an explicit `text`/`eol` attribute pin. This repo's `.gitattributes`
    pins `*.cmd`, `*.ps1`, `*.sh`, `*.diff`, `*.patch` and `**/_goldens/**`,
    so these are reachable on ordinary commits here, not hypothetical.
    """
    lfs = _clean_filter_may_apply(root, rel)
    if lfs is not None:
        raise FilterUnsupported(
            f"{rel}: {lfs} -- this path's blob is produced by a clean filter "
            "this commit path does not run, so its sha cannot be computed in "
            "process. Refused rather than written wrong."
        )

    # REFUSE every path whose checkin conversion this module has not been
    # PROVEN to reproduce. Measured against `git hash-object -w --path=<p>`:
    # an attribute-aware attempt (eol=crlf / eol=lf / -text) produced four
    # WRONG shas, so the attribute surface is not solved and pretending
    # otherwise commits different bytes than the caller wrote.
    #
    # Total refusal before narrow refusal. A refused path falls to the
    # spawning `_hash_worktree_blobs` ladder, which is why that ladder stays.
    # Widen this ONLY against a corpus run: an LF-only fixture set already let
    # a normaliser misclassifying 81% of this box's files hold a 68/68 suite
    # green, and that is the same hazard wearing a different hat.
    disposition = checkin_attrs.checkin_disposition(root, rel)
    if disposition == checkin_attrs.BINARY:
        # `-text` -- no checkin conversion at all, so the raw bytes are
        # always what git would have hashed. Zero cost, no CR check needed.
        return write_object(gitdir, b"blob", data)
    if disposition == checkin_attrs.TEXT:
        # `text` / `text=auto` / `eol=lf` / `eol=crlf` -- checkin always
        # normalizes CRLF -> LF (checkin_attrs.py's own point: the two `eol=`
        # spellings differ only on checkout). CR-free content has nothing to
        # normalize, so it is byte-identical to what git would write and
        # costs nothing. CR-bearing content under this pin (this repo's
        # `*.cmd`/`*.ps1` -> `eol=crlf` shape, measured with real CRLF
        # bytes) is the surface `_autocrlf_checkin_normalize`'s corpus was
        # never proven against -- an attribute-aware attempt at it produced
        # wrong shas (see module docstring), so it is refused to the
        # batched fallback rather than guessed.
        if bytes([13]) not in data:
            return write_object(gitdir, b"blob", data)
        raise FilterUnsupported(
            f"{rel}: a text/eol attribute ({disposition}) pins this path's "
            "checkin conversion and its content contains CR bytes -- "
            "normalizing that in process is not proven byte-identical to "
            "git, so it is refused to the batched fallback rather than "
            "guessed."
        )
    if disposition == checkin_attrs.UNRESOLVED:
        raise FilterUnsupported(
            f"{rel}: an [attr] macro governs this path's checkin attribute "
            "and is not resolved in process. Refused rather than guessed."
        )

    # UNSET -- no attribute matched, so `core.autocrlf` decides. A CR byte
    # here is the shape `_autocrlf_checkin_normalize`'s corpus DID cover
    # under autocrlf=true; with autocrlf off or unresolvable, git stores the
    # bytes as-is and so do we.
    if bytes([13]) in data:
        if _repo_autocrlf_true(root):
            return write_object(gitdir, b"blob", _autocrlf_checkin_normalize(data))
        raise FilterUnsupported(
            f"{rel}: contains CR bytes -- checkin normalization for this path "
            "is not reproduced in process, and the raw bytes hash to a blob "
            "git disagrees with. Refused rather than silently committing "
            "different content."
        )
    return write_object(gitdir, b"blob", data)


def hash_worktree_blobs_via_spawn(
    paths: Sequence[str],
    *,
    cwd: Union[str, Path],
) -> Dict[str, str]:
    """The ONE-spawn `blob_fallback` for `commit_paths`/`stage_paths_in_process`:
    `git hash-object -w --stdin-paths`, batched over every refused path in a
    single call regardless of how many there are (the path list travels over
    stdin, not argv, so there is no Windows argv-length cap to chunk against).

    Restated here, small, rather than imported from
    `coordinator_core.ops.ceremony.git_native`'s `_hash_object_stdin_paths` /
    `_hash_worktree_blobs` -- `commit_v2`'s handler (this module's caller) is
    barred from importing `commit_pipeline.py` or `git_native.py` in any form
    (docs/plans/2026-08-27-something-must-commit-ceremony-commit-v2.md, C3
    body), so the ONE-spawn shape those modules already proved is restated
    directly against git, not re-derived.

    Returns `{path: blob_sha}` for every path in `paths`, in the same order
    `paths` was given (git's own `--stdin-paths` output-order contract).
    Raises `CommitRefused` on a non-zero exit or a sha count that does not
    match the path count -- never returns a partial or misaligned mapping.
    """
    if not paths:
        return {}
    from coordinator_core.git.run import run_git

    stdin_data = ("\n".join(paths) + "\n").encode("utf-8", "surrogateescape")
    result = run_git(["hash-object", "-w", "--stdin-paths"], cwd=str(cwd), input=stdin_data)
    if result.returncode != 0:
        raise CommitRefused(
            "git hash-object -w --stdin-paths failed for "
            f"{len(paths)} path(s): {result.stderr.strip()}"
        )
    shas = [line for line in result.stdout.splitlines() if line]
    if len(shas) != len(paths):
        raise CommitRefused(
            "git hash-object -w --stdin-paths returned "
            f"{len(shas)} sha(s) for {len(paths)} refused path(s) -- "
            "refusing to guess an alignment"
        )
    return dict(zip(paths, shas))


def _identity() -> Tuple[str, str]:
    name = os.environ.get("GIT_COMMITTER_NAME") or os.environ.get("GIT_AUTHOR_NAME")
    email = os.environ.get("GIT_COMMITTER_EMAIL") or os.environ.get("GIT_AUTHOR_EMAIL")
    return name or "coordinator", email or "coordinator@local"


def _stamp() -> str:
    now = int(time.time())
    off = -(time.altzone if time.daylight and time.localtime().tm_isdst else time.timezone)
    sign = "+" if off >= 0 else "-"
    off = abs(off)
    return f"{now} {sign}{off // 3600:02d}{(off % 3600) // 60:02d}"


def _cas_target(repo: Union[str, Path]) -> Optional[Tuple[Path, str]]:
    """`(gitdir, ref)` for the ref this commit must swap, or None.

    A packed-only ref (the post-`git pack-refs` shape) resolves here rather
    than refusing: `cas_ref` reads its comparand out of `packed-refs` and
    writes a loose ref that shadows it, which is what git itself does on the
    first ref update after a pack.
    """
    worktree_gitdir = resolve_git_dir(repo)
    try:
        head_text = (worktree_gitdir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not head_text.startswith("ref:"):
        return worktree_gitdir, "HEAD"
    ref_rel = head_text[len("ref:"):].strip()
    common = resolve_git_common_dir(repo)
    if not (common / ref_rel).is_file() and read_packed_ref(common, ref_rel) is None:
        return None
    return common, ref_rel


_REQUIRED = object()


def commit_paths(
    repo: Union[str, Path] = _REQUIRED,  # type: ignore[assignment]
    paths: Sequence[str] = _REQUIRED,  # type: ignore[assignment]
    message: str = _REQUIRED,  # type: ignore[assignment]
    *,
    repo_root: Union[str, Path, None] = None,
    deleted_paths: Sequence[str] = (),
    supplied_blobs: Optional[Mapping[str, str]] = None,
    prefer_staged: Sequence[str] = (),
    prefer_deliberate_stage: bool = False,
    allow_empty: bool = False,
    blob_fallback: Optional[Callable[[Sequence[str]], Mapping[str, str]]] = None,
) -> CommitOutcome:
    """Commit exactly `paths` (+ remove `deleted_paths`). Zero git spawns.

    Raises `CommitRefused` without writing anything on: an empty pathspec (an
    empty pathspec to `git commit` commits the WHOLE INDEX, so it is refused
    here rather than defaulted), a directory in `paths`, an unresolvable CAS
    ref, a lost CAS race, or a tree identical to HEAD's (`NothingToCommit`,
    unless `allow_empty=True` -- see that class).

    `prefer_deliberate_stage` (DR-379): a caller-declared opt-in, DEFAULT
    FALSE, that turns the already-computed `worktree_over_staged` set from a
    report into a substitution. When true, every path this call would
    otherwise have named passed-over instead commits its staged bytes
    (`entry.mode`, `entry.sha`), landing inside the existing settle-against-
    HEAD window -- no extra spawn, no extra object read, no extra index read.
    A path this policy preserves is not "passed over", so it moves out of
    `worktree_over_staged` and into `staged_preferred` alongside a
    caller-declared `prefer_staged` path: both are the same observable
    outcome (staged bytes committed), just declared through a different
    door. Opt in only where a third party's deliberate partial stage can
    exist to be preserved (`ops/session/safe_commit_offer.py`,
    `coordinator/bin/coordinator-safe-commit.py`) -- every other caller
    commits paths it authored itself in the same pass, so there is nothing
    a widened default could preserve, only stale blobs it could newly hide.

    WHY `repo_root` IS ACCEPTED AS AN ALIAS FOR `repo`. This function is the
    sanctioned leg-1 route for the dispatched committer, whose call block
    lives in a doc claude-klabauter does not own (`agents/git-commit-agent.md`,
    `snippets/scoped-commit-route.md` -- both name the parameter
    `repo_root`). A `TypeError` here does not read to that agent as "wrong
    keyword": it reads as *leg 1 is unavailable*, and it drops to the plain
    `git commit -- <paths>` fallback, which the subagent commit guard then
    denies -- so an entire dispatched workflow halts at its commit phase with
    both legs apparently dead. Every emitted plan wave is gated behind that
    phase. `repo_root` is also the name `ops/dispatch_emit/emit.py` uses for
    the same value throughout, so the collision is systemic rather than one
    doc's typo. Accepting it costs a branch and removes the halt; the correct
    path is made reachable rather than the wrong one walled off. Supplying
    both is a caller confusion, not a shorthand, and still raises.

    `paths` is likewise optional so a deletion-only commit -- documented as
    legal ("at least one of `paths` / `deleted_paths`") -- reaches the empty-
    pathspec refusal below on its own terms instead of a missing-argument
    `TypeError` the caller reads the same wrong way.
    """
    if repo is _REQUIRED:
        if repo_root is None:
            raise TypeError(
                "commit_paths() missing the repo root: pass it positionally, "
                "as `repo=`, or as the alias `repo_root=`"
            )
        repo = repo_root
    elif repo_root is not None:
        raise TypeError(
            "commit_paths() got both `repo` and its alias `repo_root` -- pass "
            "one; they name the same value and disagreeing is a caller bug"
        )
    if paths is _REQUIRED:
        paths = ()
    if message is _REQUIRED:
        raise TypeError("commit_paths() missing required argument: 'message'")

    root = Path(repo)
    gitdir = resolve_git_dir(repo)
    supplied = dict(supplied_blobs or {})

    path_list = [_index_key(root, p) for p in paths]
    delete_list = [_index_key(root, p) for p in deleted_paths]
    if not path_list and not delete_list:
        raise CommitRefused(
            "empty pathspec -- refused, never defaulted: an empty pathspec "
            "commits the whole index rather than nothing"
        )
    for p in path_list:
        if (root / p).is_dir():
            raise CommitRefused(
                f"{p} is a directory -- pass explicit file paths; a directory "
                "pathspec matches whatever lands inside it at commit time, "
                "including a peer's file added after the caller computed it"
            )

    # PHANTOM DELETION -- a declared deletion for a path the worktree still
    # has. The stale-shared-index shape this refuses (DoE-claude's
    # `guard-phantom-staged-deletion-precommit.py`, filed against
    # `state/bug-backlog/2026-08-28-a-stale-shared-index-arms-a-phantom-
    # deletion-of-any-freshly-committed-path.yaml`) lands a removal in HEAD
    # for a file that is sitting on disk, so the next reader finds the path
    # untracked and the history saying it was deleted on purpose. That guard
    # is a NATIVE pre-commit hook and this route fires no native hook -- 82%
    # of commits measured on a shared branch come through here -- so the
    # refusal has to live at the in-process seam or it covers almost nothing.
    #
    # Absolute, with no caller opt-out, because no caller wants the other
    # side of it: every producer derives its deletions from absence
    # (`publish.py`, `safe_commit_offer.py`'s `_split_paths_for_commit_v2`,
    # `directives_commit_tail.py`), and the one that derived from HEAD
    # membership alone (`memo_send.py`) was declaring a deletion it could not
    # know had happened -- fixed at its own site rather than tolerated here.
    # An untrack-but-keep ("git rm --cached") has no caller and gets no
    # parameter: add one when a caller needs it, and name it there.
    #
    # One `exists()` per DECLARED deletion, never per path in the pathspec:
    # an ordinary commit declares none and pays nothing.
    for p in delete_list:
        if (root / p).exists():
            raise CommitRefused(
                f"{p} is declared deleted but still present in the worktree "
                "-- drop it from `deleted_paths`, or remove the file first"
            )

    # DEFAULT-PATH SHAPE CHECK (C2): the sweeping/orphan/out-of-repo legs of
    # `block_subagent_commit`'s guard predicate -- see `action_guard.assert_
    # pathspec_shape_permitted`'s own docstring. Called unconditionally: every
    # `commit_paths` call gets its pathspec's shape checked. This does NOT
    # reach `assert_paths_in_session_scope` (the ownership leg) -- that leg
    # has no parameter on this route to reach it at all (C2a: no verified
    # identity to check it against; see `action_guard.assert_noncooperative_
    # identity_available`'s docstring).
    #
    # No try/except here (review: overengineering-reviewer Finding 7,
    # 2026-08-30): `action_guard` raises `CommitDeniedByActionGuard` --
    # this module's own exception, lazy-imported inside `action_guard`'s
    # function body -- directly, so there is nothing to catch and re-raise.
    # Every known `commit_paths` caller (`commit_v2.py`, `close_out_and_
    # stamp.py`, `memo_send.py`, `directives_commit_tail.py`, `safe_commit_
    # offer.py`) catches `(CommitRefused, FilterUnsupported)`, and
    # `CommitDeniedByActionGuard` IS a `CommitRefused`, so none of the five
    # need an edit.
    from coordinator_core.git import action_guard

    action_guard.assert_pathspec_shape_permitted(
        path_list + delete_list, False, str(root)
    )

    # MID-SEQUENCE REFUSAL, and it has to sit HERE -- before the index read,
    # before the first object write, before the CAS. This function builds a
    # commit with exactly one parent (HEAD). In a repo partway through a
    # merge, cherry-pick, or revert, the pending operation's other parent is
    # recorded ONLY in `<gitdir>/MERGE_HEAD` (etc.), so landing that
    # single-parent commit silently drops it: the merge disappears from
    # history, the sequencer file is left dangling, and the index -- still
    # carrying stage != 0 entries -- then fails `splice_index`, surfacing as
    # `IndexStaleAfterCommit` AFTER the wrong commit is already unreachable-
    # by-nothing. Observed 2026-09-02: a percolate round pulled
    # origin/candidate into `claude-klabauter`, conflicted on 31 generated
    # paths, and committed over the top; the merge parent survived only
    # because the pending commit was still on the remote.
    #
    # Three `exists()` calls on a path already resolved, zero spawns: an
    # ordinary commit into a settled repo pays three stats.
    for _seq_file, _seq_verb in (
        ("MERGE_HEAD", "merge"),
        ("CHERRY_PICK_HEAD", "cherry-pick"),
        ("REVERT_HEAD", "revert"),
    ):
        if (gitdir / _seq_file).exists():
            raise CommitRefused(
                f"{repo} is partway through a {_seq_verb} "
                f"(`{_seq_file}` is present) -- refusing to commit, because "
                "this route writes a single-parent commit and would drop the "
                f"pending parent from history. Finish the {_seq_verb} (`git "
                f"commit`) or abandon it (`git {_seq_verb} --abort`), then "
                "run this again."
            )

    # THE ONE INDEX READ, and it decides nothing about mechanism: it answers
    # invariant 1 for exactly the k paths in this call. Scoped, so it never
    # materialises an entry outside `paths`.
    staged = parse_index_identity(repo, wanted=set(path_list))

    assembled: Dict[str, object] = {}
    index_updates: Dict[str, object] = {}
    staged_preferred = []
    staged_passed_over: list = []
    refused: list = []

    prefer_staged_set = {p.replace("\\", "/") for p in prefer_staged}
    delete_set = set(delete_list)
    for p in path_list:
        if p in delete_set:
            # DECLARED DELETION WINS over the content read, and it has to win
            # HERE rather than at the `assembled` write below: a path named in
            # BOTH argument lists is how a caller says "this member of my
            # pathspec is the deletion" -- the shape `run_commit_pipeline`
            # required (its `deleted_paths` verified a claim `stage_paths`
            # had to stage) and the shape every caller carrying that habit
            # sends. Reading it first turned that declaration into `cannot
            # read <path>`, a refusal naming an unreadable file for a commit
            # that had declared the file gone.
            continue
        entry = staged.get(p)
        if p in supplied:
            blob = supplied[p]
            mode = entry.mode if entry is not None else _mode_for(root / p)
        elif p in prefer_staged_set and entry is not None:
            # INVARIANT 1, and it is DECLARED, never inferred. "The index
            # differs from the worktree" does NOT identify a deliberate
            # partial stage -- it is equally true of an ordinary unstaged
            # edit, which is the common case and whose worktree bytes are
            # exactly what the caller means. Inferring here committed the
            # stale index blob and left the worktree modified (fd shape B).
            # A caller that deliberately staged something else says so.
            blob = entry.sha
            mode = entry.mode
            staged_preferred.append(p)
        else:
            try:
                data = (root / p).read_bytes()
            except FileNotFoundError as exc:
                if entry is not None:
                    # The caller named a path git still tracks and the worktree
                    # no longer has: a deletion, undeclared. Naming the
                    # parameter is the difference between a refusal the caller
                    # can act on and an errno they have to guess at.
                    raise CommitRefused(
                        f"{p} is gone from the worktree but still tracked -- "
                        "pass it in `deleted_paths` to commit the deletion"
                    ) from exc
                raise CommitRefused(f"cannot read {p}: {exc}") from exc
            except OSError as exc:
                raise CommitRefused(f"cannot read {p}: {exc}") from exc
            try:
                blob = _worktree_blob(gitdir, root, p, data)
            except FilterUnsupported:
                # COLLECT, don't explode. A refusal is "this module cannot
                # compute this path's blob", not "this commit fails" -- the
                # caller supplies a fallback that can. Every refused path in
                # the pass is gathered and resolved in ONE batch below, so the
                # fallback costs one process for the whole set rather than one
                # per path (and zero if nothing is refused).
                refused.append(p)
                continue
            mode = entry.mode if entry is not None else _mode_for(root / p)
            if entry is not None and blob != entry.sha:
                # CANDIDATE ONLY -- the worktree differs from the index, which
                # is NOT yet evidence that anything was deliberately staged.
                # It is equally true of an ordinary unstaged edit, where the
                # index still holds HEAD's bytes. Settled against HEAD below,
                # once the tree spine (already read for the commit itself) is
                # in hand.
                staged_passed_over.append(p)
        assembled[p] = (mode, blob)
        index_updates[p] = (mode, blob)

    if refused:
        if blob_fallback is None:
            raise FilterUnsupported(
                f"{len(refused)} path(s) need a checkin conversion this module "
                f"does not reproduce ({', '.join(refused[:5])}"
                + (", ..." if len(refused) > 5 else "")
                + ") and no `blob_fallback` was supplied. Pass one -- "
                "`git_native._hash_worktree_blobs` is the batched, one-spawn "
                "resolver the commit pipeline injects."
            )
        resolved = blob_fallback(refused)
        for p in refused:
            blob = resolved.get(p)
            if not blob:
                raise CommitRefused(
                    f"{p}: the blob fallback returned no sha for a path this "
                    "module refused. Nothing was written."
                )
            entry = staged.get(p)
            mode = entry.mode if entry is not None else _mode_for(root / p)
            if entry is not None and blob != entry.sha:
                # SAME CANDIDACY CHECK as the direct-blob branch above -- a
                # path refused to `blob_fallback` (LFS/CRLF-pinned/`[attr]`)
                # can diverge from a partial stage exactly like a directly
                # hashed one, and was silently absent from the loss report
                # before this. Settled against HEAD in the same pass below,
                # off the same spine -- no extra spawn, no extra read.
                staged_passed_over.append(p)
            assembled[p] = (mode, blob)
            index_updates[p] = (mode, blob)

    for p in delete_list:
        assembled[p] = _ABSENT
        index_updates[p] = index_write.ABSENT

    old_head = head_sha(repo)
    spine = read_tree_spine(repo, list(assembled))
    if spine is None:
        raise CommitRefused("could not read HEAD's tree spine")
    # SETTLE THE CANDIDATES AGAINST HEAD, off the spine that was read for the
    # commit anyway -- no extra spawn, no extra object read, and the "zero git
    # spawns" contract is untouched.
    #
    # The discriminator is index-vs-HEAD, never index-vs-worktree. A path whose
    # index entry still equals HEAD was never deliberately staged: the worktree
    # simply moved on, which is an ordinary edit and the common case. Only a
    # path whose index differs from HEAD had something put there on purpose,
    # and only that path loses anything by committing the worktree instead.
    # Reporting the wider set would fire on nearly every commit and train its
    # reader to ignore the field, which is the same silence by another route.
    worktree_over_staged = []
    for p in staged_passed_over:
        head_dir, _, head_name = p.rpartition("/")
        head_entry = spine.get(head_dir, {}).get(head_name)
        entry = staged.get(p)
        if entry is not None and (head_entry is None or head_entry[1] != entry.sha):
            if prefer_deliberate_stage:
                # DR-379: the settled set is a substitution here, not just a
                # report -- the caller declared it wants the deliberate stage
                # preserved rather than passed over. Same window, same spine,
                # same entry already in hand: no extra read.
                assembled[p] = (entry.mode, entry.sha)
                index_updates[p] = (entry.mode, entry.sha)
                staged_preferred.append(p)
            else:
                worktree_over_staged.append(p)

    # THE DELTA PASS, off the spine already in hand -- no extra spawn, no
    # extra object read, and it runs BEFORE any tree is written, so the
    # refusal below leaves nothing behind. `assembled` is final at this point:
    # `prefer_deliberate_stage` above is the last thing that can substitute
    # into it, and its substitutions are exactly the ones that can turn a
    # would-be delta back into a no-op.
    #
    # Compared entry-for-entry rather than root-tree-to-root-tree because the
    # latter needs the rewrite the refusal is meant to precede. The comparison
    # is exact: `assembled`'s values are the same `(mode, sha)` tuples
    # `_rewrite_head_spine` splices into the spine.
    #
    # EVERY path is checked, not just enough of them to prove one differs. The
    # early exit was cheaper and threw away the k-of-N answer: a commit where
    # four of five declared paths changed is a REAL commit and must land, but
    # the caller named five and got four, and the fifth is exactly as
    # invisible as an all-empty commit was. DoE-claude's `874cf35dd` is the
    # worked case -- five paths, four landed, and the plan `.md` that was the
    # point of the commit contributed nothing because a status-transition hook
    # had already committed it moments earlier. Same failure as the empty
    # commit, one notch narrower, and the loop was already computing the fact
    # per path before discarding it.
    no_delta = []
    declared_absent_from_head = []
    for p, val in assembled.items():
        head_dir, _, head_name = p.rpartition("/")
        head_entry = spine.get(head_dir, {}).get(head_name)
        if val is _ABSENT:
            if head_entry is None:
                no_delta.append(p)
                declared_absent_from_head.append(p)
        elif head_entry == val:
            no_delta.append(p)

    if not allow_empty and len(no_delta) == len(assembled):
        raise NothingToCommit(
            f"nothing to commit -- all {len(assembled)} path(s) already "
            "match HEAD. Refused: a commit with no diff reports as "
            "delivery to every caller reading the success line. Pass "
            "allow_empty=True for a deliberate marker commit."
        )

    filled = _synthesize_absent_spine_dirs(spine, assembled)
    if filled is not None:
        spine = filled
    root_tree = _rewrite_head_spine(gitdir, spine, assembled)
    if root_tree is None:
        raise CommitRefused(
            "the tree spine could not be rewritten for this pathspec -- a "
            "refusal, not a lost commit: nothing was written, HEAD is "
            "unmoved, and the shared index was never touched"
        )

    if not allow_empty and root_tree == head_tree_sha(repo):
        # THE BACKSTOP, and it is deliberately redundant with the per-path
        # delta pass above. That pass answers "does each declared path differ" by
        # comparing entries; this one answers the only question the caller
        # actually asked -- "does this commit change the repository" -- off the
        # object git itself would compare. Any shape where the per-path
        # comparison misses (a lookup that does not find HEAD's entry where
        # HEAD has one, and so reads as a delta) lands an empty commit
        # reporting `committed sha=<x>`, which is `NothingToCommit`'s whole
        # reason to exist arriving through a door it does not watch. One
        # object read, no spawn, and it runs before any ref moves.
        raise NothingToCommit(
            "nothing to commit -- the assembled tree is HEAD's own tree. "
            "Refused: a commit with no diff reports as delivery to every "
            "caller reading the success line. Pass allow_empty=True for a "
            "deliberate marker commit."
        )

    name, email = _identity()
    who = f"{name} <{email}> {_stamp()}"
    body = f"tree {root_tree}\n"
    if old_head:
        body += f"parent {old_head}\n"
    body += f"author {who}\ncommitter {who}\n\n{message}"
    if not body.endswith("\n"):
        body += "\n"
    commit_sha = write_object(gitdir, b"commit", body.encode("utf-8", "surrogateescape"))

    target = _cas_target(repo)
    if target is None:
        raise CommitRefused("could not resolve the ref to compare-and-swap")
    ref_gitdir, ref = target
    if not cas_ref(ref_gitdir, ref, old_head, commit_sha,
                   reflog_committer=f"{name} <{email}>",
                   reflog_message=message.splitlines()[0] if message.strip() else "commit",
                   head_gitdir=gitdir):
        raise CommitRefused(
            f"compare-and-swap failed -- {ref} moved since {old_head} was "
            "captured. Refusing to retry silently: a retry needs the tree "
            "rebuilt against the new HEAD, and reusing this one would commit "
            "past a peer."
        )

    # INVARIANT 3, and it must follow the ref, not precede it: an index that
    # matches a commit which never landed is the same lie in the other
    # direction. A failure here leaves a correct commit and a stale index --
    # recoverable by any `git add`/`git status` -- so it is reported, never
    # rolled back onto a landed commit.
    #
    # "REPORTED" HAS TO MEAN A TYPE THE CALLER CAN TELL APART. The bare
    # `index_write` error escaping here says `IndexWriteLockBusy`, whose own
    # docstring promises the opposite of what is true at THIS line: that it
    # was "raised BEFORE any bytes reach `.git/index` and before the ref
    # moves, so retrying is correct there". Past the `cas_ref` above, the ref
    # HAS moved. Every `commit_paths` caller in the tree catches
    # `(CommitRefused, FilterUnsupported)` and nothing else, so the lock-busy
    # propagated as a raw internal error for a commit that had LANDED -- and
    # the honest response to an internal error is a retry, which commits the
    # same work twice.
    #
    # `IndexStaleAfterCommit` was written for exactly this outcome ("the one
    # outcome on this surface that must not be retried") and had no raise
    # site, so the word existed and was never said. This is that site. At the
    # ~50-session load norm a peer holding `.git/index.lock` for the width of
    # a splice is routine, not exotic; example-retrieval-repo-em observed the memo.send
    # face of it on 2026-09-01 (`cross-repo/inbox/2026-09-01-example-retrieval-repo-em-
    # ceremony-engine-defects-second-repo-confirmation.md`).
    outcome = CommitOutcome(
        sha=commit_sha,
        staged_preferred=tuple(staged_preferred),
        worktree_over_staged=tuple(worktree_over_staged),
        no_delta=tuple(no_delta),
        declared_absent_from_head=tuple(declared_absent_from_head),
    )
    try:
        index_write.splice_index(repo, index_updates)
    except index_write.IndexWriteError as exc:
        raise index_write.IndexStaleAfterCommit(
            f"commit {commit_sha} LANDED; the index splice did not: {exc}. "
            "Do not retry -- the work is in history. Any subsequent "
            "`git add`/`git status` refreshes the index.",
            outcome=outcome,
        ) from exc

    return CommitOutcome(
        sha=commit_sha,
        staged_preferred=tuple(staged_preferred),
        worktree_over_staged=tuple(worktree_over_staged),
        no_delta=tuple(no_delta),
        declared_absent_from_head=tuple(declared_absent_from_head),
    )


def _index_key(root: Path, raw_path: str) -> str:
    """The `.git/index` name for `raw_path`: repo-relative, forward slashes.

    Callers hand paths in either spelling -- the pipeline threads absolute
    paths through for a caller-named file and repo-relative ones for its own.
    An absolute path used as an index key matches no entry, so a deletion
    named that way was recorded against a name the index does not contain and
    silently vanished.
    """
    p = raw_path.replace("\\", "/")
    candidate = Path(p)
    if not candidate.is_absolute():
        return p
    try:
        return candidate.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        raise CommitRefused(f"path is outside the repository: {raw_path}")


def stage_paths_in_process(
    repo: Union[str, Path],
    paths: Sequence[str],
    deleted_paths: Sequence[str] = (),
    blob_fallback: Optional[Callable[[Sequence[str]], Mapping[str, str]]] = None,
) -> Tuple[str, ...]:
    """Stage `paths` (+ record `deleted_paths` as removals) with ZERO spawns.

    The in-process replacement for `git add -- <paths>`, which was the only
    `.git/index` write in a commit pass and ~20.3ms of process creation. Same
    observable effect: after this returns, `git status` reports these paths
    staged, so every gate that asks "is this staged" keeps its meaning.

    Returns the paths actually staged. Raises `IndexWriteLockBusy` (from
    `index_write`) if a peer holds `.git/index.lock` -- a refusal, never a
    steal, matching `git add`'s own behaviour under contention.

    NOT a general `git add`: no pathspec globbing, no `-A`, no directory
    recursion. Explicit files only, which is all the commit path ever passes
    and all a shared tree should ever accept.
    """
    root = Path(repo)
    gitdir = resolve_git_dir(repo)
    updates: Dict[str, object] = {}
    staged = []
    refused: list = []
    spelling: Dict[str, str] = {}
    keys = [_index_key(root, raw_path) for raw_path in paths]
    # `git add`'s own mode decision for a TRACKED path is the index's
    # existing entry, not a stat: on Windows `core.fileMode=false` makes
    # the worktree bit uninformative (`_mode_for` always answers 100644
    # there), so re-staging an executable file already tracked at 100755
    # would otherwise silently drop it back to 100644.
    existing = parse_index_identity(repo, wanted=set(keys))
    for raw_path, p in zip(paths, keys):
        spelling[p] = raw_path
        target = root / p
        if target.is_dir():
            # `IsADirectoryError` is an `OSError`, so without this a directory
            # handed in as a path would fall into the deletion branch below and
            # silently stage the removal of a name that is not a file. Refuse
            # loudly instead -- this module takes explicit files only.
            raise CommitRefused(f"path is a directory, not a file: {raw_path}")
        try:
            data = target.read_bytes()
        except OSError:
            # ABSENT FROM DISK IS A DELETION, NOT A SKIP. `git add <path>` on a
            # removed file stages the removal, and the pipeline relies on that
            # -- it puts deleted paths in `to_stage` for exactly this reason.
            # Skipping them silently left every deletion unstaged and the
            # dirty-tree gate then reported the path unattributable.
            updates[p] = index_write.ABSENT
            staged.append(raw_path)
            continue
        entry = existing.get(p)
        mode = entry.mode if entry is not None else _mode_for(root / p)
        try:
            updates[p] = (mode, _worktree_blob(gitdir, root, p, data))
        except FilterUnsupported:
            refused.append(p)
            continue
        staged.append(raw_path)
    if refused:
        if blob_fallback is None:
            raise FilterUnsupported(
                f"{len(refused)} path(s) need a checkin conversion this module "
                "does not reproduce and no `blob_fallback` was supplied: "
                + ", ".join(refused[:5])
            )
        resolved = blob_fallback(refused)
        # The keys are INDEX KEYS, and that is checked rather than assumed.
        # `_index_key` exists because an unnormalized path written into
        # `updates` produces an entry the index does not contain -- silently.
        # Trusting a caller-supplied resolver to have normalized its own keys
        # would leave exactly that hole open on the fallback leg.
        unknown = set(resolved) - set(refused)
        if unknown:
            raise CommitRefused(
                "blob_fallback returned key(s) that were not asked for, so "
                "they are not index keys: " + ", ".join(sorted(unknown)[:5])
            )
        missing = set(refused) - set(resolved)
        if missing:
            raise CommitRefused(
                "blob_fallback resolved no blob for: "
                + ", ".join(sorted(missing)[:5])
            )
        for rel, sha in resolved.items():
            entry = existing.get(rel)
            mode = entry.mode if entry is not None else _mode_for(root / rel)
            updates[rel] = (mode, sha)
            staged.append(spelling.get(rel, rel))

    for raw_path in deleted_paths:
        updates[_index_key(root, raw_path)] = index_write.ABSENT
    if updates:
        index_write.splice_index(repo, updates)
    return tuple(staged)
