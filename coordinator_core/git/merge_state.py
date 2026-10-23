"""In-flight merge state, read and cleared without spawning `git`.

`read_merge_state` answers "is this repo mid-merge, on which parent, over which
paths"; `merge_refusal` answers "may this pathspec finish it"; `clear_merge_state`
removes the sequencer files once the ref has moved. Every cost is bounded by the
HEAD-vs-MERGE_HEAD changed subtrees plus one index walk, never the whole tree.
"""

from __future__ import annotations

from pathlib import Path
from typing import Collection, NamedTuple, Optional, Union

from coordinator_core.git import git_state
from coordinator_core.git.git_dir import resolve_git_dir
from coordinator_core.git.git_index import parse_index_identity
from coordinator_core.git.reconcile_manifest import has_marker

MERGE_OCTOPUS = "merge_octopus"
MERGE_MARKERS = "merge_markers"
MERGE_UNRESOLVED = "merge_unresolved"
MERGE_FOREIGN = "merge_foreign"
MERGE_INCOMPLETE = "merge_incomplete"

_MERGE_FILES = ("MERGE_HEAD", "MERGE_MSG", "MERGE_MODE", "AUTO_MERGE")


class MergeStateError(RuntimeError):
    """The merge's trees could not be read; no path set can be derived."""


class MergeState(NamedTuple):
    """`merge_head` keeps every sha line of MERGE_HEAD, newline-joined (an octopus
    has more than one; `merge_refusal` rejects it). `unmerged` are index paths at
    stage != 0. `merge_paths` are the changed paths the merge actually moved."""

    merge_head: str
    unmerged: tuple
    merge_paths: tuple


def read_merge_state(repo: Union[str, Path]) -> Optional[MergeState]:
    """None when `<gitdir>/MERGE_HEAD` is absent. Raises `MergeStateError` when
    HEAD or a merge tree is unreadable, never an empty `merge_paths`."""
    head_file = resolve_git_dir(repo) / "MERGE_HEAD"
    try:
        raw = head_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    shas = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not shas:
        raise MergeStateError(f"{head_file} names no commit")
    head = git_state.head_sha(repo)
    if head is None:
        raise MergeStateError("HEAD is unreadable")
    changes = git_state.diff_commit_trees(repo, head, shas[0])
    if changes is None:
        raise MergeStateError(f"trees of HEAD and {shas[0]} are unreadable")
    candidates = [c.path for c in changes]
    unmerged: set = set()
    index = parse_index_identity(repo, wanted=candidates, unmerged_out=unmerged)
    head_ids = git_state.head_blobs(repo, candidates)
    merge_paths = []
    for path in candidates:
        if path in unmerged:
            merge_paths.append(path)
            continue
        idx = index.get(path)
        base = head_ids.get(path)
        if idx is None:
            if base is not None:
                merge_paths.append(path)
        elif base is None or (idx.mode, idx.sha) != (base[0], base[1]):
            merge_paths.append(path)
    return MergeState("\n".join(shas), tuple(sorted(unmerged)), tuple(merge_paths))


def merge_refusal(
    state: MergeState, paths: Collection[str], root: Union[str, Path]
) -> Optional[str]:
    """None when `paths` may finish the merge, else a register string for the
    first failing check. Markers are scanned over `state.unmerged` only: git
    writes fresh markers just into conflicted paths. A merge-touched path whose
    resolution equals HEAD is not in `merge_paths`, so naming it is MERGE_FOREIGN."""
    path_set = set(paths)
    if len(state.merge_head.split()) > 1:
        return f"{MERGE_OCTOPUS}: MERGE_HEAD names more than one commit"
    root_path = Path(root)
    marked = []
    for p in state.unmerged:
        try:
            text = (root_path / p).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if has_marker(text):
            marked.append(p)
    if marked:
        return f"{MERGE_MARKERS}: conflict markers remain in {', '.join(marked)}"
    unresolved = [p for p in state.unmerged if p not in path_set]
    if unresolved:
        return f"{MERGE_UNRESOLVED}: unmerged paths not in the pathspec: {', '.join(unresolved)}"
    merge_set = set(state.merge_paths)
    foreign = sorted(path_set - merge_set)
    if foreign:
        return f"{MERGE_FOREIGN}: paths outside the merge: {', '.join(foreign)}"
    missing = [p for p in state.merge_paths if p not in path_set]
    if missing:
        return f"{MERGE_INCOMPLETE}: merge paths absent from the pathspec: {', '.join(missing)}"
    return None


def clear_merge_state(gitdir: Union[str, Path]) -> None:
    """Unlink the files git's own merge conclusion removes; missing ones are fine."""
    base = Path(gitdir)
    for name in _MERGE_FILES:
        (base / name).unlink(missing_ok=True)
