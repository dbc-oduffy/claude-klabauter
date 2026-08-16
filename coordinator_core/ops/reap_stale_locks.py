"""
coordinator_core.ops.reap_stale_locks — ported from
coordinator/bin/coordinator-reap-stale-locks (DOE-PORT bin-entrypoint variant), with its
coordinator/lib/coordinator-session.sh sourcing severed entirely (this port needs none of
that library's session/liveness/claim machinery — the bash oracle only sourced it for
portable stat helpers, which this port replaces with os.stat).

Purpose: conservative self-heal for the orphaned-`.git/index.lock` failure mode under
concurrent-EM on Git-for-Windows. The root cause is a Windows file-sharing artifact: a
foreground `git add`/`git commit` writes a full index copy to `index.lock`, and the final
rename(index.lock -> index)/unlink can fail with a sharing violation when another process
(a concurrent session's git, antivirus, the search indexer, or git's detached auto-maintenance
child) holds a handle. The commit's objects+ref are already durable (the commit "succeeds")
but the orphan lock survives and blocks every subsequent commit in that worktree with
`fatal: Unable to create '.../.git/index.lock': File exists.`

`.git/index.lock` carries NO holder PID, so liveness cannot be read from the lock itself.
This reaper therefore gates every candidate lock on TWO conservative conditions, both of
which must hold before any removal:
    1. AGE    — the lock is older than a threshold far beyond any legitimate commit
                (default 120s; gc/maintenance gets a larger threshold since it runs longer).
    2. STABLE — its (mtime, size) do not change across a short re-sample window, i.e. no
                process is actively writing it right now.
A fresh or actively-mutating lock is NEVER reaped — fail-safe, the caller proceeds (or
fails loud) rather than risk corrupting a genuinely in-flight commit's index.

Behavior contract (byte-for-byte parity with the bash oracle):
    Exit codes:
        0 — clean: nothing to reap, or all stale locks reaped successfully.
        2 — a FRESH index.lock is present (a live commit may be in progress); not reaped.
            Informational for the caller — distinct from a hard error.
        1 — hard error (not in a git repo, or a reap was attempted and unlink failed).
    stdout/stderr: no stdout. Reap/error/not-a-repo messages go to stderr, prefixed
        "coordinator-reap-stale-locks: ", matching the bash oracle's message text so any
        caller grepping stderr for that prefix keeps working unmodified.
    Always safe to call repeatedly (idempotent) and from a hot path (commit pre-flight).

Locks covered, per-worktree vs shared-object-store (unchanged from the bash oracle):
    - index.lock            — per-worktree (`git rev-parse --absolute-git-dir`).
    - next-index-*.lock     — per-worktree, same dir as index.lock.
    - objects/maintenance.lock — SHARED object store (`git rev-parse --git-common-dir`),
      which differs from --git-dir inside a linked worktree; resolving it via --git-dir
      would silently never fire there.

Env tunables (test seams, same names as the bash oracle):
    COORDINATOR_LOCK_REAP_AGE_SEC        (default 120)  — index/next-index age floor.
    COORDINATOR_LOCK_REAP_MAINT_AGE_SEC  (default 600)  — maintenance.lock age floor.
    COORDINATOR_LOCK_REAP_STABILITY_SEC  (default 2)    — re-sample wait, in seconds.
    COORDINATOR_LOCK_REAP_NO_SLEEP       (any non-empty value) — skip the real sleep
        entirely (used by callers/tests that don't want to pay real wall-clock time).

Negative-spec (faithfully reproduced bash-oracle behavior — do NOT "fix" mid-port):
    - Does NOT hold any lock/mutex while deciding to reap — the residual TOCTOU window
      between the stability decision and unlink is bounded and safe: git creates
      index.lock with O_CREAT|O_EXCL, so a legitimate git index write can never target an
      already-existing orphan (it would fail "File exists" — the very symptom this heals).
    - Does NOT treat "lock vanished on its own between samples" as an error — the owner
      released it; nothing to reap, not reaped, not an error.
    - Does NOT surface a "fresh" verdict for next-index-*.lock or maintenance.lock the way
      it does for index.lock — those return plain not-reaped without setting the caller's
      distinguished exit-2 signal (mirrors the bash oracle's `fresh_index` variable, which
      only index.lock sets).
    - Does NOT reimplement session/claim/liveness machinery from coordinator-session.sh —
      that dependency is severed; this module is self-contained stdlib.
    - Does NOT replicate the bash oracle's arithmetic-error path for a malformed tunable
      env var: `_env_int`/`_env_float` catch a non-numeric value and fall back to the
      documented default, whereas the oracle feeds the raw string into `(( ))` and hits
      a bash arithmetic error. Deliberate improvement (fail-safe default over an
      unpredictable arithmetic error), not an oversight.

Spec backlink: cross-repo/inbox/2026-05-30-index-lock-leak-concurrent-em.md (example-game-repo
    consult); docs/wiki/concurrent-em-hazards.md § H21 (DoE-claude tree).
Prior bash implementation: coordinator/bin/coordinator-reap-stale-locks (DoE-claude tree,
    read-only oracle for this port).
"""


from __future__ import annotations

GENERATES = []  # removes only stale .git/index.lock, .git/next-index-*.lock, .git/objects/maintenance.lock -- never a tracked repo artifact

import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from coordinator_core.git.repo_root import absolute_git_dir, git_common_dir
from coordinator_core.session.declared_writes import declare_write
from coordinator_core.win_portability import no_console_creationflags

