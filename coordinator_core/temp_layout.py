"""Fleet Temp layout resolver: every path under system Temp that coordinator owns.

Stdlib-only. Paths are built from ``tempfile.gettempdir()`` at call time, never a
literal ``%TEMP%`` or ``/tmp``, so a monkeypatched ``gettempdir`` is honored.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import re
import secrets
import shutil
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from fnmatch import fnmatchcase
from pathlib import Path

FLEET_DIRNAME = "_fleet"
PYTEST_KEPT_MARKER = ".coordinator-kept-failing"
_PYTEST_RUN_DIR_RE = re.compile(r"^(\d+)-[0-9a-f]{6}$")
_ROOT_DIRNAME = "coordinator"


def _coordinator_root() -> Path:
    return Path(tempfile.gettempdir()) / _ROOT_DIRNAME


def coordinator_temp_root(repo_root: Path | str) -> Path:
    """``<gettempdir()>/coordinator/<repo dir name>/``; ValueError for a repo named ``_fleet``."""
    name = Path(repo_root).name
    if not name:
        raise ValueError(f"repo_root has no directory name: {repo_root!r}")
    if name == FLEET_DIRNAME:
        raise ValueError(f"repo directory name {FLEET_DIRNAME!r} is reserved for host-scoped Temp")
    return _coordinator_root() / name


def fleet_temp_root() -> Path:
    """``<gettempdir()>/coordinator/_fleet/``: host-scoped, never a purge root."""
    return _coordinator_root() / FLEET_DIRNAME


def pytest_basetemp_root(repo_root: Path | str) -> Path:
    """``coordinator_temp_root(repo_root)/pytest/``: parent of per-run pytest basetemps."""
    return coordinator_temp_root(repo_root) / "pytest"


RUN_TEMP_DIRNAME = "run-temp"

# Top-level Temp name shapes coordinator wrote before the managed layout. Each is checked
# against its producer; ``coordinator-probe-spray-*`` has no live producer (deleted) and stays
# so leftovers from older runs are still recognized.
#
# TRAP: these delete third-party entries on every install, so a generic prefix is pinned to
# our producer's suffix shape (pid digits, ``.json``), never a bare ``<word>-*``.
LEGACY_TOP_LEVEL_GLOBS: tuple[str, ...] = (
    "git-pathspec-[0-9]*.txt",
    "git-index-[0-9]*",
    "coordinator-pending-dispatch-*",
    "coordinator-nudged-*",
    "coordinator-dispatch-nudge-*",
    "coordinator-guard-unlock-*",
    "coordinator-probe-spray-*",
    "advisory-hook-state-*",
    "first-agent-dispatch-advisory-*",
    "rt-bark-once-*",
    "group-em-watch-*",
    "workflow-run-*.json",
    "em-poll-count-*",
    "hand-written-plan-nudge-*",
    "autonomous-run-*",
    "pickup-autofire-events.jsonl",
    "coordinator-bt-python3-invocation-cache.json",
    "coordinator-gitless-advisory-dedupe",
    "coordinator-fleet-no-repo-root",
    "backfill-initiative-fk.lock",
    "coordinator-falsifier",
    "bash-dispatch-probe-root",
    # Test and audit-probe leftovers from our own code; each has a producer in claude-klabauter,
    # coordinator-content-repo or example-store-repo (the producer is named in the commit message).
    "altlive-*",
    "sgc-cold-*",
    "fmhook-test-*",
    "fmhook-lineage-test-*",
    "runtime-tripwire-em-dispatch-default-cursor-*",
    "land_pull_test_*",
    "c[0-9]-census-fixture-*",
    "hook-census-fixture-*",
    "claude-klabauter-throwaway-tree-*",
    "claude-klabauter-publish-materialize-*",
)


def managed_tempdir(repo_root: Path | str | None = None) -> Path:
    """Created managed Temp dir: the repo's root when given a repo, else the fleet root."""
    root = coordinator_temp_root(repo_root) if repo_root is not None else fleet_temp_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


_MANAGED_DIR_RE = re.compile(r"^(\d+|kept)-[0-9a-f]{6}-.+$")
# Dead-owner entries reaped per creation: bounds the synchronous rmtree a caller can pay.
_REAP_CAP = 8
# A ``kept-`` entry has no owner pid, so age is its only reap signal; run-temp itself is never
# aged out by the Temp pruner (its mtime moves with every creation).
_KEPT_MAX_AGE_S = 7 * 86400


def _clear_readonly_and_retry(func, path, _exc) -> None:
    """rmtree error hook: a Windows read-only entry is made writable and the op retried once."""
    os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
    func(path)


def _rmtree_strict(path: Path) -> None:
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, ignore_errors=False, onexc=_clear_readonly_and_retry)
    else:
        shutil.rmtree(path, ignore_errors=False, onerror=_clear_readonly_and_retry)


