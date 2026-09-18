"""coordinator_core.group_em.atomic_record -- the shared primitives behind a holder record on
disk.

Ported from DoE-claude `coordinator/bin/lib/atomic_record.py` (W2-C1,
`docs/plans/2026-09-18-doe-holds-no-scripts.md`) -- that module's own docstring records it was
extracted from `group-em-nomination.py` so a second holder-record consumer (`uhura-mode.py`)
could share settings-home resolution, repo-key derivation and atomic-write behaviour by IMPORT,
not by reaching across a module boundary into another CLI's private names via a by-path load. A
private symbol reached through `importlib` is tighter coupling than a shared module, not looser --
the borrower breaks silently on any rename the lender makes, with no import graph showing the
dependency. That rationale is unchanged by the move to claude-klabauter; only the import surface changes,
from a hyphenated `coordinator/bin/*.py` CLI's `sys.path`-inserted `bin/lib` to an ordinary
`coordinator_core.group_em` package import.

Both callers keep their own record shape (fields, filename subdirectory, verbs) -- only the three
mechanical primitives live here.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Iterator, Optional


def settings_home() -> Path:
    override = os.environ.get("COORDINATOR_SETTINGS_HOME")
    if override:
        return Path(override)
    return Path.home() / ".coordinator-claude-settings"


def _safe_stem(text: str) -> str:
    return "".join(c for c in text if c.isalnum() or c in "-_")


def repo_key(repo_root: str) -> str:
    """Deterministic, collision-resistant filename stem for a repo root.

    A bare `_safe_stem` of the full path (statusline's pattern) risks collision once path
    separators are stripped -- two different repo roots can share the same trailing component.
    Appending a short hash of the case/separator-normalised path keeps the read path
    deterministic (no directory scan) while making that specific collision practically
    impossible.

    NOT NORMALISED, DELIBERATELY UNCLAIMED: two spellings of the SAME on-disk tree that are not
    merely a case/separator difference -- a mapped drive letter versus its UNC equivalent
    (`<drive>:\\repo` vs `\\\\<host>\\<share>\\repo`), or two different drive letters mapped to the
    same network share -- resolve to different keys here, because unifying them needs a
    filesystem-level identity check (volume GUID / resolving the mapping / `os.stat`
    device+inode) that this function does not perform. A nomination made under one spelling will
    not be found under the other. Only case and path-separator normalisation are covered;
    drive-letter/UNC identity is not.
    """
    normalised = os.path.normcase(os.path.normpath(repo_root))
    digest = hashlib.sha1(normalised.encode("utf-8")).hexdigest()[:10]
    stem = _safe_stem(Path(repo_root).name) or "repo"
    return f"{stem}-{digest}"


def write_json_atomic(target: Path, record: dict) -> None:
    """Swap a small JSON file in one step.

    Write-temp then `os.replace` means a concurrent reader sees the old record or the new one,
    never a partial one -- these records are read on hot paths (statusline) by any session on
    the machine. Callers whose writes are the record-of-record (not best-effort telemetry) must
    let a raised exception propagate; the `finally` below only cleans up the orphaned `.tmp`
    file on a failed write, it never turns the failure into success.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    tmp_path = Path(tmp)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def read_json_tolerant(path: Path) -> Optional[dict]:
    """A holder record's JSON, tolerant of a torn or malformed file -- the directory it lives in
    is written by concurrent sessions, so a torn record is an expected transient, not an error.

    Returns None on an unreadable path, invalid JSON, or a non-dict top level -- never raises.
    Callers decide what None MEANS (no record vs. corrupt record); this function only reads.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def remove_holder_record(
    path: Path, existing: dict, session_id: Optional[str]
) -> tuple[bool, int, Optional[str]]:
    """Match-check-then-unlink mechanics shared by every single-holder record's stand-down/
    release verb (`group-em-nomination.py stand_down`, `navi-singleton.py release`).

    If `session_id` is given it must match `existing`'s holder -- a caller-supplied mismatch is
    refused (exit 5), never silently accepted; omitting `session_id` removes whichever session
    currently holds the record (operator override). A swallowed unlink failure would be exactly
    the "silent lie" this exists to prevent -- the caller would believe the role vacated while
    the record, and the mutual-exclusion state it represents, is still on disk -- so that path
    returns exit 4 instead.

    Returns `(ok, exit_code, detail)`: `detail` is the prior holder's `session_id` on a mismatch
    refusal, the `OSError` detail string (`"{strerror}, errno={errno}"`) on an unlink failure,
    or `None` on success (exit 0). Message text stays the caller's job -- the two domains word
    it differently.
    """
    holder = str(existing.get("session_id") or "")
    if session_id and holder != session_id:
        return False, 5, holder
    try:
        path.unlink()
    except OSError as exc:
        return False, 4, f"{exc.strerror or exc}, errno={exc.errno}"
    return True, 0, None


class LockTimeout(RuntimeError):
    """Raised when `holder_lock` cannot acquire its lock file within `timeout` seconds."""


def _try_lock(fd: int) -> None:
    """Take a non-blocking exclusive OS lock on `fd`; raise OSError if another process holds it."""
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


@contextlib.contextmanager
def holder_lock(
    path: Path,
    *,
    timeout: float = 5.0,
    poll_interval: float = 0.05,
) -> Iterator[None]:
    """Serialize a read-check-write sequence against `path` under an exclusive OS lock.

    The lock is a kernel advisory lock on a sibling `<name>.lock` file: `fcntl.flock` on POSIX,
    `msvcrt.locking` on Windows. The kernel releases it when the holding process exits, crash
    included, so there is no staleness judgement to get wrong. Every other caller retries at
    `poll_interval` until the holder releases, or raises `LockTimeout` after `timeout`.

    Negative spec: the lock file is never unlinked. Deleting it while a waiter holds an open
    descriptor would let a third caller lock a fresh inode beside it -- two holders at once. An
    empty `<name>.lock` left on disk is inert.
    """
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        # Review: code-reviewer Finding 1 -- msvcrt.locking on Windows requires
        # the locked byte range to lie within the file's actual extent; a
        # freshly O_CREAT'd zero-length file fails to lock on the very first
        # call. Size the file to >=1 byte unconditionally before ever locking.
        os.ftruncate(fd, 1)
        deadline = time.monotonic() + timeout
        while True:
            try:
                _try_lock(fd)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise LockTimeout(f"could not acquire lock {lock_path} within {timeout}s")
                time.sleep(poll_interval)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


def load_by_path(name: str, path: Path):
    """Load a module by file path -- for the handful of hyphenated-filename CLIs (a DoE-plane
    shape this repo does not carry) whose names are not import-safe as package modules. Kept
    for parity with the ported module's contract; claude-klabauter's own `group_em` package modules never
    need this (they are ordinary importable names), so it currently has no in-repo caller here.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {name} from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
