"""Fleet Temp layout resolver: every path under system Temp that coordinator owns.

Stdlib-only. Paths are built from ``tempfile.gettempdir()`` at call time, never a
literal ``%TEMP%`` or ``/tmp``, so a monkeypatched ``gettempdir`` is honored.
"""

from __future__ import annotations

import atexit
import contextlib
import functools
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


def _legacy_coordinator_root() -> Path:
    """``<gettempdir()>/coordinator``: where every pre-disk-parent coordinator tree lives."""
    return Path(tempfile.gettempdir()) / _ROOT_DIRNAME


def _coordinator_root() -> Path:
    """``<disk-backed parent of gettempdir()>/coordinator``: the one root every layout path derives from.

    Identical to ``_legacy_coordinator_root`` except on a Linux tmpfs temp dir (see ``_disk_backed_parent``).
    """
    return _disk_backed_parent(Path(tempfile.gettempdir())) / _ROOT_DIRNAME


def coordinator_temp_root(repo_root: Path | str) -> Path:
    """``<gettempdir()>/coordinator/<repo dir name>/``; ValueError for a repo named ``_fleet``."""
    name = Path(repo_root).name
    if not name:
        raise ValueError(f"repo_root has no directory name: {repo_root!r}")
    if name == FLEET_DIRNAME:
        raise ValueError(f"repo directory name {FLEET_DIRNAME!r} is reserved for host-scoped Temp")
    return _coordinator_root() / name


def fleet_temp_root() -> Path:
    """``<gettempdir()>/coordinator/_fleet/``: host-scoped, never a purge root.

    Inside a managed session dir (Temp redirected under ``coordinator/_fleet/run-temp/``) it is
    the enclosing real fleet root, so no nested ``coordinator/_fleet`` is created in the session dir.
    """
    return _managed_run_dir_fleet_root() or _coordinator_root() / FLEET_DIRNAME


def _inside_worktree(path: Path) -> bool:
    """True when ``path`` or an ancestor holds a ``.git`` entry: a few stats, no spawn."""
    return any((p / ".git").exists() for p in (path, *path.parents))


def _system_temp_dir() -> Path:
    """The OS temp dir, never one inside a git worktree.

    A Codex child gets TEMP/TMP=``<repo>/scratch/codex-tmp``, so ``gettempdir()`` there is inside the
    repo and pytest refuses (exit 4, zero tests) a basetemp under it. Candidates are the platform
    default, not ``TMPDIR``/``TEMP``, which are the redirect. TRAP: when every candidate is inside a
    worktree or missing, ``gettempdir()`` is returned as-is rather than inventing a location.
    """
    current = Path(tempfile.gettempdir())
    if not _inside_worktree(current):
        return current
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        candidates = [Path(local) / "Temp"]
    else:
        candidates = [Path("/tmp"), Path("/var/tmp")]
    for cand in candidates:
        if cand.is_dir() and not _inside_worktree(cand):
            return cand
    return current


_MOUNTINFO = "/proc/self/mountinfo"


