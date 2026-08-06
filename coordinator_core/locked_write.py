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

    def locked_rmw(
        target: Path,
        mutate: Callable[[str], str],
        *,
        repo_root: Path,
        timeout: float = LOCK_TIMEOUT_SECS,
        missing_ok: bool = False,
    ) -> str: ...

Spec backlink: docs/plans/2026-07-06-ceremony-as-pipeline-2-invert-workstream.md § C1

Negative-spec:
  - Do NOT use threading.Lock — must serialise ACROSS processes.
  - Do NOT place the lock file under target's directory — it must survive
    delete-and-replace of the target.
  - Do NOT use os.link / rename for the lock itself — flock on a stable sidecar
    is the correct primitive on POSIX (avoids NFS/APFS caveats with link-count games).
  - Do NOT call locked_rmw re-entrantly on the same target in the same process:
    each call opens a fresh fd via os.open, so a second LOCK_NB attempt sees the
    first fd's lock and fails repeatedly until LockTimeout fires — effectively a
    local deadlock for the duration of ``timeout``.

Backend guard: locked_rmw needs one of fcntl (POSIX) or msvcrt (Windows). If a
future platform provides neither, all public names remain importable but
locked_rmw raises RuntimeError immediately rather than silently skipping the lock.

Lock-file convention: all sidecar lock files live under
``git_common_dir(target_repo) / "coordinator-locks" / "<sha1>.lock"``.
Using the git common dir (rather than a repo-relative path) means that
linked worktrees share the same lock namespace as the main checkout, so
concurrent writers across worktrees are serialised correctly.

Orphan accumulation: the sidecar ``.lock`` files are intentionally never
unlinked after release — deletion races on POSIX can cause a late opener to
receive a different inode and silently lose the serialisation guarantee.
These files are tiny (0 bytes) and reside inside ``.git/coordinator-locks/``,
a git-internal path that is never committed.  Accumulation of one file per
distinct target path is accepted in v1.  The named fallback design for
environments that cannot tolerate any accumulation is a parent-directory
flock (one sidecar per directory rather than per file).
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import time
from pathlib import Path
from typing import Callable

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


class LockTimeout(Exception):
    """Raised when locked_rmw cannot acquire the flock within ``timeout`` seconds.

    Fail-closed: no read, no mutate, no write has occurred.
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
    real = os.path.realpath(str(target))
    return hashlib.sha1(real.encode()).hexdigest()


def _lock_dir(repo_root: Path) -> Path:
    """Return the directory that holds all coordinator lock sidecars.

    Derives from the git common dir of the repository that *repo_root* belongs to,
    so worktrees share the same lock space as the main checkout.

    Uses coordinator_core.lifecycle.git_common_dir (lru_cache'd) to avoid
    spawning a subprocess on every call.
    """
    from coordinator_core.lifecycle import git_common_dir  # local import avoids cycles
    common = git_common_dir(repo_root)
    lock_dir = common / "coordinator-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    return lock_dir


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
            target_dir = target_path.parent
            target_dir.mkdir(parents=True, exist_ok=True)
            tmp_fd, tmp_path = tempfile.mkstemp(dir=str(target_dir))
            try:
                try:
                    os.write(tmp_fd, new_text.encode("utf-8"))
                finally:
                    os.close(tmp_fd)
                os.replace(tmp_path, str(target_path))
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
