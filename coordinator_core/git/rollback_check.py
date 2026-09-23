"""coordinator_core.git.rollback_check -- in-process, zero-spawn detection
of an exact-blob rollback at an agent commit route, per Part 2's rule in
`docs/plans/2026-09-11-close-the-three-silent-failure-gaps.md`.

Purpose: the old spawned-`git log` gate that used to catch a peer's clobber
silently died and nobody noticed (the plan's Part 2 problem statement); this
is its in-process, zero-spawn replacement, priced and calibrated by the
`P2a` spike (`docs/research/spike-verdicts/
2026-09-11-exact-blob-rollback-check-in-process.md`, verdict: viable, at
`W=1000` -- the spike's own re-derivation found one of the two
independently-verified historical rollback depths at 668, past the
originally-shipped `W=500`, so the default below is calibrated to that
corrected finding, not the pre-spike guess). This module is the check
ALONE -- it does not decide whether to
refuse a commit; `P2d`/`P2e` call it from the two agent commit routes
(`commit.commit_paths`, `git_native.commit_scoped`) and raise there.

For each candidate path a caller is about to commit, walks HEAD's
first-parent line up to `window` steps and asks: does the path's NEW value
(a blob sha, or `ABSENT` for a deletion) exact-match a version the path
already held at some ancestor depth on that line? Per the plan, depth
counts the path's own VERSIONS (a commit that did not touch it adds none):
V(1) is `head_sha`'s value, and `N == V(d+1)` is a rollback at depth `d`,
discarding the `d` most recent changes. `N == V(1)` is no change and never a
finding; `depth=1` undoes only the latest change. `refusal()` applies K-016's rule: refuse if any single
finding has `depth >= 2`, or three or more paths each have a finding at any
depth ("breadth-3").

Negative spec:

    - FIRST-PARENT ONLY. Ancestors come from `commit_walk.commit_meta`,
      following `parents[0]` from `head_sha`, never `commit_walk.walk` or
      `walk_since` (both walk every parent, date-ordered -- the wrong shape
      here, and the spike's own point of comparison). A commit reached only
      through a merge's SECOND parent is never an ancestor this module
      considers a "version" -- content that lived only on a side branch is
      invisible to it by construction, not filtered after the fact. The
      merge commit's OWN version of a path (the blob its tree records) is
      one version on the first-parent line like any other.

    - NO SPAWNS. Every read here is `commit_walk.commit_meta` (object-store
      commit parse) or `git_state._parse_tree_entries` over
      `git_objects.read_object` (object-store tree parse) -- no
      `subprocess`, direct or indirect, is reachable from any function in
      this module.

    - NO INDEX OR WORKTREE READS. `candidates` is handed to this module
      fully resolved (a blob sha the caller already computed, or `ABSENT`
      for a deletion) -- it never opens `.git/index` and never stats a
      worktree file. That is the caller's job (per-path blob resolution),
      done once, before this module is reached.

    - `window` (`W`) IS FIXED, and does not grow with history size. The
      walk stops at `window` first-parent steps regardless of how deep the
      branch actually goes -- a repo with fewer than `window` first-parent
      commits simply exhausts its ancestors early (`commit_meta` returning
      `None` past the root ends the walk), never an error.

    - IT DOES NOT SEE A HUMAN `git commit`. This module is reachable only
      from an agent commit route that resolves and hands it `candidates`
      before writing a commit object (`P2d`, `P2e`) -- a human committing
      through the ordinary `git commit` CLI never calls it, by construction
      of where its callers are wired in, not by any check inside it.

Cost: measured by the `P2a` spike against this repo's live object store
(cold cache, no warm reuse credited) and a 150-file/3-revision hermetic
fixture -- the same mechanism this module ships. Added spawns: 0 (no
`subprocess` import reachable from any function below). Check cost at 150
paths, `commit_paths` seam (fixture, hermetic): baseline 33.774ms, with
check 35.647ms -- added ~1.87ms at `W=500`, ~+0.207ms more at `W=1000`
(~2.08ms total), against 466.2ms of remaining headroom at the 500ms
brightline (`docs/decisions/DR-344-the-brightline-process-budget-for-claude-klabauter.md`)
-- under 0.5% overhead. Check-alone, read-only, against this repo's own
live object store at 150 paths: 0.604ms at `W=500`, 0.811ms at `W=1000`.
`W=1000` is calibrated to the two independently-verified historical
rollback depths the spike re-derived (363 and 668 first-parent commits
back) -- not tuned down to fit budget: the spike's own Result 1 shows cost
is flat across `W`, dominated by the per-call commit-walk cost paid once
and shared across every candidate path via `commit_walk._COMMIT_CACHE`,
not by `W` itself. (A third calibration commit named in the plan,
`70ed82e4aa6f`, does not resolve in this clone's object store -- the
spike flagged it as unverified, not silently dropped; see the spike's "What
this does NOT resolve" section.)
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Mapping, NamedTuple, Optional, Tuple, Union

from coordinator_core.git.commit_walk import commit_meta
from coordinator_core.git.git_objects import read_object
from coordinator_core.git.git_state import _parse_tree_entries

#: Sentinel marking a candidate as a deletion -- the path is not being
#: committed with any content. Distinct object identity, never a string, so
#: it can never collide with a real blob sha. Matches `tree_spine._ABSENT`'s
#: role but is its own sentinel: this module takes `candidates` from
#: whichever caller resolved them and must not require that caller to import
#: `tree_spine` just to spell "deleted".
ABSENT = object()

_TREE_MODE = 0o40000

#: Tree-sha-keyed, content-addressed, bounded the same way
#: `commit_walk._COMMIT_CACHE` and `git_objects._OBJECT_CACHE` are -- see
#: those modules' own comments for why bounding a content-addressed cache is
#: still worth doing on a warm long-running engine.
_TREE_CACHE_MAX_ENTRIES = 4096
_TREE_CACHE: "Dict[Tuple[str, str], Optional[Dict[str, Tuple[int, str]]]]" = {}


class RollbackFinding(NamedTuple):
    path: str
    depth: int
    restores_commit: str


def _tree_entries(
    common_dir: Path, tree_sha: str
) -> Optional[Dict[str, Tuple[int, str]]]:
    key = (str(common_dir), tree_sha)
    if key in _TREE_CACHE:
        return _TREE_CACHE[key]
    obj = read_object(common_dir, tree_sha)
    entries: Optional[Dict[str, Tuple[int, str]]] = None
    if obj is not None and obj[0] == "tree":
        entries = _parse_tree_entries(obj[1])
    if len(_TREE_CACHE) >= _TREE_CACHE_MAX_ENTRIES:
        _TREE_CACHE.clear()
    _TREE_CACHE[key] = entries
    return entries


def _blob_at_commit(common_dir: Path, commit_sha: str, path: str):
    """The path's blob sha inside `commit_sha`'s tree, or `ABSENT` if the
    path does not exist there, or `None` if `commit_sha` or a tree along the
    spine is unreadable (take the ladder -- never a false "absent")."""
    meta = commit_meta(common_dir, commit_sha)
    if meta is None:
        return None
    cur = meta.get("tree")
    if not cur:
        return None
    parts = path.split("/")
    for part in parts[:-1]:
        entries = _tree_entries(common_dir, cur)
        if entries is None:
            return None
        entry = entries.get(part)
        if entry is None:
            return ABSENT
        if entry[0] != _TREE_MODE:
            # A path component that should be a directory is a leaf here
            # (e.g. a gitlink or file shadowing what the candidate path
            # expects to be a directory) -- the path cannot exist as named.
            return ABSENT
        cur = entry[1]
    entries = _tree_entries(common_dir, cur)
    if entries is None:
        return None
    entry = entries.get(parts[-1])
    if entry is None:
        return ABSENT
    return entry[1]


def _first_parent_ancestors(
    common_dir: Path, head_sha: str, window: int
) -> List[Tuple[int, str]]:
    """`[(depth, sha), ...]`, `depth` 1-based, `head_sha` itself at depth 1,
    following `parents[0]` up to `window` entries. Stops early (never
    raises) once a commit is unreadable or the root is reached."""
    out: List[Tuple[int, str]] = []
    sha: Optional[str] = head_sha
    depth = 0
    while sha is not None and depth < window:
        meta = commit_meta(common_dir, sha)
        if meta is None:
            break
        depth += 1
        out.append((depth, sha))
        parents = meta.get("parents") or []
        sha = parents[0] if parents else None
    return out


def find_exact_blob_rollbacks(
    common_dir: Union[str, Path],
    head_sha: str,
    candidates: Mapping[str, Union[str, object]],
    window: int = 1000,
) -> List[RollbackFinding]:
    """For each `candidates` path -> new value (`blob sha` or `ABSENT`),
    the version depth `d >= 1` (module docstring) at which `head_sha`'s
    first-parent line last held that exact value, as a
    `RollbackFinding(path, depth, restores_commit)`. A path with no match
    within `window` first-parent steps, or equal to head's own value,
    contributes no finding. Pure read;
    see the module docstring's negative spec for what it never does."""
    common_dir = Path(common_dir)
    if not candidates:
        return []
    ancestors = _first_parent_ancestors(common_dir, head_sha, window)
    findings: List[RollbackFinding] = []
    for path, new_value in candidates.items():
        # Depth counts VERSIONS of the path, not commits: a commit that did
        # not touch `path` adds no version. V(1) is head's own value -- equal
        # to it is no change, never a finding (counting it made every
        # unchanged claimed path a depth-1 hit, and three tripped breadth-3).
        version = 0
        previous: object = _NO_VERSION
        for _step, sha in ancestors:
            old_value = _blob_at_commit(common_dir, sha, path)
            if old_value is None or old_value == previous:
                continue
            version += 1
            previous = old_value
            if old_value == new_value:
                if version >= 2:
                    findings.append(RollbackFinding(path, version - 1, sha))
                break
    return findings


#: `find_exact_blob_rollbacks`' "no version seen yet" marker -- distinct from
#: `ABSENT`, which is itself a version.
_NO_VERSION = object()


def refusal(findings: List[RollbackFinding]) -> bool:
    """K-016's rule, as a measurement: refuse if any single finding has
    `depth >= 2` (an OLDER version came back on one path alone), or three or
    more distinct paths each have a finding at any depth ("breadth-3")."""
    if any(f.depth >= 2 for f in findings):
        return True
    paths = {f.path for f in findings}
    return len(paths) >= 3
