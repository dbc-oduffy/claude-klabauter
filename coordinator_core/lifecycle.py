"""
coordinator_core.lifecycle — repo-root utilities and version helpers.

Stripped by C3 (2026-07-06): resident-daemon machinery (singleton lock,
IdleWatchdog, socket path/orphan-sweep, sentinels+atexit cleanup,
detached-spawn, partition registry, drain gate/in-flight counter, context class [C5])
removed.

Stripped by C4 (2026-07-06): version-skew subsystem removed (moot under the
command-type execution model — always runs current source).  Removed:
_dev_mode_enabled, _max_source_mtime, check_version_skew,
_source_tree_dirty, _source_tree_dirty_global, VersionSkewError,
assert_version_current.

Full implementations (always-survivor):
    find_repo_root          — git rev-parse --show-toplevel
    git_common_dir          — git rev-parse --git-common-dir (lru-cached)
    sentinel_dir            — <common-git-dir>/coordinator-service/
    _compute_core_version   — SHA-256 over non-test .py files
    global_sentinel_dir     — machine-global /tmp sentinel dir (used by read/version helpers)
    read_global_running_version  — version sentinel read
    read_running_version    — backward-compat shim over read_global_running_version
    is_version_current      — running-version == source-hash check

Tombstone stubs (importable but non-functional):
    global_socket_path, uds_socket_path, sweep_orphaned_sockets,
    read_endpoint, _mark_partition_active, arm_drain_flag,
    get_in_flight_count, get_or_create_partition, in_flight_decrement,
    in_flight_increment, is_draining.

Remaining live consumer of tombstone stubs: ops/health.py imports
    uds_socket_path and read_endpoint (post-daemon semantics: both return
    NotImplementedError / None respectively).

Spec backlink: docs/plans/2026-07-05-coordinator-core-execution-model-retirem.md § C4
Anti-scope: do NOT restore daemon machinery; do NOT restore version-skew subsystem;
            do NOT remove repo-root utils or tombstone stubs (C5 owns those).
"""

from __future__ import annotations

import functools
import hashlib
import os
from pathlib import Path
from typing import Optional

from coordinator_core.git import repo_root as _repo_root_seam

# Deferred-import accessor (Windows hot-path import diet — docs/plans/
# 2026-08-06-windows-hot-path-less-work-per-interpreter.md § C9c).
#
# `logging` costs ~8.1ms cold on this repo's chain (measured via Scope E,
# state/audits/2026-08-06-windows-hostility-census/E-import-cold-start.md)
# and this module's only use of it is `logging.getLogger(__name__)` plus a
# handful of `.warning()` calls in `_compute_core_version` — no other
# `logging.*` API is used at module scope.  Deferring the `import logging`
# itself (not just wrapping the getLogger call) is what actually avoids the
# cost on a cold interpreter; a module-level `logger = logging.getLogger(...)`
# idiom cannot be preserved verbatim without paying the import eagerly, so
# this lazily creates-and-caches the SAME named logger on first use instead.
# Safe: `logging.getLogger(name)` returns the same object for a given name
# regardless of when it is first called (Manager keeps loggers by name), so
# deferring creation changes only WHEN the logger object is instantiated,
# never its identity or any external configuration applied to that name.
_logger = None


def _log():
    global _logger
    if _logger is None:
        import logging
        _logger = logging.getLogger(__name__)
    return _logger

# Sentinel directory name under .git/
_SENTINEL_DIR = "coordinator-service"

# Sentinel file names (version survives — used by read_global_running_version)
_FILE_VERSION = "version"


# ---------------------------------------------------------------------------
# Repo-root discovery
# ---------------------------------------------------------------------------

def find_repo_root(cwd: Optional[str] = None) -> Path:
    """Return the canonical absolute repo root via git rev-parse --show-toplevel.

    Raises RuntimeError if the working directory is not inside a git repo.
    """
    toplevel = _repo_root_seam.show_toplevel(cwd)
    if toplevel is None:
        raise RuntimeError(
            "git rev-parse --show-toplevel failed (not a git repo?)"
        )
    return Path(toplevel).resolve()


