"""3-way sync of landed paths into the shared worktree after a private-index or commit-tree landing.

ours = worktree bytes (check-in form), base = the path's blob at the landing's parent, theirs = the
landed blob. Pure Python, zero git spawns; blobs are read in process.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Mapping, NamedTuple, Optional, Tuple, Union

from coordinator_core.git import checkin_attrs
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.git_objects import read_object
from coordinator_core.git.own_delta_merge import apply_own_delta

#: A tree entry as `(mode, blob sha)`; None = absent at that side.
Entry = Optional[Tuple[int, str]]

_EXEC_MODE = 0o100755
_SPECIAL_MODES = (0o120000, 0o160000)


class SyncOutcome(NamedTuple):
    """Per-path disposition of one sync. `head_equal`: the worktree now holds the landed blob (written
    or already so). `rebased`: it holds the landed hunks plus the worktree's own dirt. `refused`: the
    path is byte-intact and could not be reconciled."""

    head_equal: Tuple[str, ...]
    rebased: Tuple[str, ...]
    refused: Tuple[str, ...]


def _blob(common: Path, entry: Entry) -> Optional[bytes]:
    """Blob bytes of `entry`; b"" for an absent side; None when the object is unreadable."""
    if entry is None:
        return b""
    obj = read_object(common, entry[1])
    if obj is None or obj[0] != "blob":
        return None
    return obj[1]


def _read_ours(root: Path, rel: str) -> Optional[Tuple[Optional[bytes], bytes]]:
    """`(raw bytes or None when absent, check-in form)`; None for a shape this module does not hash."""
    from coordinator_core.git.commit import checkin_form

    target = root / rel
    if not os.path.lexists(target):
        return None, b""
    if target.is_symlink() or not target.is_file():
        return None
    data = target.read_bytes()
    out = checkin_form(root, rel, data)
    if out is None:
        return None
    return data, out


def _sync_one(
    root: Path, common: Path, rel: str, parent: Entry, landed: Entry
) -> Optional[str]:
    """Reconcile one path; returns "head_equal", "rebased", or None for refused (worktree untouched)."""
    if any(e is not None and e[0] in _SPECIAL_MODES for e in (parent, landed)):
        return None
    if checkin_attrs.clean_filter_may_apply(root, rel) is not None:
        return None
    base = _blob(common, parent)
    theirs = _blob(common, landed)
    if base is None or theirs is None:
        return None
    seen = _read_ours(root, rel)
    if seen is None:
        return None
    data, ours = seen
    absent = data is None
    if landed is None:
        if absent:
            return "head_equal"
        if ours != base or parent is None:
            return None
        merged: Optional[bytes] = None
        status = "head_equal"
    elif absent:
        if parent is not None:
            return None
        merged, status = theirs, "head_equal"
    elif ours == theirs:
        return "head_equal"
    elif ours == base:
        merged, status = theirs, "head_equal"
    else:
        merged = apply_own_delta(pre=base, post=theirs, head=ours)
        if merged is None or apply_own_delta(pre=base, post=ours, head=theirs) != merged:
            return None
        status = "rebased"

    payload = merged
    if payload is not None and data is not None and data != ours:
        if (
            b"\r" in payload
            or b"\r" in base
            or b"\r" in theirs
            or ours.replace(b"\n", b"\r\n") != data
        ):
            return None
        payload = payload.replace(b"\n", b"\r\n")

    target = root / rel
    tmp: Optional[Path] = None
    try:
        if payload is None:
            if _read_ours(root, rel) != (data, ours):
                return None
            target.unlink()
            return status
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{os.getpid()}.sync.tmp")
        tmp.write_bytes(payload)
        if os.name != "nt":
            perm = target.stat().st_mode & 0o777 if not absent else 0o644
            exec_bit = landed is not None and landed[0] == _EXEC_MODE
            os.chmod(tmp, (perm | 0o111) if exec_bit else (perm & ~0o111))
        if _read_ours(root, rel) != (data, ours):
            return None
        os.replace(tmp, target)
        tmp = None
        return status
    except OSError:
        return None
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass


def sync_landed_paths(
    repo: Union[str, Path], plan: Mapping[str, Tuple[Entry, Entry]]
) -> SyncOutcome:
    """Bring each landed path's worktree to HEAD, or to HEAD plus the worktree's own dirt.

    `plan` maps path -> `(parent_entry, landed_entry)`; an absent parent is a creation, an absent
    landed entry is a deletion. Zero git spawns.

    INVARIANT: a path is written only when the merge is symmetric (landed hunks and worktree-only
    hunks both present) and the worktree bytes are unchanged between the read and the replace.
    Collision, symlink or submodule, clean filter, over `MAX_MERGE_LINES`, a CR-mixed worktree and
    an unreadable blob are `refused` with the worktree byte-identical. A residual window remains
    between the final re-read and `os.replace`; no lock covers peer editors of the worktree.
    """
    root = Path(repo)
    common = resolve_git_common_dir(repo)
    head_equal: List[str] = []
    rebased: List[str] = []
    refused: List[str] = []
    for rel, (parent, landed) in plan.items():
        try:
            status = _sync_one(root, common, rel, parent, landed)
        except OSError:
            status = None
        if status == "head_equal":
            head_equal.append(rel)
        elif status == "rebased":
            rebased.append(rel)
        else:
            refused.append(rel)
    return SyncOutcome(tuple(head_equal), tuple(rebased), tuple(refused))
