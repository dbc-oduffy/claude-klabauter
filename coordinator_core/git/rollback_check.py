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
finding has depth of at least 2, or three or more paths each have a finding at any
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

    - TWO SPAWNS AT MOST PER 8000 PATH CHARS (`rev-parse`, then one
      `git log --first-parent --raw`): measured on 21 paths over 1000
      commits (example-retrieval-repo): the git spawns 15.6-31ms process time, 2 spawns,
      0 object reads; the in-process walk with targeted single-entry tree
      lookups (`_tree_lookup`) 1156-1172ms process time, 3717 object reads,
      down from 1.6s with full tree parses. The spawn path wins on both axes
      and is kept. The in-process
      walk (`_values_along`) is the fallback when git fails, the history is shorter than
      `window`, or the spawn times out.

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
fixture -- the same mechanism this module ships. Added spawns: 0 for the
object-store walk, now 2 for the git path (see TWO SPAWNS above). Check cost at 150
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

import subprocess
from pathlib import Path
from typing import Dict, List, Mapping, NamedTuple, Optional, Tuple, Union

from coordinator_core.git.commit_walk import commit_meta
from coordinator_core.git.git_objects import read_object
from coordinator_core.git.git_state import _parse_tree_entries
from coordinator_core.win_portability import no_console_creationflags

_GIT_TIMEOUT_S = 5
#: Pathspec characters per `git log` argv; Windows caps a command line near 32k.
_ARGV_CHAR_BUDGET = 8000

ABSENT = object()

_TREE_MODE = 0o40000
_NUL = bytes(1)

#: Tree-sha-keyed, content-addressed, bounded the same way
#: `commit_walk._COMMIT_CACHE` and `git_objects._OBJECT_CACHE` are -- see
#: those modules' own comments for why bounding a content-addressed cache is
#: still worth doing on a warm long-running engine.
_TREE_CACHE_MAX_ENTRIES = 4096
_TREE_CACHE: "Dict[Tuple[str, str], Optional[Dict[str, Tuple[int, str]]]]" = {}


def moved_deletions(
    assembled: "Dict[str, object]",
    head_spine: "Dict[str, Dict[str, Tuple[int, str]]]",
    absent: object,
) -> "set":
    """Deleted paths in one commit that are moves, not rollbacks.

    ``assembled`` maps path -> ``(mode, blob_sha)`` or the caller's ``absent``
    sentinel; ``head_spine`` maps dir -> {name: (mode, sha)} for HEAD. A
    deletion is a move when its HEAD blob lands at another path in the same
    commit, or when a path with the same file name is NEW in this commit
    (rename-with-edit: queue-row archival moves the row and stamps it closed).
    Both commit routes must use this one rule; two copies drifted once.
    """
    added_blobs = {v[1] for v in assembled.values() if v is not absent}
    new_names = set()
    for path, val in assembled.items():
        if val is absent:
            continue
        d, _, name = path.rpartition("/")
        if head_spine.get(d, {}).get(name) is None:
            new_names.add(name)
    moved = set()
    for path, val in assembled.items():
        if val is not absent:
            continue
        d, _, name = path.rpartition("/")
        entry = head_spine.get(d, {}).get(name)
        if (entry is not None and entry[1] in added_blobs) or name in new_names:
            moved.add(path)
    return moved


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


def _tree_lookup(
    common_dir: Path, tree_sha: str, names: "set"
) -> Optional[Dict[str, Tuple[int, str]]]:
    """Only the `names` entries of one tree, scanned from the raw bytes; None if unreadable."""
    obj = read_object(common_dir, tree_sha)
    if obj is None or obj[0] != "tree":
        return None
    raw = obj[1]
    wanted = {n.encode("utf-8", "surrogateescape"): n for n in names}
    found: Dict[str, Tuple[int, str]] = {}
    pos, end = 0, len(raw)
    while pos < end and len(found) < len(wanted):
        sp = raw.find(b" ", pos)
        nul = raw.find(_NUL, sp)
        if sp < 0 or nul < 0:
            break
        name = wanted.get(raw[sp + 1:nul])
        if name is not None:
            found[name] = (int(raw[pos:sp], 8), raw[nul + 1:nul + 21].hex())
        pos = nul + 21
    return found