def _unescape_mount(field: str) -> str:
    """mountinfo writes a space in a mountpoint as the octal escape 040; decode those."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


@functools.lru_cache(maxsize=1)
def _mount_table() -> tuple[tuple[str, str], ...]:
    """``(mountpoint, fstype)`` pairs from one read of ``/proc/self/mountinfo``; empty when unreadable.

    Cached for the process: a mount appearing mid-run is not seen, which the per-run basetemp
    choice tolerates. Tests clear it with ``_mount_table.cache_clear()``.
    """
    try:
        with open(_MOUNTINFO, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return ()
    table = []
    for line in lines:
        head, sep, tail = line.partition(" - ")
        fields = head.split()
        if sep and len(fields) >= 5 and tail.split():
            table.append((_unescape_mount(fields[4]), tail.split()[0]))
    return tuple(table)


def _real(path: Path) -> str:
    return os.path.realpath(path)


def _is_tmpfs(path: Path) -> bool:
    """True when the longest-prefix mount holding ``path`` is tmpfs (RAM-backed). No spawn."""
    target = _real(path)
    best_len, best_type = -1, ""
    for mount, fstype in _mount_table():
        prefix = mount.rstrip("/")
        if (target == mount or target.startswith(prefix + "/")) and len(prefix) > best_len:
            best_len, best_type = len(prefix), fstype
    return best_type == "tmpfs"


def _disk_backed_dir(cand: Path) -> bool:
    """Writable (or creatable under a writable parent), outside every worktree, and not tmpfs."""
    if cand.exists():
        if not cand.is_dir() or not os.access(cand, os.W_OK):
            return False
    elif not os.access(cand.parent, os.W_OK):
        return False
    return not _inside_worktree(cand) and not _is_tmpfs(cand)


def _linux_disk_fallbacks() -> list[Path]:
    xdg = os.environ.get("XDG_CACHE_HOME")
    return [Path("/var/tmp"), Path(xdg) if xdg else Path.home() / ".cache"]


def _disk_backed_parent(base: Path) -> Path:
    """``base``, except on Linux a tmpfs one yields to disk-backed ``/var/tmp`` then
    ``$XDG_CACHE_HOME`` (default ``~/.cache``).

    Purpose: scratch under a RAM-backed /tmp (basetemp, clean mirrors, run-temp) fills it (ENOSPC)
    and spends the RAM the admission gate protects. TRAP: with no qualifying fallback the tmpfs dir
    is kept, never invented; off Linux the answer is ``base`` with no filesystem touch.
    """
    if not sys.platform.startswith("linux") or not _is_tmpfs(base):
        return base
    for cand in _linux_disk_fallbacks():
        if _disk_backed_dir(cand):
            return cand
    return base


def _pytest_temp_parent() -> Path:
    """``_disk_backed_parent`` of the worktree-safe system temp dir (see ``_system_temp_dir``)."""
    return _disk_backed_parent(_system_temp_dir())


def pytest_basetemp_root(repo_root: Path | str) -> Path:
    """``<pytest temp parent>/coordinator/<repo>/pytest/``: parent of per-run pytest basetemps.

    Outside every worktree even when ``gettempdir()`` is redirected into one (see ``_system_temp_dir``),
    and off tmpfs on Linux when a disk-backed fallback exists (see ``_pytest_temp_parent``).
    """
    return _pytest_temp_parent() / _ROOT_DIRNAME / coordinator_temp_root(repo_root).name / "pytest"


def pytest_fleet_basetemp_root() -> Path:
    """``<pytest temp parent>/coordinator/_fleet/pytest/``: basetemp parent when no repo name is usable."""
    return _pytest_temp_parent() / _ROOT_DIRNAME / FLEET_DIRNAME / "pytest"


def pytest_sweep_roots() -> list[Path]:
    """Every ``<base>/coordinator`` a per-run pytest dir may live under, deduplicated.

    The dead-owner sweep walks all of them: the basetemp root moves between the system temp and a
    Linux disk fallback as tmpfs state and env change, so a dir from an earlier choice must still reap.
    """
    bases = [
        Path(tempfile.gettempdir()),
        _system_temp_dir(),
        _pytest_temp_parent(),
        _coordinator_root().parent,
    ]
    if sys.platform.startswith("linux"):
        bases += _linux_disk_fallbacks()
    roots: list[Path] = []
    for base in bases:
        root = base / _ROOT_DIRNAME
        if root not in roots:
            roots.append(root)
    return roots


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


def scratch_root(repo_root: Path | str | None = None) -> Path:
    """The one engine scratch resolver: machine-local ``paths.scratch_root`` (not created), else the managed Temp root.

    The fallback is `managed_tempdir`'s root, never a drive literal. `machine_resolver` is
    imported per call so this module stays stdlib-only at import.
    """
    from coordinator_core import machine_resolver

    configured = machine_resolver.registry_get("paths.scratch_root")
    if configured:
        return Path(configured)
    return coordinator_temp_root(repo_root) if repo_root is not None else fleet_temp_root()


def build_root() -> Path:
    """Machine-local ``paths.build_root`` (not created) for self-replacing outputs; unset falls back to `scratch_root()` with a warning."""
    import warnings

    from coordinator_core import machine_resolver

    configured = machine_resolver.registry_get("paths.build_root")
    if configured:
        return Path(configured)
    warnings.warn("paths.build_root is unset; build outputs fall back to paths.scratch_root", stacklevel=2)
    return scratch_root()


def build_output_dir(repo_root: Path | str, output: str) -> Path:
    """``<build_root>/<repo dir name>/<output>``: a path only, never created or deleted.

    invariant: a new output replaces the prior one in place and its maker leaves it after use.
    ``output`` is one path segment; ValueError otherwise.
    """
    repo = Path(repo_root).name
    if not repo:
        raise ValueError(f"repo_root has no directory name: {repo_root!r}")
    if not output or output in (".", "..") or "/" in output or "\\" in output:
        raise ValueError(f"output must be one path segment: {output!r}")
    return build_root() / repo / output


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


# Rebuildable caches under the old tmpfs root, never copied: (path under ``coordinator/``, max age s).
# bug-blitz keeps prune_claude_temp's 7-day retention; the mirrors and commit clones rebuild cheaply.
_LEGACY_CACHE_PATHS = (
    (("clean-mirror",), 86400),
    ((FLEET_DIRNAME, "claude-klabauter-commit-clone"), 86400),
    ((FLEET_DIRNAME, "coordinator-bug-blitz"), 7 * 86400),
)
_legacy_root_reaped = False


def _commit_clone_leased(clone: Path) -> bool:
    """True when ``<clone>/.git/in-use/`` names a live pid (mirrors commit_clone._clone_in_use; no import)."""
    from coordinator_core.group_em import session_registry

    try:
        names = os.listdir(clone / ".git" / "in-use")
    except OSError:
        return False
    return any(
        n.isdigit() and (int(n) == os.getpid() or session_registry.pid_alive(int(n))) for n in names
    )


def _reap_legacy_root_once() -> None:
    """Once per process, drain the old ``<gettempdir()>/coordinator`` when the root moved off it.

    Cost when idle: one stat (root unchanged, or the old root absent). Removes dead-owner run-temp
    entries (a live owner's dir is left for a later pass) and stale rebuildable caches; never copies. Old pytest dirs reap via
    ``pytest_sweep_roots``, which still walks this root.
    TRAP: only the named cache dirs age out, so sentinels and stores a pre-move process still reads stay.
    """
    global _legacy_root_reaped
    if _legacy_root_reaped:
        return
    _legacy_root_reaped = True
    old = _legacy_coordinator_root()
    if old == _coordinator_root():
        return
    try:
        children = [c for c in os.scandir(old) if c.is_dir(follow_symlinks=False)]
    except OSError:
        return
    now = time.time()
    for child in children:
        base = Path(child.path)
        _reap_dead_owner_dirs(base / RUN_TEMP_DIRNAME)
        for parts, max_age in _LEGACY_CACHE_PATHS:
            if parts[0] == FLEET_DIRNAME and child.name != FLEET_DIRNAME:
                continue
            cache = base.joinpath(*(parts[1:] if parts[0] == FLEET_DIRNAME else parts))
            try:
                entries = list(os.scandir(cache))
            except OSError:
                continue
            for entry in entries:
                try:
                    if now - entry.stat(follow_symlinks=False).st_mtime < max_age:
                        continue
                    if parts[-1] == "claude-klabauter-commit-clone" and _commit_clone_leased(Path(entry.path)):
                        continue
                    _rmtree_strict(Path(entry.path))
                except OSError:
                    pass


def _managed_run_dir_fleet_root() -> Path | None:
    """The ``coordinator/_fleet`` enclosing ``gettempdir()`` when it is under ``run-temp/``, else None."""
    parts = Path(tempfile.gettempdir()).parts
    want = (_ROOT_DIRNAME, FLEET_DIRNAME, RUN_TEMP_DIRNAME)
    for i in range(len(parts) - 2):
        if parts[i : i + 3] == want:
            return Path(*parts[: i + 2])
    return None


def _temp_is_managed_run_dir() -> bool:
    """True when ``gettempdir()`` is already a dir under ``coordinator/_fleet/run-temp/``."""
    return _managed_run_dir_fleet_root() is not None


def _create(
    prefix: str, repo_root: Path | str | None, owner_tag: str
) -> tuple[Path, Callable[[], None]]:
    if _temp_is_managed_run_dir():
        # A session dir redirected Temp into the managed layout: nesting the skeleton again
        # only eats MAX_PATH headroom, and the session dir's own teardown owns the contents.
        run_temp = Path(tempfile.gettempdir())
    else:
        run_temp = managed_tempdir(repo_root) / RUN_TEMP_DIRNAME
        run_temp.mkdir(parents=True, exist_ok=True)
        _reap_dead_owner_dirs(run_temp)
        _reap_legacy_root_once()
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
    while True:
        path = Path(parent) / f"{prefix}{secrets.token_hex(4)}"
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            continue
        return path


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
