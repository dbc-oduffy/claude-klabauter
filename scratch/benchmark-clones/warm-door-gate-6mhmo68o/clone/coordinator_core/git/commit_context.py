"""coordinator_core.git.commit_context -- the ONE pass context a commit of
k named paths resolves at pass entry, replacing the per-consumer
re-derivation over the whole repo that
`docs/plans/2026-08-27-the-commit-op-resolves-one-pass-context.md` exists to
retire.

**Proven, not proposed.** The shape here is exactly
`docs/research/spike-verdicts/2026-08-27-the-commit-op-resolves-one-pass-
context.md`'s `build_context`, measured against this repo's own
33,503-entry index: 10.16ms for a 3-path context against 267.19ms for the
six full index walks it replaces, sub-linear in k (2.34ms at k=1, 15.62ms
at k=50 -- still under one full `read_index`). Do not re-derive the
measurement or the mechanism; both are settled.

**NO CONSUMERS YET.** This module is landed alone, with an empty call-site
set, so its contract is fixed before the fifteen `R1`-`R15` consumers
(`docs/plans/2026-08-27-the-commit-op-resolves-one-pass-context.md`, C6)
depend on it. Threading it through those consumers is explicitly a later
chunk's job, not this one's.

## The six facts

```
per pass:  HEAD sha
per path:  index entry   (mode, sha)
           index stat    (size, mtime, mtime_nsec)
           HEAD blob     (mode, sha)
           worktree presence
           worktree stat
```

`index entry` is `(mode, sha)`, not `(mode, sha, stage)` -- deliberately
narrower than the spike prose's six-fact list reads at a glance. The scoped
reader this module is built on,
`coordinator_core.git.git_index.parse_index_identity`, does not surface
`stage` at all (its whole existing consumer set --
`scoped_status`/`diff_index_name_status` -- has never needed it, and its own
module docstring documents no stage handling); adding stage extraction
would mean editing that module, which sits outside this chunk's declared
`writes:` scope. The spike's own reference implementation reads the same
way: `ctx["paths"][p]["index"] = (e.mode, e.sha) if e else None`, never a
3-tuple. A caller that needs unmerged-conflict detection keeps using
`coordinator_core.git.git_state.read_index`, which raises `IndexParseError`
on any `stage != 0` entry, exactly as it does today.

## One scoped index walk, one HEAD read, one scoped HEAD-tree read, k stats

`build_commit_context` calls, once each, per invocation:

- `git_state.head_sha` -- the pass-level fact, no `git` spawn (file reads
  only).
- `git_index.parse_index_identity(root, wanted=paths)` -- the ONE scoped
  index walk. `wanted=paths` is load-bearing: passing `wanted=None` here
  would materialise every entry in the index, which is exactly the
  corpus-scale defect this whole plan exists to retire (see AC9). This
  function early-exits once every wanted path is found (see its own
  docstring for the sort-order caveat on how much that saves).
- `git_state.head_blobs(root, paths)` -- the scoped HEAD-tree read, already
  spawn-free via `read_tree_spine` in the ordinary case (falls back to one
  scoped `git ls-tree` only when the tree-spine read is itself unreadable;
  see that function's own docstring).
- one `Path.stat()` per path -- the k worktree stats.

**NOTHING materialises an entry outside `paths`.** `resolved` below is
built by iterating `paths` itself, never any of the three source
structures directly, so this holds even if a source reader is ever swapped
for one that (incorrectly) returns extra entries. This is the whole design,
and `test_commit_context.py` asserts it at module scope (AC9's shape),
against a five-figure synthesised index.

## Two generations, not one

A commit pass calls `build_commit_context` TWICE: generation A before
`coordinator_core.ops.ceremony.git_native.add_paths` runs, generation B
after it. `add_paths` is the ONLY `.git/index` mutation inside a commit
pass -- verified by claude-klabauter-75 and carried here rather than
re-derived:

- the archive sweep's `apply_sweep` moves worktree-only files via
  `os.replace` (no index write);
- `_commit_scoped_private_index`/`stage_from_patch` redirect
  `GIT_INDEX_FILE` to a temp path that `read_index`/`parse_index_identity`
  (both scoped to the REAL `.git/index` via `resolve_git_dir`) never
  resolve to;
- `_commit_via_head_spine` writes the commit object and moves the ref, and
  touches no index at all.

This module supplies the SHAPE both generations use; deciding when to build
each generation and threading the result to a consumer is a later chunk's
job (C6), not this one's -- there are no consumers of either generation
yet.

## What this module never does

- **Never serves a `fresh=True` observation from a context.**
  `_agree_branch_cas_refusal` and `_commit_via_head_spine`'s CAS
  deliberately re-observe `.git/index` with `read_index(repo, fresh=True)`
  to catch a peer writing inside the commit window, on a tree ~50 sessions
  share. This module builds a plain snapshot struct with no cache of its
  own and no relationship to `git_state.index_read_cache_scope` at all --
  it neither opens nor reads one. A caller performing a CAS keeps calling
  `read_index(fresh=True)` directly; folding that re-observation into a
  context (cached or not) would make the CAS unable to fail. See the spike
  verdict's "The one fact a context must not hold" section.
- **Never hashes worktree bytes.** `worktree_stat` is `os.stat()`'s own
  result (or `None`); settling a stat-mismatch candidate against normalized
  content is `coordinator_core.git.content_hash`'s job, not this module's --
  same boundary `git_state.py`'s own "THE WORKTREE HASH DOES NOT WORK"
  section draws.
- **No process-lifetime cache, no memoisation keyed on `root`.** Every call
  to `build_commit_context` re-derives its three source reads fresh (module
  negative-spec on `git_state`/`git_index` already forbids caching those);
  this module adds no cache of its own on top.

Spec backlink:
docs/plans/2026-08-27-the-commit-op-resolves-one-pass-context.md (C4)
Spec backlink:
docs/research/spike-verdicts/2026-08-27-the-commit-op-resolves-one-pass-context.md
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, NamedTuple, Optional, Sequence, Tuple, Union

from coordinator_core.git.git_index import parse_index_identity
from coordinator_core.git.git_state import head_blobs, head_sha

__all__ = [
    "PathContext",
    "CommitContext",
    "build_commit_context",
]


class PathContext(NamedTuple):
    """One path's slice of a `CommitContext` -- five of the six facts (the
    sixth, HEAD sha, is pass-level and lives on `CommitContext.head`).

    `index` / `index_stat` / `head` are `None` when the path is absent from
    that side (untracked, or not present at HEAD); `on_disk` and
    `worktree_stat` are independent of both -- an untracked path can still
    exist on disk, and a staged path can be deleted from the worktree.
    """

    index: Optional[Tuple[int, str]]
    """`(mode, sha)` from the index, or `None` if `path` is not staged."""

    index_stat: Optional[Tuple[int, int, int]]
    """`(size, mtime, mtime_nsec)` from the SAME index entry as `index` --
    `None` exactly when `index` is `None`."""

    head: Optional[Tuple[int, str]]
    """`(mode, sha)` from HEAD's tree, or `None` if `path` is not present
    at HEAD."""

    on_disk: bool
    """Whether `path` currently exists in the worktree at all."""

    worktree_stat: Optional[os.stat_result]
    """`Path.stat()`'s own result, or `None` when `on_disk` is `False`."""