@functools.lru_cache(maxsize=32)
def git_common_dir(repo_root: Path) -> Path:
    """Return the absolute path to the git common directory (always a real directory).

    Purpose: for a regular repository, this is repo_root/.git.  For a linked git
    worktree, repo_root/.git is a FILE (a gitdir: pointer), NOT a directory; this
    function returns the main worktree's .git directory instead, which is always a
    real directory and safe to mkdir under.

    Uses --path-format=absolute so that git emits an absolute path directly — this
    avoids the relative-path trap where Path(x).resolve() would resolve against the
    CURRENT PROCESS working directory instead of the subprocess cwd=repo_root,
    silently producing a wrong path.

    Negative-spec:
      - Do NOT call .resolve() on the returned path — it is already absolute (git
        guarantees this with --path-format=absolute).
      - Do NOT omit --path-format=absolute and use .resolve() as a substitute —
        that resolves relative to the calling process cwd, not the subprocess cwd.

    Raises RuntimeError if git fails (e.g. not inside a git repository).
    Results are cached per repo_root (lru_cache, maxsize=32) — the common dir
    for a given repo does not change during a process lifetime.
    """
    common_dir = _repo_root_seam.git_common_dir(str(repo_root))
    if common_dir is None:
        raise RuntimeError("git rev-parse --git-common-dir failed")
    return Path(common_dir)


def main_worktree_root(common_dir: Path) -> Path:
    """Return the main worktree root from the git common dir.

    For the standard .git-under-worktree layout, lifecycle.git_common_dir() returns
    <main-worktree>/.git for both a regular repository and a linked worktree
    (see git_common_dir()).  The main worktree is therefore common_dir.parent.

    NEGATIVE-SPEC (standard-layout assumption):
    - Assumes .git is a real directory directly under the main worktree root.
      Does NOT handle bare repos, repos with --separate-git-dir, repos where
      .git is in a non-standard location, or git worklists layouts that produce
      a common dir that is not <worktree>/.git.
    - Handlers MUST call this helper — do NOT inline a bare .parent anywhere.
      This function is the single reviewed home for the derivation and its
      documented assumption.  Inlining .parent skips the negative-spec and makes
      the standard-layout assumption implicit and unauditable.
    - params.repo_root is NEVER used as the worktree source (it is the D3 check only).
      This helper derives the worktree from the engine-supplied common_dir arg,
      which is socket-authoritative (cannot be spoofed by the caller).
    """
    return common_dir.parent


# ---------------------------------------------------------------------------
# Sentinel directory
# ---------------------------------------------------------------------------

def sentinel_dir(repo_root: Path) -> Path:
    """Return the sentinel directory under the repository's common git directory.

    For a regular repository: <repo_root>/.git/coordinator-service/
    For a linked git worktree: <main-worktree>/.git/coordinator-service/

    Uses git_common_dir() so the path always resolves to a real directory rather
    than through <worktree>/.git (which is a file, not a directory, in a linked
    worktree).  Falls back to the legacy repo_root/.git/<sentinel> path when git is
    unavailable (e.g. in unit tests running in a bare tmp_path).

    Negative-spec:
      - Do NOT derive this path as repo_root / ".git" / _SENTINEL_DIR — that
        mkdir crashes with NotADirectoryError in a linked worktree because
        <worktree>/.git is a gitdir-pointer FILE, not a directory.
    """
    try:
        common = git_common_dir(repo_root)
    except RuntimeError:
        # Not a git repo (e.g. test tmp_path); fall back to the legacy layout.
        common = repo_root / ".git"
    return common / _SENTINEL_DIR


