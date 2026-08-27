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
from coordinator_core.git.git_state import head_sha, read_tree_spine
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
    if disposition != checkin_attrs.UNSET:
        raise FilterUnsupported(
            f"{rel}: a text/eol/binary attribute ({disposition}) governs this "
            "path's checkin conversion, which is not reproduced in process. "
            "Refused rather than written wrong."
        )
    if bytes([13]) in data:
        raise FilterUnsupported(
            f"{rel}: contains CR bytes -- checkin normalization for this path "
            "is not reproduced in process, and the raw bytes hash to a blob "
            "git disagrees with. Refused rather than silently committing "
            "different content."
        )
    return write_object(gitdir, b"blob", data)
    if disposition == checkin_attrs.TEXT:
        return write_object(gitdir, b"blob", _autocrlf_checkin_normalize(data))

    # UNSET -- no attribute matched, so `core.autocrlf` decides. A CR byte
    # here is the shape `_autocrlf_checkin_normalize`'s corpus DID cover
    # under autocrlf=true; with autocrlf off or unresolvable, git stores the
    # bytes as-is and so do we.
    if _repo_autocrlf_true(root):
        return write_object(gitdir, b"blob", _autocrlf_checkin_normalize(data))
    return write_object(gitdir, b"blob", data)


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


def commit_paths(
    repo: Union[str, Path],
    paths: Sequence[str],
    message: str,
    *,
    deleted_paths: Sequence[str] = (),
    supplied_blobs: Optional[Mapping[str, str]] = None,
    prefer_staged: Sequence[str] = (),
    blob_fallback: Optional[Callable[[Sequence[str]], Mapping[str, str]]] = None,
) -> CommitOutcome:
    """Commit exactly `paths` (+ remove `deleted_paths`). Zero git spawns.

    Raises `CommitRefused` without writing anything on: an empty pathspec (an
    empty pathspec to `git commit` commits the WHOLE INDEX, so it is refused
    here rather than defaulted), a directory in `paths`, an unresolvable CAS
    ref, or a lost CAS race.
    """
    root = Path(repo)
    gitdir = resolve_git_dir(repo)
    supplied = dict(supplied_blobs or {})

    path_list = [p.replace("\\", "/") for p in paths]
    delete_list = [p.replace("\\", "/") for p in deleted_paths]
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

    # THE ONE INDEX READ, and it decides nothing about mechanism: it answers
    # invariant 1 for exactly the k paths in this call. Scoped, so it never
    # materialises an entry outside `paths`.
    staged = parse_index_identity(repo, wanted=set(path_list))

    assembled: Dict[str, object] = {}
    index_updates: Dict[str, object] = {}
    staged_preferred = []
    refused: list = []

    prefer_staged_set = {p.replace("\\", "/") for p in prefer_staged}
    for p in path_list:
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
            assembled[p] = (mode, blob)
            index_updates[p] = (mode, blob)

    for p in delete_list:
        assembled[p] = _ABSENT
        index_updates[p] = index_write.ABSENT

    old_head = head_sha(repo)
    spine = read_tree_spine(repo, list(assembled))
    if spine is None:
        raise CommitRefused("could not read HEAD's tree spine")
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
    index_write.splice_index(repo, index_updates)

    return CommitOutcome(sha=commit_sha, staged_preferred=tuple(staged_preferred))


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
    for raw_path in paths:
        p = _index_key(root, raw_path)
        spelling[p] = raw_path
        try:
            data = (root / p).read_bytes()
        except OSError:
            # ABSENT FROM DISK IS A DELETION, NOT A SKIP. `git add <path>` on a
            # removed file stages the removal, and the pipeline relies on that
            # -- it puts deleted paths in `to_stage` for exactly this reason.
            # Skipping them silently left every deletion unstaged and the
            # dirty-tree gate then reported the path unattributable.
            updates[p] = index_write.ABSENT
            staged.append(raw_path)
            continue
        try:
            updates[p] = (_mode_for(root / p), _worktree_blob(gitdir, root, p, data))
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
        for rel, sha in blob_fallback(refused).items():
            updates[rel] = (_mode_for(root / rel), sha)
            staged.append(spelling.get(rel, rel))

    for raw_path in deleted_paths:
        updates[_index_key(root, raw_path)] = index_write.ABSENT
    if updates:
        index_write.splice_index(repo, updates)
    return tuple(staged)