_DEFAULT_AGE_SEC = 120
_DEFAULT_MAINT_AGE_SEC = 600
_DEFAULT_STABILITY_SEC = 2.0

_PREFIX = "coordinator-reap-stale-locks"


def _now_epoch() -> int:
    return int(time.time())


def _mtime_epoch(path: Path) -> int:
    try:
        return int(path.stat().st_mtime)
    except OSError:
        return 0


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return -1


def _git_absolute_git_dir(cwd: Optional[Path] = None) -> Optional[Path]:
    out = absolute_git_dir(str(cwd) if cwd is not None else None)
    return Path(out) if out else None


def _git_common_dir(git_dir: Path, cwd: Optional[Path] = None) -> Path:
    out = git_common_dir(str(cwd) if cwd is not None else None)
    if not out:
        return git_dir
    common = Path(out)
    if common.is_absolute():
        return common
    base = cwd if cwd is not None else Path.cwd()
    return (base / common).resolve()


def _append_log(reap_log: Path, message: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Review: code-reviewer (Finding 4) — force LF regardless of platform newline
    # translation; the bash oracle's `>>` redirection always appends a literal LF,
    # including on Windows/MSYS, so default text-mode writes (which would emit CRLF
    # on native Windows Python) would break byte-for-byte log parity.
    with open(reap_log, "a", encoding="utf-8", newline="\n") as f:
        f.write(f"[{ts}] {message}\n")
    # DR-276: declared AFTER the write lands, never before — the contract is a
    # report of what was ACTUALLY written, not of an intended surface.
    declare_write(reap_log)


def stale_and_stable(
    lock: Path,
    age_floor: int,
    stability_sec: float,
    no_sleep: bool,
    on_wait: Optional[Callable[[], None]] = None,
) -> bool:
    """Return True iff `lock` is older than `age_floor` AND its (mtime, size) are
    unchanged across the re-sample window. Returns False if absent, fresh, or
    actively mutating.

    `on_wait`, when given, replaces the real sleep entirely — the injectable resample
    seam tests use to simulate a mutating writer deterministically, without paying
    real wall-clock time.
    """
    if not lock.exists():
        return False
    now = _now_epoch()
    mtime1 = _mtime_epoch(lock)
    age = now - mtime1
    if age < age_floor:
        return False
    size1 = _file_size(lock)

    if on_wait is not None:
        on_wait()
    elif not no_sleep:
        time.sleep(stability_sec)

    if not lock.exists():
        return False
    mtime2 = _mtime_epoch(lock)
    size2 = _file_size(lock)
    return mtime1 == mtime2 and size1 == size2


def do_reap(
    lock: Path,
    age_floor: int,
    label: str,
    reap_log: Path,
    stability_sec: float,
    no_sleep: bool,
    on_wait: Optional[Callable[[], None]] = None,
) -> int:
    """Attempt to reap `lock`. Returns 0 (reaped), 1 (rm failed — hard error), or
    2 (present but not stale/stable, or absent)."""
    if not stale_and_stable(lock, age_floor, stability_sec, no_sleep, on_wait):
        return 2
    try:
        lock.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        _append_log(reap_log, f"FAILED to reap {label}: {lock} (rm failed)")
        print(f"{_PREFIX}: ERROR — rm failed on {lock}", file=sys.stderr)
        return 1
    _append_log(reap_log, f"reaped stale {label}: {lock}")
    print(f"{_PREFIX}: reaped stale {label} ({lock})", file=sys.stderr)
    return 0


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def main(argv: list[str]) -> int:
    # Review: code-reviewer (Finding 1) — the bash oracle never inspects $@ at all;
    # an argparse-based flag surface printed the docstring to stdout on --help and
    # exited 2 on any stray arg, both violating the "no stdout" / byte-for-byte
    # parity contract documented above. `argv` is intentionally unused, matching
    # the oracle exactly.
    del argv

    age_sec = _env_int("COORDINATOR_LOCK_REAP_AGE_SEC", _DEFAULT_AGE_SEC)
    maint_age_sec = _env_int("COORDINATOR_LOCK_REAP_MAINT_AGE_SEC", _DEFAULT_MAINT_AGE_SEC)
    stability_sec = _env_float("COORDINATOR_LOCK_REAP_STABILITY_SEC", _DEFAULT_STABILITY_SEC)
    no_sleep = bool(os.environ.get("COORDINATOR_LOCK_REAP_NO_SLEEP", ""))

    git_dir = _git_absolute_git_dir()
    if git_dir is None:
        print(f"{_PREFIX}: not a git repository", file=sys.stderr)
        return 1
    git_common_dir = _git_common_dir(git_dir)

    reap_log = git_dir / "lock-reap.log"

    rc = 0
    fresh_index = False

    index_lock = git_dir / "index.lock"
    if index_lock.exists():
        result = do_reap(index_lock, age_sec, "index.lock", reap_log, stability_sec, no_sleep)
        if result == 1:
            rc = 1
        elif result == 2:
            fresh_index = True

    for next_lock in sorted(git_dir.glob("next-index-*.lock")):
        result = do_reap(next_lock, age_sec, "next-index lock", reap_log, stability_sec, no_sleep)
        if result == 1:
            rc = 1

    maint_lock = git_common_dir / "objects" / "maintenance.lock"
    if maint_lock.exists():
        result = do_reap(maint_lock, maint_age_sec, "maintenance.lock", reap_log, stability_sec, no_sleep)
        if result == 1:
            rc = 1

    if rc == 1:
        return 1
    if fresh_index:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