class CommitContext(NamedTuple):
    """The whole pass context for one `build_commit_context` call: the
    pass-level HEAD sha plus one `PathContext` per requested path.

    `paths` holds an entry for EVERY path in the `paths` argument
    `build_commit_context` was called with, and NEVER an entry for any
    other path -- see the module docstring's "NOTHING materialises an
    entry outside `paths`" section.
    """

    head: Optional[str]
    paths: Dict[str, PathContext]


def build_commit_context(
    root: Union[str, Path], paths: Sequence[str]
) -> CommitContext:
    """Resolve one `CommitContext` for `paths` (repo-relative), scoped to
    exactly those paths -- see the module docstring for the mechanism, the
    six facts, and what this deliberately never does.

    `paths` is consumed once into a `list` at the top, so a caller passing a
    generator or another single-use iterable does not silently starve the
    second and third reads below.
    """
    root_path = Path(root)
    wanted = list(paths)

    head = head_sha(root)
    index_entries = parse_index_identity(root, wanted=wanted)
    head_entries = head_blobs(root, wanted)

    resolved: Dict[str, PathContext] = {}
    for path in wanted:
        entry = index_entries.get(path)
        try:
            stat_result: Optional[os.stat_result] = (root_path / path).stat()
        except OSError:
            stat_result = None

        resolved[path] = PathContext(
            index=(entry.mode, entry.sha) if entry is not None else None,
            index_stat=(
                (entry.size, entry.mtime, entry.mtime_nsec)
                if entry is not None
                else None
            ),
            head=head_entries.get(path),
            on_disk=stat_result is not None,
            worktree_stat=stat_result,
        )

    return CommitContext(head=head, paths=resolved)