def global_sentinel_dir() -> Path:
    """Return the machine-scoped global sentinel dir for coordinator service state.

    Production default (COORDINATOR_SVC_ROOT unset):
        /tmp/coordinator-svc-<uid>/coordinator-service/
        Parent is created with mode 0700 (user-only security boundary).

    Injectable override (COORDINATOR_SVC_ROOT set):
        $COORDINATOR_SVC_ROOT/coordinator-service/
        Used in tests to give each test an isolated sentinel dir so full-suite
        parallel/sequential runs do not collide on the machine-global lock.

    Read at CALL TIME (not module import) so per-test monkeypatch / os.environ
    mutation takes effect without a process restart.

    Spec backlink: docs/plans/2026-07-04-coordinator-core-global-multiplex-migration.md § C1a
    """
    svc_root = os.environ.get("COORDINATOR_SVC_ROOT")
    if svc_root:
        d = Path(svc_root) / "coordinator-service"
    else:
        # Cross-platform svc-root. POSIX keeps the historical /tmp/coordinator-svc-<uid>
        # (short path — stays within the macOS AF_UNIX sun_path limit). Windows has no
        # os.getuid and its temp dir is already per-user, so use tempfile.gettempdir()
        # with the login name as the namespacing token.
        if hasattr(os, "getuid"):
            _svc_token = str(os.getuid())
            _svc_base = Path("/tmp")
        else:  # Windows
            import getpass
            import tempfile
            _svc_token = getpass.getuser()
            _svc_base = Path(tempfile.gettempdir())
        parent = _svc_base / f"coordinator-svc-{_svc_token}"
        parent.mkdir(exist_ok=True)
        try:
            parent.chmod(0o700)  # user-only boundary on POSIX; best-effort/no-op on Windows
        except (NotImplementedError, OSError):
            pass
        d = parent / "coordinator-service"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Version hash — content hash of the coordinator_core package source
# ---------------------------------------------------------------------------

def _compute_core_version() -> str:
    """Return a SHA-256 hex digest over this package's Python source files.

    Used as the `version` sentinel to detect stale-binary clients (design spike §1.5).
    Only includes .py files under the coordinator_core package directory.

    Review: code-reviewer (F4) / EM-ratified (option a, docstring-only) — an unreadable
    subtree is excluded from the hash rather than failing the computation outright, which is
    the correct trade-off for this sentinel's post-DR-215 spawn-per-call consumer
    (`is_version_current()` / `ops/health.py`): excluding a subtree CHANGES the computed
    digest relative to any baseline that was hashed with that subtree readable, so exclusion
    can only ever manufacture a false version-SKEW signal (loud, safe) — it can never mask a
    genuine skew by coincidentally matching a stale baseline, because the excluded bytes are
    simply missing from both sides of the comparison, not silently treated as unchanged. The
    accompanying `logger.warning` per excluded subtree (below) makes the exclusion itself
    diagnosable, so a false-positive skew traces directly to the unreadable path rather than
    reading as an unexplained restart trigger.
    """
    pkg_dir = Path(__file__).parent
    hasher = hashlib.sha256()

    # NOTE: uses os.walk(onerror=...), NOT rglob("*.py") — Path.glob()'s selector
    # silently swallows PermissionError while walking (an unreadable subtree makes
    # rglob() yield nothing for it, no exception), which would silently exclude that
    # subtree from the version hash with no log line. os.walk's onerror hook is the
    # standard way to observe (rather than silently skip) an unwalkable directory.
    walk_errors: list[OSError] = []
    py_files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(pkg_dir, onerror=walk_errors.append):
        # Review: code-reviewer (F1) — exclude tests/ subtree; test-file edits must not
        # change the production source hash or trigger spurious version-skew restarts.
        # Pruned during the walk (not filtered after) so an unreadable file under a
        # tests/ dir never reaches walk_errors either.
        dirnames[:] = [d for d in dirnames if d != "tests"]
        for fn in filenames:
            if fn.endswith(".py"):
                py_files.append(Path(dirpath) / fn)

    for exc in walk_errors:
        _log().warning(
            "_compute_core_version: cannot walk %s — %s; subtree excluded from the "
            "version hash (unreadable dir is NOT the same as 'no source files here')",
            getattr(exc, "filename", pkg_dir),
            exc,
        )

    for py_file in sorted(py_files):
        try:
            hasher.update(py_file.read_bytes())
        except OSError as exc:
            # Best-effort: an unreadable source file is excluded from the hash rather
            # than failing version detection outright. Rare (permissions/race), so
            # this is worth a log line when it does happen.
            _log().warning("_compute_core_version: skipping unreadable %s: %s", py_file, exc)
    return hasher.hexdigest()