def _blob_at_commit(common_dir: Path, commit_sha: str, path: str):
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


class _Node:
    """One directory prefix of the candidate paths; `prev` is the subtree sha last seen."""

    __slots__ = ("files", "dirs", "prev")

    def __init__(self) -> None:
        self.files: Dict[str, str] = {}
        self.dirs: Dict[str, "_Node"] = {}
        self.prev: object = None


def _paths_below(node: _Node) -> List[str]:
    out = list(node.files.values())
    for child in node.dirs.values():
        out.extend(_paths_below(child))
    return out


def _forget_below(node: _Node) -> None:
    node.prev = None
    for child in node.dirs.values():
        _forget_below(child)


def _resolve_node(
    common_dir: Path, node: _Node, tree_sha: Optional[str], cur: Dict[str, object]
) -> None:
    """Refresh `cur` for every path under `node`, skipping a subtree whose sha is unchanged.

    `tree_sha` None means the directory is absent at this ancestor. An
    unreadable tree sets None and leaves `prev` unset so it is never skipped.
    """
    marker: object = tree_sha if tree_sha is not None else _NO_VERSION
    if node.prev is not None and node.prev == marker:
        return
    if tree_sha is None:
        _forget_below(node)
        node.prev = marker
        for p in _paths_below(node):
            cur[p] = ABSENT
        return
    entries = _tree_lookup(common_dir, tree_sha, set(node.files) | set(node.dirs))
    if entries is None:
        _forget_below(node)
        for p in _paths_below(node):
            cur[p] = None
        return
    node.prev = marker
    for name, path in node.files.items():
        entry = entries.get(name)
        cur[path] = ABSENT if entry is None else entry[1]
    for name, child in node.dirs.items():
        entry = entries.get(name)
        child_sha = entry[1] if entry is not None and entry[0] == _TREE_MODE else None
        _resolve_node(common_dir, child, child_sha, cur)


def _values_along(
    common_dir: Path, ancestors: List[Tuple[int, str]], paths: List[str]
) -> "Dict[str, List[Tuple[str, object]]]":
    """Each path's `_blob_at_commit` value at every ancestor, in one pass.

    A directory whose tree sha equals the previous ancestor's is not re-read;
    every path below it keeps its previous value.
    """
    root = _Node()
    for path in paths:
        parts = path.split("/")
        node = root
        for part in parts[:-1]:
            node = node.dirs.setdefault(part, _Node())
        node.files[parts[-1]] = path
    out: "Dict[str, List[Tuple[str, object]]]" = {p: [] for p in paths}
    cur: Dict[str, object] = {}
    for _step, sha in ancestors:
        meta = commit_meta(common_dir, sha)
        tree = meta.get("tree") if meta is not None else None
        if not tree:
            for p in paths:
                cur[p] = None
            _forget_below(root)
        else:
            _resolve_node(common_dir, root, tree, cur)
        for p in paths:
            out[p].append((sha, cur[p]))
    return out


