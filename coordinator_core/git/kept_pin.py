"""Pin kept (withheld-work) blobs under refs/preserve/kept/<run_id>, in-process.

One ref per run points at a commit wrapping a flat tree whose entry names are
the kept blob shas, so `git gc` honours every blob. No subprocess.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

from coordinator_core.git import git_objects

KEPT_REF_PREFIX = "refs/preserve/kept/"

_IDENTITY = "coordinator-kept-pin <coordinator-kept-pin@localhost>"
_BLOB_MODE = 0o100644
_RETRIES = 2


def _pinned_blobs(common_dir: Path, commit_sha: Optional[str]) -> set[str]:
    """Blob shas in the pin commit's tree; empty when the pin or tree is unreadable."""
    if not commit_sha:
        return set()
    commit = git_objects.read_object(common_dir, commit_sha)
    if commit is None or commit[0] != "commit":
        return set()
    tree_sha = None
    for line in commit[1].split(b"\n"):
        if line.startswith(b"tree "):
            tree_sha = line[5:].decode("ascii").strip()
            break
        if not line:
            break
    if tree_sha is None:
        return set()
    tree = git_objects.read_object(common_dir, tree_sha)
    if tree is None or tree[0] != "tree":
        return set()
    data, pos, found = tree[1], 0, set()
    while pos < len(data):
        nul = data.index(b"\x00", pos)
        found.add(data[nul + 1 : nul + 21].hex())
        pos = nul + 21
    return found


def pin_kept(common_dir: Path, run_id: str, blobs: Iterable[str]) -> bool:
    """Pin `blobs` (plus any already pinned) under KEPT_REF_PREFIX + run_id.

    Returns False when the pin could not be written; True when every blob is
    referenced by the ref (including the no-op empty case).
    """
    wanted = {b.lower() for b in blobs}
    if not wanted:
        return True
    ref = KEPT_REF_PREFIX + run_id
    for _ in range(_RETRIES):
        prior = git_objects.read_ref_loose_or_packed(common_dir, ref)
        union = wanted | _pinned_blobs(common_dir, prior)
        tree = git_objects.build_tree(
            common_dir, {sha: (_BLOB_MODE, sha) for sha in sorted(union)}
        )
        lines = [f"tree {tree}"]
        if prior:
            lines.append(f"parent {prior}")
        lines += [f"author {_IDENTITY} 0 +0000", f"committer {_IDENTITY} 0 +0000"]
        body = "\n".join(lines) + f"\n\nkept blobs for {run_id}\n"
        new = git_objects.write_object(common_dir, b"commit", body.encode("utf-8"))
        if git_objects.cas_ref(common_dir, ref, prior, new):
            return True
    return False


def unreachable_kept(common_dir: Path, run_id: str, blobs: Iterable[str]) -> list[str]:
    """Sorted `blobs` absent from the object store.

    A blob that resolves but is not yet in the run's pin (recorded before
    pinning existed) is not unreachable; it is pinned here, best effort.
    """
    wanted = {b.lower() for b in blobs}
    missing = sorted(b for b in wanted if git_objects.read_object(common_dir, b) is None)
    present = wanted - set(missing)
    if present:
        prior = git_objects.read_ref_loose_or_packed(common_dir, KEPT_REF_PREFIX + run_id)
        if not present <= _pinned_blobs(common_dir, prior):
            pin_kept(common_dir, run_id, present)
    return missing
