"""One landing's read-once view of the halted-hold pass.

The pass serves one requirement: never land another run's hunks, never be withheld by a finished run.
Every leg of a landing asks it about the same in-window halted runs, so the answers that do not depend
on the asking leg are read once per `halted_hold_release.release_scope()` and shared: each record
dir's stamp, each run's extra declarations, the git index (one walk grown to the union of the paths
asked, re-walked only when `.git/index` changed), and which withheld paths are dirty. Outside a scope
every function computes directly and caches nothing.

Invariants: a cached answer never outlives its scope; an index answer is keyed on the index file's
`(mtime_ns, size)`, so a commit inside the scope re-reads it. Dirt is read once per landing: a file
edited by a peer mid-landing reads as it did at first sight, toward holding longer.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Collection, Optional


_scope_var = None


def _state() -> Optional[dict]:
    """The live `release_scope` state, or None outside one."""
    global _scope_var
    if _scope_var is None:
        from coordinator_core.ops.dispatch_emit import halted_hold_release

        _scope_var = halted_hold_release._SCOPE
    return _scope_var.get()


def _slot(name: str) -> Optional[dict]:
    """The scope's dict for `name`, or None outside a scope."""
    state = _state()
    return None if state is None else state.setdefault("view_" + name, {})


def shared(name: str, key, compute: Callable):
    """`compute()` once per scope per `(name, key)`; direct outside a scope. Exceptions are not cached."""
    slot = _slot(name)
    if slot is None:
        return compute()
    if key not in slot:
        slot[key] = compute()
    return slot[key]


def stamp(run_id: str) -> Optional[float]:
    """`stopped_runs._stamp_epoch(run_id)`, parsed once per scope."""
    from coordinator_core.ops.dispatch_emit import stopped_runs

    return shared("stamp", run_id, lambda: stopped_runs._stamp_epoch(run_id))


def _index_key(root: Path):
    from coordinator_core.git.git_dir import resolve_git_dir

    path = resolve_git_dir(root) / "index"
    try:
        st = os.stat(path)
    except OSError:
        return str(path), None
    return str(path), (st.st_mtime_ns, st.st_size)


#: `{(root, commit sha): {dir: {name: (mode, sha)}}}`: a commit's directory trees never change, so this
#: outlives a landing; bounded by `_SPINES_MAX` commits.
_SPINES: dict = {}
_SPINES_MAX = 4


def _spine_covers(spine: dict, path: str) -> bool:
    """True when `spine` already answers `path`: its parent directory is loaded, or an ancestor is
    loaded and proves the path absent."""
    if "" not in spine:
        return False
    cur = ""
    for part in path.split("/")[:-1]:
        entry = spine[cur].get(part)
        if entry is None or entry[0] != 0o40000:
            return True
        cur = f"{cur}/{part}" if cur else part
        if cur not in spine:
            return False
    return True


def tree_entries(root: Path, commit_sha: str, paths: Collection[str]) -> Optional[dict]:
    """`{path: (mode, sha)}` for the `paths` that are files in `commit_sha`'s tree. Each directory tree is read
    once per process, however many path sets ask. None when the commit or a tree is unreadable."""
    from coordinator_core.git import git_state

    wanted = list(dict.fromkeys(p for p in paths if p))
    if not wanted:
        return {}
    key = (str(root), commit_sha)
    spine = _SPINES.get(key)
    if spine is None:
        spine = _SPINES[key] = {}
        while len(_SPINES) > _SPINES_MAX:
            _SPINES.pop(next(iter(_SPINES)))
    missing = [p for p in wanted if not _spine_covers(spine, p)]
    if missing:
        commit = git_state.read_commit(root, commit_sha)
        loaded = git_state.read_tree_spine(root, missing, root_tree_sha=commit.tree) if commit is not None else None
        if loaded is None:
            return None
        spine.update(loaded)
    out: dict = {}
    for p in wanted:
        parent, _, leaf = p.rpartition("/")
        entry = (spine.get(parent) or {}).get(leaf)
        if entry is not None and entry[0] != 0o40000:
            out[p] = entry
    return out


def hint_index(root: Path, paths: Collection[str]) -> None:
    """Widen the first index walk of the scope to `paths`, so later narrower asks are served from it."""
    slot = _slot("index_hint")
    if slot is not None:
        slot.setdefault(str(root), set()).update(paths)