# ---------------------------------------------------------------------------
# Sentinel read helpers — query-only; no daemon writes these post-C3
# ---------------------------------------------------------------------------

def read_global_running_version() -> Optional[str]:
    """Read the version sentinel from the global service sentinel directory.

    Returns None if the sentinel is absent, empty, or unreadable.
    Authoritative version source for the single global multiplex service (C1a/AC-4).

    Spec backlink: docs/plans/2026-07-04-coordinator-core-global-multiplex-migration.md § C5
    """
    vf = global_sentinel_dir() / _FILE_VERSION
    try:
        return vf.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def read_running_version(repo_root: Optional[Path] = None) -> Optional[str]:
    """Read the version sentinel of the running service; return None if absent.

    Deprecated: repo_root is ignored in the global multiplex model (C1a/C5).
    Delegates to read_global_running_version(). Kept for backward compat with
    ops/health.py and test callers until those migrate to the global variant.
    """
    return read_global_running_version()


def is_version_current(repo_root: Optional[Path] = None) -> bool:
    """Return True iff the running service version matches the current source hash.

    repo_root is ignored in the global multiplex model (C1a/C5). Kept as an
    optional arg for backward compat with ops/health.py and test callers.
    """
    running = read_global_running_version()
    if running is None:
        return False
    return running == _compute_core_version()


# ---------------------------------------------------------------------------
# Tombstone stubs — importable but non-functional.
#
# C5 completed (DR-215): ipc.py no longer imports daemon-lifecycle symbols.
# Remaining live consumer: ops/health.py imports read_endpoint and uds_socket_path.
# Stubs return the correct post-daemon semantics (None / NotImplementedError)
# without requiring changes outside this file.
# ---------------------------------------------------------------------------

def global_socket_path() -> Path:
    """Retired by C3 — UDS transport removed. Raises NotImplementedError."""
    raise NotImplementedError(
        "global_socket_path: daemon transport retired (C3). "
        "coordinator_core is now a command-type engine; no socket path exists."
    )


def uds_socket_path(repo_root: Path) -> Path:  # noqa: ARG001
    """Retired by C3 — UDS transport removed. Raises NotImplementedError."""
    raise NotImplementedError(
        "uds_socket_path: daemon transport retired (C3). "
        "coordinator_core is now a command-type engine; no socket path exists."
    )


def sweep_orphaned_sockets(uid: Optional[int] = None) -> None:  # noqa: ARG001
    """Retired by C3 — no-op (no UDS sockets to sweep)."""


def read_endpoint(repo_root: Optional[Path] = None) -> Optional[str]:  # noqa: ARG001
    """Retired by C3 — always returns None (no daemon endpoint exists).

    Kept for backward compat with ops/health.py. Post-daemon semantics:
    the endpoint is always absent.
    """
    return None


def _mark_partition_active(repo_root: Optional[Path]) -> None:  # noqa: ARG001
    """Retired by C3 — no-op (no partition registry)."""


def arm_drain_flag() -> bool:
    """Retired by C3 — returns False (no drain state)."""
    return False


def get_in_flight_count() -> int:
    """Retired by C3 — returns 0 (no in-flight counter)."""
    return 0


def get_or_create_partition(repo_root: Path) -> dict:  # noqa: ARG001
    """Retired by C3 — raises NotImplementedError (no partition registry)."""
    raise NotImplementedError(
        "get_or_create_partition: daemon partition registry retired (C3)."
    )


def in_flight_decrement() -> None:
    """Retired by C3 — no-op (no in-flight counter)."""


def in_flight_increment() -> None:
    """Retired by C3 — no-op (no in-flight counter)."""


def is_draining() -> bool:
    """Retired by C3 — returns False (no drain state)."""
    return False


# write_sentinels removed by DR-215 strip (2026-07-06).
# Its only consumer (test_version_drift.py) was deleted by C4.