def _reap_dead_owner_dirs(run_temp: Path) -> None:
    """Remove ``<pid>-<6hex>-<prefix>`` entries whose pid is dead: one listdir, no walk.

    Best-effort per entry: a peer reaping the same entry or a held handle never fails the
    caller's own creation.
    """
    from coordinator_core.group_em import session_registry

    try:
        names = os.listdir(run_temp)
    except OSError:
        return
    reaped = 0
    for name in names:
        if reaped >= _REAP_CAP:
            return
        m = _MANAGED_DIR_RE.fullmatch(name)
        if m is None:
            continue
        if m.group(1) == "kept":
            try:
                if time.time() - (run_temp / name).stat().st_mtime < _KEPT_MAX_AGE_S:
                    continue
            except OSError:
                continue
        else:
            pid = int(m.group(1))
            if pid == os.getpid() or session_registry.pid_alive(pid):
                continue
        reaped += 1
        try:
            _rmtree_strict(run_temp / name)
        except OSError:
            pass


def _temp_is_managed_run_dir() -> bool:
    """True when ``gettempdir()`` is already a dir under ``coordinator/_fleet/run-temp/``."""
    parts = _coordinator_root().parent.parts
    want = (_ROOT_DIRNAME, FLEET_DIRNAME, RUN_TEMP_DIRNAME)
    return any(parts[i : i + 3] == want for i in range(len(parts) - 2))


def _create(
    prefix: str, repo_root: Path | str | None, owner_tag: str
) -> tuple[Path, Callable[[], None]]:
    if _temp_is_managed_run_dir():
        # A session dir redirected Temp into the managed layout: nesting the skeleton again
        # only eats MAX_PATH headroom, and the session dir's own teardown owns the contents.
        run_temp = _coordinator_root().parent
    else:
        run_temp = managed_tempdir(repo_root) / RUN_TEMP_DIRNAME
        run_temp.mkdir(parents=True, exist_ok=True)
        _reap_dead_owner_dirs(run_temp)
    path = run_temp / f"{owner_tag}-{secrets.token_hex(3)}-{prefix}"
    path.mkdir()

    def cleanup() -> None:
        if path.exists():
            _rmtree_strict(path)

    return path, cleanup


def create_managed_temp_dir(
    prefix: str, repo_root: Path | str | None = None
) -> tuple[Path, Callable[[], None]]:
    """Create ``run-temp/kept-<6hex>-<prefix>`` and return ``(path, cleanup)``; no atexit hook.

    For a caller that keeps the dir past process exit: the ``kept-`` name carries no pid, so
    the dead-owner reap spares it until it is older than ``_KEPT_MAX_AGE_S``.
    """
    return _create(prefix, repo_root, "kept")


def make_managed_temp_dir(prefix: str, repo_root: Path | str | None = None) -> Path:
    """Create ``<managed_tempdir>/run-temp/<pid>-<6hex>-<prefix>`` and remove it at interpreter exit.

    For a caller that hands the path across a boundary. A killed process skips atexit; its dir
    is reaped by the next creation once its pid is dead.
    """
    path, cleanup = _create(prefix, repo_root, str(os.getpid()))
    atexit.register(cleanup)
    return path


@contextlib.contextmanager
def managed_temp_dir(
    prefix: str, repo_root: Path | str | None = None, *, strict: bool = False
) -> Iterator[Path]:
    """Context manager yielding a fresh managed dir that is removed on exit, error or not.

    A teardown OSError is swallowed (the leaked dir is reaped once this pid is dead). With
    ``strict=True`` it re-raises as an OSError naming the leaked path; when the body raised, the
    body's exception propagates and carries the teardown failure as a note.
    """
    path, cleanup = _create(prefix, repo_root, str(os.getpid()))
    try:
        yield path
    except BaseException as body_exc:
        try:
            cleanup()
        except OSError as td_exc:
            if strict:
                body_exc.add_note(f"managed temp dir teardown failed, leaked {path}: {td_exc}")
        raise
    else:
        try:
            cleanup()
        except OSError as td_exc:
            if strict:
                raise OSError(
                    td_exc.errno, f"managed temp dir teardown failed, leaked {path}: {td_exc}"
                ) from td_exc


def mkdtemp_in(prefix: str, parent: Path | str | None) -> Path:
    """Create ``<parent>/<prefix><random>`` and return it; the caller removes it.

    For a caller that needs a chosen parent (a short socket-safe root, a repo scratch dir)
    rather than the managed layout. ``None`` creates a pid-stamped dir under the managed
    run-temp root, removed at interpreter exit (never bare system Temp).
    """
    if parent is None:
        return make_managed_temp_dir(prefix)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=str(parent)))


@contextlib.contextmanager
def temp_dir_in(prefix: str, parent: Path | str | None) -> Iterator[Path]:
    """``mkdtemp_in`` removed on exit; a teardown OSError never replaces the body's outcome."""
    path = mkdtemp_in(prefix, parent)
    try:
        yield path
    finally:
        try:
            _rmtree_strict(path)
        except OSError:
            pass


def legacy_top_level_path(name: str) -> Path:
    """``<gettempdir()>/<name>``: the sanctioned read of a pre-managed-layout location."""
    return Path(tempfile.gettempdir()) / name


def is_legacy_top_level(name: str) -> bool:
    """True when a top-level Temp entry name matches a shape coordinator used to write."""
    return any(fnmatchcase(name, g) for g in LEGACY_TOP_LEVEL_GLOBS)


def parse_pytest_run_dir(name: str) -> int | None:
    """Owning pid of a ``<pid>-<6 hex>`` per-run basetemp dir name, else None."""
    m = _PYTEST_RUN_DIR_RE.fullmatch(name)
    return int(m.group(1)) if m else None
