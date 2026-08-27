"""
coordinator_core.locked_write — cross-process file-level RMW lock helper.

Purpose: provide locked_rmw(), a cross-process read-modify-write primitive
that serialises concurrent writes to any single file across processes in the same
git repository, using a stable sidecar lock file under the git common directory
so that symlinks and worktrees produce no phantom duplicates.

Platform backing: the exclusive advisory lock is taken via ``fcntl.flock`` on
POSIX and ``msvcrt.locking`` on Windows — both are kernel-enforced and released
automatically on process death (so a crashed holder leaves no stale lock). The
two paths are unified behind ``_plat_try_lock`` / ``_plat_unlock`` below; every
caller of ``locked_rmw`` is platform-agnostic.

Public surface (pinned contract — do not change without updating consumers):

    class LockTimeout(Exception): ...
    class MutateAbort(Exception): ...
    LOCK_TIMEOUT_SECS: float = 10.0
    CONTENDED_LOCK_WAIT_SECS: float = 180.0   # ceiling, narrow-only
    CONTENDED_LOCK_WAIT_ENV: str = "COORDINATOR_LOCK_WAIT_SECS"

    def contended_lock_wait_secs(default: float = CONTENDED_LOCK_WAIT_SECS) -> float: ...

    def locked_rmw(
        target: Path,
        mutate: Callable[[str], str],
        *,
        repo_root: Path,
        timeout: float = LOCK_TIMEOUT_SECS,
        missing_ok: bool = False,
    ) -> str: ...

    class LockAnchorError(ValueError): ...

    @contextmanager
    def held_lock(
        target: Path,
        *,
        anchor_root: Path | None = None,
        timeout: float = LOCK_TIMEOUT_SECS,
        holder_label: str = "",
    ) -> Iterator[None]: ...

Spec backlink: pln-ceremony-as-pipeline-2-invert--fd1b98 § C1
                state/handoffs/2026-08-07-percolate-performance-delta-sweep.md Part B
                (`held_lock` — hold-for-a-scope wrapper around the same acquire/
                release primitive `locked_rmw` uses, for callers that need to hold
                an advisory lock across a whole operation rather than a single RMW).

Negative-spec:
  - Do NOT remove the upper clamp in ``contended_lock_wait_secs``, and do NOT
    add a second env var that reaches past it. ``CONTENDED_LOCK_WAIT_SECS`` is
    a ceiling, not a starting point: the env var and the ``default`` parameter
    narrow it and nothing widens it. An op that needs a longer wait to succeed
    is contending too hard, and the fix is fewer/shorter holds, not more
    sleeping processes on a box already running 50-70 of them.
  - Do NOT use threading.Lock — must serialise ACROSS processes.
  - Do NOT place the lock file under target's directory — it must survive
    delete-and-replace of the target.
  - Do NOT use os.link / rename for the lock itself — flock on a stable sidecar
    is the correct primitive on POSIX (avoids NFS/APFS caveats with link-count games).
  - Do NOT call locked_rmw re-entrantly on the same target in the same process:
    each call opens a fresh fd via os.open, so a second LOCK_NB attempt sees the
    first fd's lock and fails repeatedly until LockTimeout fires — effectively a
    local deadlock for the duration of ``timeout``.
  - Do NOT call held_lock re-entrantly on the same target in the same process —
    identical non-reentrancy contract to locked_rmw, same reason (fresh fd per call).
  - Do NOT re-introduce a required ``repo_root``-shaped parameter on held_lock.
    The sidecar DIRECTORY is what establishes the mutual-exclusion namespace,
    so letting each caller choose it independently is what made two callers
    naming the same ``target`` silently non-exclusive (see the fragility note
    below). held_lock now derives that anchor itself, from
    ``_machine_lock_dir()`` — a per-user, per-machine rendezvous outside
    every checkout; ``anchor_root`` exists only as an explicit opt-out for
    callers that must be namespaced elsewhere (the test suite), and is
    validated on the way in.
  - Do NOT re-derive held_lock's default namespace from the checkout
    shipping this module. That was the intermediate fix, and it still gave
    two INSTALLS of this engine two namespaces for one ``target`` — the
    residual closed below.
  - Do NOT put the machine rendezvous under ``tempfile.gettempdir()``. Temp
    reapers unlink files there; a sidecar deleted between two openers hands
    them different inodes and silently drops the serialisation guarantee.
  - Do NOT derive held_lock's anchor from ``target`` — that is precisely the
    ``.git``-rename incident below, and ``_assert_anchor_outside_target`` now
    rejects it (whether reached via ``anchor_root`` or the default).
  - Do NOT make ``_assert_anchor_outside_target`` stat the filesystem. It is
    pure path arithmetic over ``os.path.realpath`` because ``target`` is
    explicitly permitted not to exist (see held_lock's docstring); an
    existence-based check would reject the primary real caller, whose
    ``target`` is a destination repo root that may not be present yet.
  - Do NOT add a timestamp-based force-steal to held_lock. The OS already
    releases the underlying flock on holder process death (verified by
    TestCrashSafety in test_locked_write.py); the only thing that can go stale
    is the informational holder-metadata blob written into the sidecar, and
    that is naturally overwritten by the next successful acquirer. A
    time-based steal would race a legitimately long-held lock (a full
    klabauter publish runs ~10 minutes) and defeat the "fail loud, don't wait
    forever" goal held_lock exists for.

Holder metadata (for held_lock's fail-loud timeout message): on acquire, any
stale metadata already in the sidecar is cleared FIRST, then this process's
own PID, an opaque ``holder_label`` (e.g. the destination name), and an
ISO-8601 UTC acquire timestamp are written back in as one JSON line; on
release the sidecar is truncated back to empty (matching locked_rmw's
0-byte-sidecar convention). Clearing before writing closes the window a
killed prior holder can leave open: if that holder's release-side clear
never ran (SIGKILL, no ``finally``), its metadata bytes outlive it on disk
even though the OS has already reclaimed its flock; the very next acquirer
clears that stale-but-well-formed blob before writing its own, so a
concurrent `_describe_holder` read during the remaining sub-syscall window
between the clear and the write can only ever land on "holder unknown" —
never on a dead PID reported as if it were still live. A LockTimeout raised
by held_lock best-effort reads this metadata from a second, non-locking fd
to name the holder in the error message. The sidecar can also be corrupt or
absent — any read/parse failure (including reading zero bytes, which is what
the just-cleared-not-yet-written window looks like) degrades to "holder
unknown" rather than raising or naming a stale holder.

Backend guard: locked_rmw needs one of fcntl (POSIX) or msvcrt (Windows). If a
future platform provides neither, all public names remain importable but
locked_rmw raises RuntimeError immediately rather than silently skipping the lock.

Lock-file convention: all sidecar lock files live under
``<lock dir> / "<sha1>.lock"``. For locked_rmw the lock dir is
``git_common_dir(repo_root) / "coordinator-locks"`` — the git common dir
rather than a repo-relative path, so linked worktrees share the main
checkout's lock namespace and concurrent writers across worktrees are
serialised correctly. For held_lock it is ``_machine_lock_dir()``
(``~/.coordinator/coordinator-locks``), because its ``target`` is foreign to
the caller's repo and the namespace must not vary with which install is
running (see the Fragility section).

--------------------------------------------------------------------------
Fragility, proven not hypothetical (state/subagent-share, 2026-08-07 publish
`.git`-rename incident):
--------------------------------------------------------------------------
The lock KEY (``_lock_key``) derives solely from ``target``, but the
sidecar's DIRECTORY (``_lock_dir``) derives from an anchor path. Two
processes locking the SAME ``target`` under DIFFERENT anchors land in two
different sidecar files (same key, different directory) and are therefore
NOT mutually excluded, silently — no error, no diagnostic. The convention
that kept this safe was "every caller anchors to its own checkout", which
was load-bearing and, until now, enforced by nothing but prose.

held_lock no longer takes the anchor from the caller at all: it derives the
sidecar directory from ``_machine_lock_dir()``. ``locked_rmw`` still takes
``repo_root`` — its ``target`` is always a file inside the caller's own
repo, so the anchor is not a free choice there in the way it was for
held_lock, whose ``target`` is a FOREIGN repo root.

The first fix anchored held_lock to the checkout shipping this module,
which removed caller-vs-caller disagreement but left a residual: two
callers running from two DIFFERENT installs of this engine against one
``target`` still got two namespaces, silently. That residual is now CLOSED,
by ``_machine_lock_dir()`` — a per-user, per-machine rendezvous
(``~/.coordinator/coordinator-locks``) derived from neither ``target`` nor
any checkout, so two arbitrary installs resolve to the same sidecar for the
same ``target``. This machine is exactly the topology that made the
residual live (an engine-resident checkout plus a shared install; see
``verify-publish-targets-portable-sync.py``'s docstring on multi-copy
layouts).

Crash-release dependency, named (Review: code-reviewer P2): the argument
below — a killed holder self-releases, so nothing here wedges the way the
retired ceremony-lock did — holds only because a spawned child does NOT
inherit the open lock fd and outlive its parent. That in turn rests on two
CPython/OS defaults this module does not set and has never had to: ``os.open``
sets ``O_CLOEXEC`` on the fd by default since Python 3.4 (PEP 446), and
``subprocess.Popen`` defaults ``close_fds=True``, which closes non-CLOEXEC fds
in the child pre-exec regardless. If either default is overridden by a future
caller — a ``subprocess.Popen(..., close_fds=False)`` on POSIX while a
``held_lock`` scope is open, or an inheritable Windows handle — a child
spawned inside that scope could inherit the lock fd, survive the parent's
death, and keep the kernel lock held: the exact wedge this design is built to
rule out. Proved cross-process by
``TestMachineRendezvousCrashRelease.test_grandchild_spawned_under_the_lock_does_not_inherit_it``
in ``tests/test_locked_write_held_lock.py``, which holds the lock, spawns a
plain (default-flags) child while holding it, kills the holder, and confirms
the next acquirer is not blocked by the still-running child.

Why this is not the retired machine-global lock: the mechanism killed by PM
ruling on 2026-08-07 (``coordinator_core/ops/ceremony/ceremony_lock.py``)
wedged the tree because its liveness was a holder DESCRIBING ITSELF into a
directory, with no reaper outside the holding process — a holder that died
without unwinding was unreclaimable, and only a human ``rm`` recovered it.
Nothing here depends on a holder describing itself: liveness is the kernel
lock, which ``fcntl`` (POSIX) and ``msvcrt`` (Windows) both release on
process death. A crashed or SIGKILLed holder self-clears with no reaper and
no human intervention — proved cross-process against this very directory by
``TestMachineRendezvousCrashRelease`` in
``tests/test_locked_write_held_lock.py``, on top of the pre-existing
``TestCrashSafety``. The acceptance bar for adopting it was precisely that
property, not elegance.

Residual, stated and bounded: the rendezvous is per-OS-user. Two different
OS users publishing to one destination on one machine are not mutually
excluded. That shape is out of scope (the destination's file ownership
fails first) and is not closable on Windows in any case, which has no
cross-user-writable well-known directory analogous to POSIX ``/tmp`` — and
``/tmp`` is independently unusable here because temp reapers unlink
sidecars, splitting inodes between openers.

What IS now enforced (``_assert_anchor_outside_target``): the resolved
sidecar directory must not lie inside ``target``. A caller that (as
``coordinator/bin/publish.py`` once did) passes the same path as both
``target`` and anchor gets a lock sidecar living INSIDE the tree ``target``
names, which is actively dangerous if that tree is ever renamed, replaced,
or deleted out from under the open lock-file handle (see
``coordinator/bin/publish.py``'s ``_swap_publish_staging_into_dest``, which
renames ``.git`` — a held lock keyed there previously left an open handle
inside the very directory being renamed, producing a deterministic Windows
``PermissionError`` misdiagnosed as an antivirus race before this note was
added). That call shape now raises ``LockAnchorError`` before any handle is
opened and before the offending directory is created.

Residual risk of removing the transient-rename retry (Review: code-reviewer
P3): the same incident's original fix also removed
``_rename_tolerating_transient_windows_lock``, a retry-with-backoff wrapper
around the ``os.rename`` calls in ``_swap_publish_staging_into_dest``. That
retry was diagnosed as solving the wrong problem — it was masking a
self-held handle (this module's own lock sidecar, per the fragility note
above) for an entire diagnosis cycle, not a genuine external transient
holder (antivirus, search indexer) racing the rename. Two full publishes
plus a two-pass convergence proof have since run clean on a plain
``os.rename`` with no retry. This is an explicit, not silent, decision: a
genuine external-transient failure on those renames is an accepted residual
risk going forward, and should be re-diagnosed on its own evidence rather
than answered by restoring a blind retry that would once again paper over a
self-inflicted bug if one recurs.

Orphan accumulation: the sidecar ``.lock`` files are intentionally never
unlinked after release — deletion races on POSIX can cause a late opener to
receive a different inode and silently lose the serialisation guarantee.
These files are tiny (0 bytes for locked_rmw; one short JSON line for
held_lock) and reside either inside ``.git/coordinator-locks/``, a
git-internal path that is never committed, or — for held_lock — under
``~/.coordinator/coordinator-locks/``, outside every checkout and so never
committed either.  Accumulation of one file per distinct target path is
accepted in v1.  The named fallback design for
environments that cannot tolerate any accumulation is a parent-directory
flock (one sidecar per directory rather than per file).
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

# ---------------------------------------------------------------------------
# Platform lock backend detection
# ---------------------------------------------------------------------------
# fcntl on POSIX, msvcrt on Windows. Both give a kernel-enforced advisory lock
# that the OS releases on process death, so a crashed holder never strands the
# lock. locked_rmw needs exactly one of them; it raises only if NEITHER exists.

try:
    import fcntl as _fcntl  # noqa: F401 — presence check only
    _FCNTL_AVAILABLE = True
except ImportError:
    _FCNTL_AVAILABLE = False

try:
    import msvcrt as _msvcrt  # noqa: F401 — presence check only
    _MSVCRT_AVAILABLE = True
except ImportError:
    _MSVCRT_AVAILABLE = False

_LOCKING_AVAILABLE = _FCNTL_AVAILABLE or _MSVCRT_AVAILABLE

# ---------------------------------------------------------------------------
# Public constants & exceptions
# ---------------------------------------------------------------------------

LOCK_TIMEOUT_SECS: float = 10.0

#: Default wait for a lock whose holder is *working*, not wedged.
#:
#: ``LOCK_TIMEOUT_SECS`` is calibrated for a single-file RMW, where ten
#: seconds of contention means the holder is stuck. A per-destination publish
#: lock is the opposite case: the holder legitimately holds it for the length
#: of a round, and on this box (§ CLAUDE.md, Load norm) peers generate rounds
#: faster than the lock frees. Timing out at ten seconds there does not
#: protect anything — it converts one sleeping process into a fresh process
#: per retry, and process creation is the cost the brightline actually counts
#: (§ DR-344). Waiting is the cheap half; giving up early is the expensive one.
#:
#: It is a CEILING, not a starting point — see ``contended_lock_wait_secs``.
#: Nothing at runtime resolves above it; the ratchet test pins that.
CONTENDED_LOCK_WAIT_SECS: float = 180.0

#: Per-invocation override for ``contended_lock_wait_secs()``, NARROWING ONLY.
#: Named on the environment rather than passed down every call chain because
#: the acquire site is several process boundaries away from the session that
#: wants a shorter, fail-fast wait.
CONTENDED_LOCK_WAIT_ENV: str = "COORDINATOR_LOCK_WAIT_SECS"


def contended_lock_wait_secs(default: float = CONTENDED_LOCK_WAIT_SECS) -> float:
    """Resolve the contended-lock wait, honouring ``CONTENDED_LOCK_WAIT_ENV``.

    The env var and the *default* parameter may both LOWER the wait; neither
    can raise it above ``CONTENDED_LOCK_WAIT_SECS``. Both clamps are ``min``,
    matching the shape at ``coordinator_core.ipc :: _timeout_for`` — the knob
    that would otherwise be the escape hatch is the thing the clamp sits after.

    A malformed or non-positive value falls back to the ceiling rather than
    raising: the env var is an operator convenience on a hot path, and a typo
    in it must not be the reason a publish round refuses to run.

    Why the direction is one-way (2026-08-21 PM ruling, DR-344 § the
    brightline): the failure this closes is a session that hits contention,
    exports a bigger number, and re-runs — so the box carries a process asleep
    for as long as the number says, on a machine where 50-70 peers are already
    queued behind it. A wait that must be raised to succeed is a contention
    defect, not a dial position. Raising the ceiling is a PM-sized change to
    ``CONTENDED_LOCK_WAIT_SECS`` itself, guarded by
    ``coordinator_core/tests/test_contended_lock_wait_ratchet.py``.
    """
    ceiling = min(default, CONTENDED_LOCK_WAIT_SECS)
    raw = os.environ.get(CONTENDED_LOCK_WAIT_ENV, "").strip()
    if not raw:
        return ceiling
    try:
        parsed = float(raw)
    except ValueError:
        return ceiling
    if not parsed > 0:
        # `not parsed > 0` rather than `parsed <= 0`: `float("nan")` parses
        # cleanly and fails BOTH comparisons, and `min(nan, ceiling)` returns
        # the nan — an unorderable timeout handed straight to `_acquire_flock`.
        return ceiling
    return min(parsed, ceiling)


class LockTimeout(Exception):
    """Raised when locked_rmw cannot acquire the flock within ``timeout`` seconds.

    Fail-closed: no read, no mutate, no write has occurred.
    """


class LockAnchorError(ValueError):
    """Raised when a lock's sidecar directory would land INSIDE ``target``.

    A caller bug, not a runtime condition: it means the anchor that decides
    the sidecar's location was derived from the very resource being locked,
    so the lock would hold an open handle inside a tree the caller is about
    to rename or replace. Fail-closed and loud — raised before any lock file
    is opened or created.
    """


class MutateAbort(Exception):
    """Raised by a ``mutate`` callable to signal: do not write; propagate this error.

    The lock is released cleanly; the target file is left unchanged.
    Carry the op's error dict or message as the exception's args so the caller
    can map it to _err(...).
    """


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _lock_key(target: Path) -> str:
    """Return a stable hex identifier for the canonical form of *target*.

    Uses sha1(os.path.realpath(target)) so that symlinks, relative paths, and
    extra slashes all resolve to the same key.  The 40-char hex fits comfortably
    in any filesystem path without truncation.
    """
    # Function-local: this module sits on `coordinator_core.ipc`'s cold-start
    # import path, which is measured against a module-count ceiling
    # (`coordinator_core/benchmarks/import-budget-manifest.json`). Do not hoist.
    import hashlib

    real = os.path.realpath(str(target))
    return hashlib.sha1(real.encode()).hexdigest()


def _lock_dir_path(anchor_root: Path) -> Path:
    """Return where lock sidecars live for *anchor_root*, WITHOUT creating it.

    Split out from _lock_dir so held_lock can validate the anchor (see
    _assert_anchor_outside_target) before anything is written to disk — a
    mis-anchored call must not leave the very directory it is being rejected
    for behind on the filesystem.

    Derives from the git common dir of the repository that *anchor_root* belongs
    to, so worktrees share the same lock space as the main checkout.

    Uses coordinator_core.lifecycle.git_common_dir (lru_cache'd) to avoid
    spawning a subprocess on every call.
    """
    from coordinator_core.lifecycle import git_common_dir  # local import avoids cycles
    return git_common_dir(anchor_root) / "coordinator-locks"


def _lock_dir(repo_root: Path) -> Path:
    """Return the directory that holds all coordinator lock sidecars, creating it."""
    lock_dir = _lock_dir_path(repo_root)
    lock_dir.mkdir(parents=True, exist_ok=True)
    return lock_dir


MACHINE_LOCK_ROOT_ENV = "COORDINATOR_LOCK_ROOT"
"""Env var that relocates the machine rendezvous root (test isolation only).

Exactly the same status as ``held_lock``'s ``anchor_root``: an explicit
opt-OUT of the shared namespace, present so the test suite can get
per-``tmp_path`` isolation without writing into the real user home. Setting
it in a production environment for only some processes reintroduces the
split-namespace defect ``_machine_lock_dir`` exists to remove.
"""


def _machine_lock_dir() -> Path:
    """Return held_lock's machine-scoped rendezvous directory, WITHOUT creating it.

    ``held_lock``'s ``target`` is a FOREIGN path (a destination repo root),
    so the sidecar's directory cannot come from ``target`` (that is the
    ``.git``-rename incident below) and cannot come from the caller's own
    checkout either (two installs of this engine then get two namespaces and
    are silently not mutually excluded). The only namespace two arbitrary
    installs both resolve to for one ``target`` is one derived from neither:
    a per-user, per-machine directory.

    ``Path.home()`` is the derivation because it is the same value for one
    operator regardless of which checkout the engine was loaded from, takes
    no input the two callers could answer differently, and is not a
    sweep target (unlike ``tempfile.gettempdir()``, whose reapers can unlink
    a sidecar between two openers and hand them different inodes — the
    classic lock-file inode split).

    Scope, stated: the namespace is per-OS-user. Two different OS users
    publishing to one destination on one machine are not mutually excluded
    here — an out-of-scope shape (the destination's file ownership breaks
    first), and unclosable on Windows anyway, where there is no
    cross-user-writable well-known directory analogous to POSIX ``/tmp``.

    This is NOT the mechanism retired in
    ``coordinator_core/ops/ceremony/ceremony_lock.py``. That one wedged
    because its liveness came from a holder describing itself into a
    directory with no reaper outside the holding process, so a holder that
    died without unwinding was unreclaimable. Here liveness IS the kernel
    lock: ``fcntl.flock`` and ``msvcrt.locking`` are both released by the OS
    when the holding process dies, so a crashed or SIGKILLed holder
    self-clears with no reaper and no human ``rm`` (proved cross-process, in
    this very directory, by ``TestMachineRendezvousCrashRelease`` in
    ``tests/test_locked_write_held_lock.py``). The residue a dead holder can
    leave is the informational metadata blob, which the next acquirer
    overwrites.
    """
    override = os.environ.get(MACHINE_LOCK_ROOT_ENV)
    if override:
        return Path(override).expanduser() / "coordinator-locks"
    return Path.home() / ".coordinator" / "coordinator-locks"


def _assert_anchor_outside_target(target: Path, lock_dir: Path) -> None:
    """Reject an anchor whose sidecar directory would live inside *target*.

    Pure path arithmetic over os.path.realpath — deliberately never stats, so
    it stays correct for the non-existent ``target`` held_lock explicitly
    permits (a destination repo root that has not been created yet). realpath
    normalises a missing path rather than failing, and Path comparison is
    case-insensitive on Windows, so both platforms compare canonically.
    """
    real_target = Path(os.path.realpath(str(target)))
    real_lock_dir = Path(os.path.realpath(str(lock_dir)))
    if real_lock_dir == real_target or real_target in real_lock_dir.parents:
        raise LockAnchorError(
            f"refusing to lock {target}: the lock sidecar would live at "
            f"{real_lock_dir}, inside the very path being locked. The anchor "
            f"deciding the sidecar's location must be a checkout OUTSIDE "
            f"`target` (omit `anchor_root` to use the default, which is), "
            f"never `target` itself — a held lock keyed there leaves an open "
            f"handle inside a tree callers rename or replace."
        )


def _plat_try_lock(fd: int) -> bool:
    """Attempt one non-blocking exclusive lock on *fd*.

    Returns True if the lock was acquired, False if another holder currently
    holds it (the would-block case). Any other error propagates.

    POSIX: ``fcntl.flock(LOCK_EX | LOCK_NB)`` — would-block raises BlockingIOError.
    Windows: ``msvcrt.locking(LK_NBLCK, 1)`` locks a 1-byte range from offset 0;
    would-block raises OSError with errno EACCES(13) or EDEADLOCK(36). Locking a
    single byte at a fixed offset makes the whole file mutually exclusive for our
    purposes (every caller locks the same byte). The sidecar is a 0-byte file;
    Windows permits locking a range at/beyond EOF.
    """
    if _FCNTL_AVAILABLE:
        import fcntl
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False
    else:  # Windows msvcrt backend
        import errno
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EDEADLOCK):
                return False
            raise


def _plat_unlock(fd: int) -> None:
    """Release the exclusive lock held on *fd* (platform-dispatched)."""
    if _FCNTL_AVAILABLE:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)
    else:  # Windows: unlock the same 1-byte range from offset 0
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


def _acquire_flock(fd: int, timeout: float) -> None:
    """Acquire an exclusive lock on *fd*, polling with backoff up to *timeout* seconds.

    Raises LockTimeout if the lock cannot be obtained within *timeout* seconds.
    Uses a non-blocking poll so we can honour the timeout without blocking the
    process indefinitely. The name is historical (flock); the backend is
    platform-dispatched via _plat_try_lock.
    """
    deadline = time.monotonic() + timeout
    interval = 0.005  # 5 ms initial back-off
    max_interval = 0.1
    while True:
        if _plat_try_lock(fd):
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LockTimeout(
                f"Could not acquire coordinator lock within {timeout}s"
            )
        sleep_time = min(interval, remaining)
        time.sleep(sleep_time)
        interval = min(interval * 1.5, max_interval)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Windows refuses `os.replace` with WinError 5 (ERROR_ACCESS_DENIED) while ANY
# other handle is open on the destination -- including a plain reader that took
# no lock. The exclusive lock below excludes other WRITERS and does nothing
# about readers, so every atomic-publish site in this repo has a window where a
# concurrent read turns a successful write into a raised PermissionError.
#
# Not theoretical on this box: `hooks/track_touched_files.py` and
# `hooks/track_dispatched_agents.py` write through `locked_rmw` on the hook
# path, firing on every tool call across 50-70 concurrent sessions.
#
# Retrying past the reader's (very short) handle lifetime costs nothing on the
# uncontended path -- the first `os.replace` wins and returns.
#
# Provenance: found and paid for three passes deep in `warm/supervisor.py`'s
# discovery-record publish (2026-08-25), then lifted here rather than left as a
# one-site fix, because the bug is a SHAPE -- any atomic publish whose readers
# take no lock has it -- and a second hand-rolled copy would drift from this one.
REPLACE_RETRY_BUDGET_SECS = 0.25
REPLACE_RETRY_SLEEP_SECS = 0.002


def replace_with_retry(
    tmp_path: str,
    target: str,
    *,
    budget_secs: float = REPLACE_RETRY_BUDGET_SECS,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """`os.replace` with a bounded retry on the Windows sharing-violation path.

    Returns True if the replace won, False if the budget expired. NEVER raises
    `PermissionError` -- the caller decides what losing means, because the right
    answer genuinely differs by site:

      - A general read-modify-write (`locked_rmw`) must FAIL, since it returns
        `new_text` to a caller who will then believe it is on disk.
      - A published availability record (`warm.supervisor.write_discovery`) must
        FALL BACK to an in-place write, because raising there leaves the clone
        with no record at all -- strictly worse than one torn read.

    Any other `OSError` propagates unchanged: a cross-device link error or a
    vanished temp file is a real fault, not contention, and retrying it would
    turn a loud failure into a slow one.
    """
    deadline = time.monotonic() + budget_secs
    while True:
        try:
            os.replace(tmp_path, target)
            return True
        except PermissionError:
            if time.monotonic() >= deadline:
                return False
            sleep(REPLACE_RETRY_SLEEP_SECS)


def locked_rmw(
    target: Path,
    mutate: Callable[[str], str],
    *,
    repo_root: Path,
    timeout: float = LOCK_TIMEOUT_SECS,
    missing_ok: bool = False,
) -> str:
    """Read-modify-write *target* under an exclusive cross-process flock.

    Algorithm
    ---------
    1. Canonicalise key: sha1(os.path.realpath(target)).
    2. Lock sidecar: git_common_dir(target's repo) / "coordinator-locks" / "<sha1>.lock".
       (repo_root is the caller's repo; git_common_dir resolves worktrees transparently.)
    3. Acquire fcntl.LOCK_EX on the sidecar, poll-with-backoff up to *timeout* seconds.
       Raises LockTimeout on expiry (fail-closed: nothing has been read or written).
    4. Read *target* (or "" if missing_ok is True and the file is absent).
    5. Call mutate(old_text) -> new_text.
       - If mutate raises MutateAbort: release the lock, do NOT write, re-raise.
       - Any other exception propagates after the lock is released.
    6. If new_text is byte-identical to old_text: skip the write entirely (no mtime churn).
       Otherwise: atomic write via mkstemp + os.replace in target's directory.
    7. Return new_text.

    Non-reentrant: do NOT call locked_rmw while already holding the lock for the same
    target in the same process — each call opens a fresh fd via os.open; a second
    LOCK_NB attempt sees the first fd's lock held and fails repeatedly until
    LockTimeout fires (effectively a local deadlock for the duration of ``timeout``).

    Parameters
    ----------
    target:
        Path to the file to read-modify-write.  Need not exist if missing_ok=True.
    mutate:
        Pure function str -> str.  Receives current file text (or "" for absent),
        returns desired new text.  Raise MutateAbort to abort the write cleanly.
    repo_root:
        Any path inside the target file's git repository (worktree root or .git dir).
        Used to locate the git common dir and thus the lock sidecar directory.
    timeout:
        Maximum seconds to wait for the flock.  Default: LOCK_TIMEOUT_SECS (10 s).
    missing_ok:
        If True and *target* does not exist, mutate receives "".  If False (default)
        and *target* is absent, FileNotFoundError is raised before calling mutate.

    Returns
    -------
    str
        The new file text after mutation (same as what was written, or old text on
        no-op).
    """
    if not _LOCKING_AVAILABLE:
        raise RuntimeError(
            "locked_rmw requires a file-lock backend (fcntl on POSIX, msvcrt on "
            "Windows); neither is available on this platform"
        )

    key = _lock_key(target)
    lock_path = _lock_dir(repo_root) / f"{key}.lock"

    # Open (or create) the stable sidecar lock file.  Concurrent create is safe —
    # whichever process wins the kernel race just truncates an already-empty file,
    # harmless for a lock sidecar. Windows' msvcrt.locking needs read+write access
    # to lock a byte range, so open O_RDWR there; POSIX flock is content-agnostic
    # and keeps the historical O_WRONLY.
    _open_flags = os.O_CREAT | (os.O_WRONLY if _FCNTL_AVAILABLE else os.O_RDWR)
    lock_fd = os.open(str(lock_path), _open_flags, 0o600)
    try:
        _acquire_flock(lock_fd, timeout)
        # Lock is held from here through the write (or abort).
        try:
            # --- Read ---
            # Resolve symlinks so reads and writes go to the canonical file,
            # not through the symlink.  _lock_key already used realpath, so
            # the lock sidecar is already keyed to this same canonical path.
            target_path = Path(os.path.realpath(str(target)))
            if not target_path.exists():
                if missing_ok:
                    old_text = ""
                else:
                    raise FileNotFoundError(f"locked_rmw: target not found: {target}")
            else:
                old_text = target_path.read_text(encoding="utf-8")

            # --- Mutate ---
            # Review: code-reviewer (F4) — removed dead try/except that re-raised
            # unconditionally from both arms; lock is released by the finally clause
            # regardless, so the try/except had no effect.
            new_text = mutate(old_text)

            # --- Write (skip if identical) ---
            if new_text == old_text:
                return new_text

            # Atomic write: mkstemp in the same directory so os.replace is
            # guaranteed to be same-filesystem (avoids cross-device link errors).
            # Function-local, and the single largest saving on this module's
            # cold-start path: `tempfile` drags in `shutil` -> `bz2`/`lzma`,
            # which on Python 3.14 route through the new `compression` package
            # (`compression.zstd` included) — ~20 modules for one `mkstemp`.
            # Ceiling: `coordinator_core/benchmarks/import-budget-manifest.json`
            # `/entrypoints/coordinator_core.ipc`. Do not hoist.
            import tempfile

            target_dir = target_path.parent
            target_dir.mkdir(parents=True, exist_ok=True)
            tmp_fd, tmp_path = tempfile.mkstemp(dir=str(target_dir))
            try:
                try:
                    os.write(tmp_fd, new_text.encode("utf-8"))
                finally:
                    os.close(tmp_fd)
                if not replace_with_retry(tmp_path, str(target_path)):
                    # Contended past the budget. RAISE rather than fall back to
                    # a non-atomic write: this function returns `new_text` to a
                    # caller who will believe it reached disk, and a torn write
                    # here corrupts an arbitrary caller's file rather than one
                    # well-understood record.
                    raise OSError(
                        "locked_rmw: could not atomically replace %s within %.3fs -- "
                        "another process held the destination open for the whole "
                        "window" % (target_path, REPLACE_RETRY_BUDGET_SECS)
                    )
                tmp_path = None  # claimed; don't unlink in finally
            finally:
                if tmp_path is not None:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        # Best-effort cleanup of the stray tempfile after an upstream
                        # failure (os.replace didn't run); must not raise here and
                        # mask the original exception already propagating.
                        pass

            return new_text

        finally:
            # Explicit unlock — releasing before close minimises the hold window.
            # Closing the fd alone would also release the lock, but the explicit
            # unlock makes the release point unambiguous for code readers.
            # The sidecar file is NOT deleted — deletion races can cause a new
            # opener to get a different inode and lose the serialisation guarantee
            # (classic POSIX lock-file pitfall).
            _plat_unlock(lock_fd)

    finally:
        os.close(lock_fd)


# ---------------------------------------------------------------------------
# held_lock — scope-held advisory lock (Part B: concurrent-publish guard)
# ---------------------------------------------------------------------------

_METADATA_OFFSET = 1
"""Byte offset the JSON holder blob starts at, not 0.

`_plat_try_lock` locks a 1-byte range at offset 0 (msvcrt backend). Windows'
`msvcrt.locking` is a MANDATORY range lock — unlike POSIX `flock`, it also
blocks other processes' plain `read()` calls into that byte range, not just
competing `locking()` calls. A reader in `_describe_holder` opening a fresh
fd while the lock is held would therefore get a read error at offset 0 on
Windows and always degrade to "holder unknown", defeating the whole
fail-loud-with-a-name point of held_lock. Reserving byte 0 as an untouched
sentinel and writing the metadata from offset 1 keeps it outside the locked
range on Windows, and is harmless on POSIX (flock has no byte-range
semantics to begin with).
"""


def _write_holder_metadata(lock_fd: int, holder_label: str) -> None:
    """Write this process's holder metadata into *lock_fd* as one JSON line.

    Called only while the caller already holds the exclusive lock on lock_fd,
    so this write cannot race a concurrent holder. Writes from
    `_METADATA_OFFSET` (see its docstring) and truncates to the exact new
    length so a shorter payload never leaves trailing bytes from a longer
    previous one (defends the "corrupt sidecar" read path).
    """
    # Function-local — see `_lock_key`'s note on this module's cold-start budget.
    from datetime import datetime, timezone

    payload = json.dumps(
        {
            "pid": os.getpid(),
            "holder": holder_label,
            "acquired_at": datetime.now(timezone.utc).isoformat(),
        }
    ).encode("utf-8")
    os.lseek(lock_fd, _METADATA_OFFSET, os.SEEK_SET)
    os.write(lock_fd, payload)
    os.ftruncate(lock_fd, _METADATA_OFFSET + len(payload))


def _clear_holder_metadata(lock_fd: int) -> None:
    """Truncate the sidecar's metadata back to empty on release.

    Truncates to `_METADATA_OFFSET` rather than 0 so the reserved sentinel
    byte the lock range covers is left in place; matches locked_rmw's
    "sidecar shrinks back down, never deleted" convention.
    """
    os.ftruncate(lock_fd, _METADATA_OFFSET)


def _describe_holder(lock_path: Path) -> str:
    """Best-effort read of another process's holder metadata for an error message.

    Opens a fresh, non-locking fd — never attempts to acquire the lock — so this
    is safe to call while a different process holds it. Reads from
    `_METADATA_OFFSET` (see its docstring re: Windows mandatory range locks).
    Any failure (file missing, unreadable, truncated mid-write, not valid
    JSON, wrong shape) degrades to "holder unknown" rather than raising; the
    metadata is informational only and must never itself become a new
    failure mode.
    """
    try:
        fd = os.open(str(lock_path), os.O_RDONLY)
    except OSError:
        return "holder unknown"
    try:
        os.lseek(fd, _METADATA_OFFSET, os.SEEK_SET)
        raw = os.read(fd, 4096)
    except OSError:
        return "holder unknown"
    finally:
        os.close(fd)
    if not raw:
        return "holder unknown"
    try:
        data = json.loads(raw.decode("utf-8"))
        pid = data["pid"]
        holder = data.get("holder") or "(unlabelled)"
        acquired_at = data.get("acquired_at", "unknown time")
    except (ValueError, KeyError, UnicodeDecodeError, TypeError):
        return "holder unknown (corrupt lock metadata)"
    return f"pid={pid} holder={holder!r} acquired_at={acquired_at}"


@contextmanager
def held_lock(
    target: Path,
    *,
    anchor_root: "Path | None" = None,
    timeout: float = LOCK_TIMEOUT_SECS,
    holder_label: str = "",
) -> "Iterator[None]":
    """Hold an exclusive advisory lock on *target* for the duration of a scope.

    Purpose: exposes the same acquire/release primitive `locked_rmw` uses
    (`_lock_key`, `_lock_dir`, `_acquire_flock`, `_plat_try_lock`/`_plat_unlock`)
    to callers that need to hold a lock across a whole multi-step operation
    (e.g. a publish run) rather than a single read-modify-write. *target* need
    not be an existing file — it is only ever used to derive the lock key
    (`_lock_key`) and is never read or written; a convenient stable path that
    names the resource being protected (e.g. a destination repo root) is
    sufficient.

    Anchoring (enforced, not conventional)
    --------------------------------------
    Mutual exclusion comes from two callers resolving the same sidecar, and
    the sidecar's DIRECTORY comes from an anchor while its NAME comes from
    *target*. An anchor the caller supplies is therefore an anchor two
    callers can answer differently, silently losing exclusion — and so is an
    anchor derived from the caller's own checkout, once two installs of this
    engine are on one machine (which is routine here). So this function
    derives it from neither: ``_machine_lock_dir()``, a per-user,
    per-machine rendezvous outside every checkout, which two arbitrary
    installs both resolve to for the same *target*. The correct call is the
    short one: pass *target* and nothing else.

    ``anchor_root`` remains available as an explicit opt-out for callers that
    must be namespaced elsewhere. Passing it opts OUT of the shared namespace:
    a caller that passes one only some of the time for the SAME *target* has
    reintroduced exactly the defect this parameter's default exists to remove.

    Two legitimate opt-out classes, both required to pass it EVERY time for
    their target:

    1. The test suite, which needs per-``tmp_path`` isolation.
    2. The ``touched.txt`` writers — ``session/scope.py::touch()`` and
       ``session/claims.py::atomic_dedup_append`` (C2/C6,
       ``docs/plans/2026-08-14-cli-authored-writes-get-claimed.md``). Their
       *target* is a file inside the caller's OWN repo, not a foreign repo
       root, so the reasoning above (an anchor two installs answer
       differently) does not apply to them. They MUST anchor at the repo
       root, because the third writer of that same file —
       ``hooks/track_touched_files.py`` — locks it via ``locked_rmw``, whose
       sidecar resolves through ``_lock_dir_path``/``git_common_dir``. A
       default-anchored ``held_lock`` here would land in
       ``_machine_lock_dir()`` and exclude that writer not at all, silently.
       Do NOT "simplify" those call sites to the short form: it compiles,
       it runs, it locks, and it stops serialising the one writer it most
       needs to serialise against.

    Either way the resolved anchor is validated: if the sidecar directory
    would land inside *target* (the ``anchor_root == target`` shape
    ``coordinator/bin/publish.py`` once used), ``LockAnchorError`` is raised
    before any file is opened or created. See the module docstring's
    "Fragility" section for the incident.

    Raises LockTimeout, naming the current holder's PID/label/acquire time
    when that metadata is readable (see module docstring's "Holder metadata"
    section), if the lock cannot be acquired within *timeout* seconds.
    Fail-closed: on LockTimeout nothing in the `with` block ever runs.

    Non-reentrant: see module docstring's negative-spec — do not nest calls
    for the same target in the same process.

    Spec backlink: state/handoffs/2026-08-07-percolate-performance-delta-sweep.md Part B
    """
    if not _LOCKING_AVAILABLE:
        raise RuntimeError(
            "held_lock requires a file-lock backend (fcntl on POSIX, msvcrt on "
            "Windows); neither is available on this platform"
        )

    # Review: code-reviewer P3 — _assert_anchor_outside_target's realpath
    # comparison resolves a relative or Windows drive-relative path (e.g.
    # `C:foo`, no separator) against the process cwd, which could produce a
    # false NEGATIVE (an anchor wrongly judged safe). Both real call sites
    # already pass absolute paths; this makes that a checked precondition
    # rather than an unstated one, before any path arithmetic runs.
    if not os.path.isabs(str(target)):
        raise ValueError(f"held_lock: target must be an absolute path, got {target!r}")
    if anchor_root is not None and not os.path.isabs(str(anchor_root)):
        raise ValueError(
            f"held_lock: anchor_root must be an absolute path, got {anchor_root!r}"
        )

    lock_dir = (
        _machine_lock_dir() if anchor_root is None else _lock_dir_path(anchor_root)
    )
    _assert_anchor_outside_target(target, lock_dir)
    lock_dir.mkdir(parents=True, exist_ok=True)

    key = _lock_key(target)
    lock_path = lock_dir / f"{key}.lock"

    _open_flags = os.O_CREAT | (os.O_WRONLY if _FCNTL_AVAILABLE else os.O_RDWR)
    lock_fd = os.open(str(lock_path), _open_flags, 0o600)
    try:
        try:
            _acquire_flock(lock_fd, timeout)
        except LockTimeout:
            holder_desc = _describe_holder(lock_path)
            raise LockTimeout(
                f"Could not acquire lock for {target} within {timeout}s "
                f"(held by: {holder_desc})"
            ) from None

        # Lock held from here through the caller's scope.
        # Review: code-reviewer P2 — clear any stale metadata (e.g. left
        # behind by a killed prior holder whose release-side clear never
        # ran) before writing this holder's own, so a concurrent
        # _describe_holder read in the narrow window between the two calls
        # can only ever see "holder unknown", never a dead PID reported as
        # the live holder. See module docstring's "Holder metadata" section.
        _clear_holder_metadata(lock_fd)
        _write_holder_metadata(lock_fd, holder_label)
        try:
            yield
        finally:
            _clear_holder_metadata(lock_fd)
            _plat_unlock(lock_fd)
    finally:
        os.close(lock_fd)
