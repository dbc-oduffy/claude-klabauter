"""coordinator_core.git.fleet_maintenance_lock -- the one box-wide lock over every pack-deleting path.

Purpose: `object_store_lock` is keyed per repo, so two engines (claude-klabauter and the klabauter twin) and
every repo's maintenance can still overlap across repos. Every path that deletes a pack file or
launches a repack, gc or maintenance holds `hold(wait_s=0, ...)` here, so at most one engine
process on the box is mutating any object store at a time.

Lock order: this fleet lock is the OUTER lock, `object_store_lock` the INNER one. Take the fleet
lock first, never the reverse; a holder of an `object_store_lock` must not wait on this one.

Backed by `locked_write.held_lock` (fcntl.flock on POSIX, msvcrt.locking on Windows; released by
the kernel on process death). The lock key is a path under the settings home, so every engine
install of one operator resolves the same lock; the sidecar file lives in held_lock's machine
rendezvous directory.

Negative-spec:
    - Does NOT gate a user's own `git gc`; per-repo config (`gc.auto=0`) is the lever there.
    - Does NOT block past `wait_s`: the caller defers on `FleetMaintenanceBusy`, nothing unlinked.
    - Is NOT a re-entrancy bypass across threads: depth is per thread.
    - Never falls back to a drive root: an unresolvable settings home is `FleetLockHomeUnresolved`.
"""

from __future__ import annotations

import contextlib
import threading
from pathlib import Path
from typing import Iterator

_depth = threading.local()

#: Path parts under the settings home that name the lock; only its identity matters, the file is
#: never created at this path.
_TARGET_PARTS = ("locks", "fleet-maintenance")

SETTINGS_HOME_KEY = "COORDINATOR_SETTINGS_HOME"


class FleetMaintenanceBusy(Exception):
    """Another engine process owned the fleet maintenance lock for the whole `wait_s`."""


class FleetLockHomeUnresolved(RuntimeError):
    """The settings home that keys the lock could not be resolved; names the key to set."""


def lock_target() -> Path:
    """The path whose identity keys the fleet lock; raises `FleetLockHomeUnresolved`."""
    from coordinator_core._settings_home import settings_home

    try:
        return settings_home().joinpath(*_TARGET_PARTS)
    except (ValueError, OSError) as exc:
        raise FleetLockHomeUnresolved(
            f"fleet maintenance lock needs a resolvable settings home; set {SETTINGS_HOME_KEY} ({exc})"
        ) from None


@contextlib.contextmanager
def hold(*, wait_s: float, holder_label: str) -> Iterator[None]:
    """Hold the fleet maintenance lock for the scope.

    Raises `FleetMaintenanceBusy` when it is not acquired within `wait_s` (0 tries once); nothing
    in the `with` body runs then. Re-entrant per thread: a nested `hold` only bumps a depth
    counter, because `held_lock` is non-reentrant (a second acquire would deadlock against the first).
    """
    target = lock_target()
    key = str(target)
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
            stack.enter_context(held_lock(target, timeout=wait_s, holder_label=holder_label))
        except LockTimeout as exc:
            raise FleetMaintenanceBusy(str(exc)) from None
        depths[key] = 1
        try:
            yield
        finally:
            depths.pop(key, None)