def index_identity(root: Path, wanted: Collection[str]) -> dict:
    """`git_index.parse_index_identity(root, wanted=wanted)` answered from the scope's index walk.

    The walk covers every path asked so far plus the `hint_index` union; a new path outside it, or a
    changed index file, walks again. An unmerged entry among the asked paths raises `IndexParseError`
    exactly as the direct call does."""
    from coordinator_core.git import git_index

    ask = set(wanted)
    slot = _slot("index")
    if slot is None or not ask:
        return git_index.parse_index_identity(root, wanted=ask)
    path, key = _index_key(Path(root))
    if key is None:
        return git_index.parse_index_identity(root, wanted=ask)
    held = slot.get(path)
    if held is None or held[0] != key or not ask <= held[1]:
        covered = set(ask)
        if held is not None and held[0] == key:
            covered |= held[1]
        covered |= _slot("index_hint").get(str(root), set())
        unmerged: set = set()
        entries = git_index.parse_index_identity(root, wanted=covered, unmerged_out=unmerged)
        held = slot[path] = (key, covered, entries, unmerged)
    bad = ask & held[3]
    if bad:
        raise git_index.IndexParseError(f"unmerged entries in {path}: {sorted(bad)[:3]}")
    return {p: held[2][p] for p in ask if p in held[2]}


def dirty_status(root: Path, wanted: Collection[str]) -> Optional[set]:
    """The `wanted` file paths `git status --porcelain` would list, decided in process from the index, HEAD's
    tree and the worktree: staged against HEAD, a worktree file differing from its index blob (or gone),
    or untracked and not ignored. None when any path cannot be decided here (a directory, a symlink, an
    unmerged entry, an ignore verdict git alone knows, an unreadable HEAD or index): the caller asks git.
    A skip-worktree path reads as gone, so dirty: toward holding."""
    from coordinator_core.git import git_state
    from coordinator_core.ops.ceremony import commit_companion
    from coordinator_core.session.claim_index import _normalize_key

    try:
        root = Path(root)
        paths = sorted({_normalize_key(p) for p in wanted if p})
        for p in paths:
            f = root / p
            if f.is_symlink() or f.is_dir():
                return None
        index = index_identity(root, paths)
        head_commit = git_state.head_sha(root)
        head = tree_entries(root, head_commit, paths) if head_commit else None
        if head is None:
            return None
        out: set = set()
        matcher = None
        for p in paths:
            f, entry, at_head = root / p, index.get(p), head.get(p)
            if entry is None:
                if at_head is not None:
                    out.add(p)
                elif f.is_file():
                    if matcher is None:
                        from coordinator_core.git.ignore_match import IgnoreMatcher

                        matcher = IgnoreMatcher(root)
                    verdict = matcher.ignored(p, False)
                    if verdict is None:
                        return None
                    if verdict is False:
                        out.add(p)
            elif at_head is None or at_head[1] != entry.sha or at_head[0] != entry.mode:
                out.add(p)
            elif not f.is_file() or commit_companion._differs_from_index(f, entry):
                out.add(p)
        return out
    except Exception:  # noqa: BLE001 -- undecided here: git decides
        return None


def dirty_withheld(root: Path, paths: Collection[str]) -> frozenset:
    """The `paths` dirty in the worktree against the index (`commit_companion.own_left_behind`'s test, under
    the active sibling-claim resolver), each path decided once per scope and index state."""
    from coordinator_core.ops.ceremony import commit_companion
    from coordinator_core.session.claim_index import _normalize_key

    want = frozenset(_normalize_key(p) for p in paths)
    slot = _slot("dirty")
    if slot is None:
        return frozenset(commit_companion.own_left_behind(root, {"-": sorted(want)}, "-", ()))
    path, key = _index_key(Path(root))
    bucket = slot.setdefault((path, key, id(commit_companion._sibling_claims.get())), {})
    missing = sorted(p for p in want if p not in bucket)
    if missing:
        found = set(commit_companion.own_left_behind(root, {"-": missing}, "-", ()))
        for p in missing:
            bucket[p] = p in found
    return frozenset(p for p in want if bucket.get(p))
