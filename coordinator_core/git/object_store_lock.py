"""coordinator_core.git.object_store_lock -- the one engine-wide lock over a repo's `objects/pack`.

Purpose: every engine writer that mutates the pack directory (`git_maintenance.run_tier`,
`store_hygiene.consolidate_small_packs`, `git_objects._write_pack` / `_flush_pack`) holds
`hold(common_dir, ...)` around the mutation, so two engine writers can never overlap. A fold
deleting a tail pack while a repack reads it, or two repacks racing, is the shape that loses
objects. Git's own `maintenance.lock` is not consulted (measured not to exclude).

Backed by `locked_write.held_lock` (fcntl.flock on POSIX, msvcrt.locking on Windows; released
by the kernel on process death, so a crashed holder never strands the lock). The lock is keyed
on the repo's git COMMON dir, never a worktree gitdir, so every linked worktree shares it.

Negative-spec:
    - Does NOT gate a user's or third party's own `git gc`; only engine writers take it.
    - Does NOT block past `wait_s`: the caller defers (maintenance) or falls back to loose
      objects (the landing path) on `ObjectStoreBusy`.
    - Is NOT a re-entrancy bypass across threads: depth is per thread, so a second thread in
      the same process contends like any other holder.
"""

from __future__ import annotations

import contextlib
import threading
from pathlib import Path
from typing import Iterator

#: How long a landing may wait before falling back to loose objects. Far inside the 500ms
#: brightline (DR-344) and under the 200ms one-process line: a landing that cannot take the
#: lock inside 150ms writes loose objects, which no pack delete can lose, instead of queueing
#: behind a repack.
HOT_WAIT_S: float = 0.15

_depth = threading.local()


class ObjectStoreBusy(Exception):
    """Another holder owned the object-store lock for the whole `wait_s`."""


def _anchor_root(common_dir: Path) -> Path:
    """The checkout root `held_lock` derives the sidecar directory from."""
    return common_dir.parent if common_dir.name == ".git" else common_dir


@contextlib.contextmanager
def hold(common_dir: Path, *, wait_s: float, holder_label: str) -> Iterator[None]:
    """Hold the object-store lock of the repo owning `common_dir` for the scope.

    Raises `ObjectStoreBusy` when it is not acquired within `wait_s` (0 tries once); nothing in
    the `with` body runs then. Re-entrant per thread: a nested `hold` on the same `common_dir`
    only bumps a depth counter, because `held_lock` itself is non-reentrant (a second acquire
    opens a fresh fd and would deadlock against the first).
    """
    key = str(common_dir)
    depths = _depth.__dict__.setdefault("depths", {})
    if depths.get(key, 0) > 0:
        depths[key] += 1
        try:
            yield
        finally:
            depths[key] -= 1
        return

    from coordinator_core.locked_write import LockTimeout, held_lock

    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(
                held_lock(
                    common_dir / "objects" / "pack",
                    anchor_root=_anchor_root(common_dir),
                    timeout=wait_s,
                    holder_label=holder_label,
                )
            )
        except LockTimeout as exc:
            raise ObjectStoreBusy(str(exc)) from None
        depths[key] = 1
        try:
            yield
        finally:
            depths.pop(key, None)