def _git(common_dir: Path, args: List[str]) -> Optional[str]:
    try:
        r = subprocess.run(
            ["git", "--git-dir", str(common_dir), "--literal-pathspecs", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            timeout=_GIT_TIMEOUT_S,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def _git_series(
    common_dir: Path, head_sha: str, paths: List[str], window: int
) -> "Optional[Dict[str, List[Tuple[str, object]]]]":
    """Each path's (commit, value) versions inside the window, newest first.

    One `git log --first-parent --raw` yields every in-window change with the
    old and new blob oid. The value at `head_sha` is the newest change's new
    oid; each older value sits at that change's first parent. A parent equal
    to the window boundary (`head~window`) is outside the window and ends the
    series. A path with no change in the window has an empty series (one
    version, so never a finding). None means git could not answer.
    """
    boundary = _git(
        common_dir, ["rev-parse", "--verify", "--quiet", f"{head_sha}~{window}^{{commit}}"]
    )
    if boundary is None or not boundary.strip():
        return None
    boundary = boundary.strip()
    wanted = set(paths)
    changes: "Dict[str, List[Tuple[str, Optional[str], str, str]]]" = {p: [] for p in paths}
    batch: List[str] = []
    size = 0
    for path in paths + [None]:  # type: ignore[list-item]
        if path is not None and (size + len(path) < _ARGV_CHAR_BUDGET or not batch):
            batch.append(path)
            size += len(path) + 1
            continue
        out = _git(
            common_dir,
            [
                "log", "--first-parent", "--diff-merges=first-parent", "--no-renames",
                "--format=%H %P", "--raw", "--no-abbrev", "-z",
                f"{boundary}..{head_sha}", "--", *batch,
            ],
        )
        if out is None:
            return None
        commit, parent = "", None
        tokens = out.split("\0")
        i = 0
        while i < len(tokens):
            tok = tokens[i].lstrip("\n")
            i += 1
            if tok.startswith(":"):
                fields = tok[1:].split(" ")
                if i < len(tokens) and len(fields) >= 5 and tokens[i] in wanted:
                    changes[tokens[i]].append((commit, parent, fields[2], fields[3]))
                i += 1
            elif tok:
                parts = tok.split(" ")
                commit = parts[0]
                parent = parts[1] if len(parts) > 1 else None
        batch, size = ([path], len(path) + 1) if path is not None else ([], 0)
    series: "Dict[str, List[Tuple[str, object]]]" = {}
    for p, rows in changes.items():
        entries: "List[Tuple[str, object]]" = []
        if rows:
            entries.append((head_sha, ABSENT if not rows[0][3].strip("0") else rows[0][3]))
            for _commit, parent, old, _new in rows:
                if parent is None or parent == boundary:
                    break
                entries.append((parent, ABSENT if not old.strip("0") else old))
        series[p] = entries
    return series


def find_exact_blob_rollbacks(
    common_dir: Union[str, Path],
    head_sha: str,
    candidates: Mapping[str, Union[str, object]],
    window: int = 1000,
) -> List[RollbackFinding]:
    common_dir = Path(common_dir)
    if not candidates:
        return []
    findings: List[RollbackFinding] = []
    along = _git_series(common_dir, head_sha, list(candidates), window)
    if along is None:
        ancestors = _first_parent_ancestors(common_dir, head_sha, window)
        along = _values_along(common_dir, ancestors, list(candidates))
    for path, new_value in candidates.items():
        # Depth counts VERSIONS of the path, not commits: a commit that did
        # not touch `path` adds no version. V(1) is head's own value -- equal
        # to it is no change, never a finding (counting it made every
        # unchanged claimed path a depth-1 hit, and three tripped breadth-3).
        version = 0
        previous: object = _NO_VERSION
        for sha, old_value in along[path]:
            if old_value is None or old_value == previous:
                continue
            version += 1
            previous = old_value
            if old_value == new_value:
                if version >= 2:
                    findings.append(RollbackFinding(path, version - 1, sha))
                break
    return findings


_NO_VERSION = object()


def refusal(findings: List[RollbackFinding]) -> bool:
    if any(f.depth >= 2 for f in findings):
        return True
    paths = {f.path for f in findings}
    return len(paths) >= 3


def refusal_detail(
    findings: List[RollbackFinding], candidates: Mapping[str, Union[str, object]]
) -> str:
    """The refusal text both commit routes print, ending in a pasteable override.

    A deletion of a path added inside the window is a rollback by K-016 clause
    2 (ruling state/rulings/2026-10-08-apm-rollback-gate-deletions.md), so an
    intended `git rm` lands here; naming it a deletion keeps it a one-retry cost.
    """
    deleted = [f.path for f in findings if candidates.get(f.path) is ABSENT]
    restored = [f for f in findings if candidates.get(f.path) is not ABSENT]
    parts = [f"{f.path} (depth {f.depth}, restores {f.restores_commit})" for f in restored]
    if deleted:
        parts.append(
            "deletion of file(s) added within the window: " + ", ".join(deleted)
        )
    flags = " ".join(f"--declared-revert {f.path}" for f in findings)
    return f"staged rollback detected -- {'; '.join(parts)}. If intended, re-run with: {flags}"
