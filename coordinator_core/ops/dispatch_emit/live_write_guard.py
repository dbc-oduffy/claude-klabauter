"""Write-time refusal: a run may not replace a live-tree path with bytes that drop a peer's unlanded hunks.

`check_live_write(root, run_id, path, new_bytes)` returns a refusal string naming the holder, or None.
The holder lookup is `sibling_claims.sibling_run_claims`; ownership of each live hunk is
`run_hunk_claims.own_hunks`. Zero git spawns: HEAD is read through `git_state.head_blobs` and
`git_objects.read_object`, hunks come from `changed_opcodes` in process.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

UNATTRIBUTED = "unattributed"


def _lines(data: bytes) -> list[bytes]:
    return data.splitlines(keepends=True)


def _contains(haystack: list[bytes], needle: list[bytes]) -> bool:
    """Whether `needle` occurs as a contiguous run in `haystack`."""
    n = len(needle)
    if n == 0:
        return True
    first = needle[0]
    for i in range(len(haystack) - n + 1):
        if haystack[i] == first and haystack[i:i + n] == needle:
            return True
    return False


def _head_lines(root: Path, path: str) -> Optional[list[bytes]]:
    """HEAD content of `path` as lines; `[]` when absent from HEAD; None when unreadable."""
    from coordinator_core.git import git_objects, git_state
    from coordinator_core.git.git_dir import resolve_git_common_dir

    found = git_state.head_blobs(root, [path])
    if path not in found:
        return []
    obj = git_objects.read_object(resolve_git_common_dir(root), found[path][1])
    if obj is None or obj[0] != "blob":
        return None
    return _lines(obj[1])


def _hunk_dropped(head: list[bytes], live: list[bytes], new: list[bytes], op: tuple[str, int, int, int, int]) -> bool:
    _tag, i1, i2, j1, j2 = op
    added = live[j1:j2]
    removed = head[i1:i2]
    if added:
        return not _contains(new, added)
    return bool(removed) and _contains(new, removed)


def check_live_write(root, run_id: Optional[str], path: str, new_bytes: bytes) -> Optional[str]:
    """Refusal text naming the holder when writing `new_bytes` to live `path` would drop a hunk a
    peer's unlanded run holds (or an unattributed hunk: unknown is not own); None otherwise.

    A hunk is a difference of the live bytes from HEAD that `own_hunks` does not attribute to
    `run_id`. It is dropped when its added lines no longer occur in `new_bytes`, or, for a pure
    deletion, the deleted HEAD lines occur again. Never raises: an unreadable input yields None
    only when no peer claim is proven."""
    try:
        return _check(Path(root), run_id, path, new_bytes)
    except Exception:  # noqa: BLE001 -- unknown claim: the commit-side gates still apply
        return None


def _check(root: Path, run_id: Optional[str], path: str, new_bytes: bytes) -> Optional[str]:
    from coordinator_core.ops.dispatch_emit import run_hunk_claims, sibling_claims

    rel = sibling_claims._norm(path)
    holder = sibling_claims.sibling_run_claims(root, run_id, [rel]).get(rel)
    if not holder:
        return None
    try:
        live = _lines((root / rel).read_bytes())
    except FileNotFoundError:
        return None
    head = _head_lines(root, rel)
    if head is None:
        return None
    ops = run_hunk_claims.changed_opcodes(head, live)
    if not ops:
        return None
    hunks = [
        run_hunk_claims.Hunk(
            rel,
            i1 if i2 == i1 else i1 + 1, i2 - i1,
            j1 if j2 == j1 else j1 + 1, j2 - j1,
        )
        for _tag, i1, i2, j1, j2 in ops
    ]
    split = run_hunk_claims.own_hunks(root, run_id or "", rel, hunks, {holder: True})
    own = {id(h) for h in split.own}
    unattributed = {id(h) for h in split.unattributed}
    new = _lines(new_bytes)
    lost: list[str] = []
    for hunk, op in zip(hunks, ops):
        if id(hunk) in own or not _hunk_dropped(head, live, new, op):
            continue
        lost.append(UNATTRIBUTED if id(hunk) in unattributed else holder)
    if not lost:
        return None
    named = holder if holder in lost else UNATTRIBUTED
    return (
        f"write to {rel} drops {len(lost)} unlanded hunk(s) held by {named}. "
        "Build the fix on a HEAD export and land it by commit."
    )
