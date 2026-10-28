"""coordinator_core.benchmarks.isolated_clone -- where a benchmark fixture's
throwaway warm-engine clone is PUT, and how it is taken back down.

WHERE -- `mkdtemp_for_clone`, and the two constraints that pin it. These
clones are built by `os.link` (see `_hardlink_coordinator_core`), so the
destination must sit on the SAME VOLUME as the engine root: the platform temp
directory is routinely a different drive here and is simply not available.
The original sites answered that with `dir=source_root.parent`, which for a
repo checked out at a volume's top level is the BARE DRIVE ROOT -- same
volume, and outside every git repo,
so `bash_guards.bump_outside_repo_write` (whose whole predicate is "target
resolves under NO git root") could never see it even in principle, and neither
could anything else. `mkdtemp_for_clone` keeps the volume and drops the
rootlessness by landing under `<engine_root>/scratch/`, which is gitignored at
any depth, outside `pyproject.toml`'s `testpaths`, and -- the point -- under a
git root, where write confinement can reason about it.

WHY THIS EXISTS -- THE OBSERVED LEAK. A fixture that boots
`python -m coordinator_core.warm.server` inside an isolated clone does NOT own
exactly one process. `server.py`'s boot sequence starts a daemon thread that
calls `supervisor.ensure_listener(repo_root)`, which spawns the http listener
DETACHED (`ops.ceremony.detached_spawn`) -- deliberately outliving its invoker,
because that is what a production listener must do. A teardown that terminates
only the breadcrumb pid therefore stops the server and leaves the supervisor
running, rooted in the clone, holding its file handles open forever.

The second half of the same defect is what made it invisible for so long:
`shutil.rmtree(tmp_parent, ignore_errors=True)` cannot delete a tree a live
process holds open on Windows, and `ignore_errors` swallows the
`PermissionError` without a word. The fixture reports a clean teardown, the
clone stays on disk, and the supervisor stays resident. Observed on the normal
tier: 37 orphaned supervisors across 37 surviving clones, ~14GB, accumulated
over two days of gate runs -- 37 idle interpreters on a box whose load norm is
50-70 concurrent LLMs (`CLAUDE.md` s Load norm; "the load is us").

NO SUPERVISOR IS SPAWNED IN A CLONE NOW. `engine_env` sets
`COORDINATOR_WARM_LISTENER_AUTOSTART=0`, so `ensure_listener` never spawns the
detached listener there, and `spawn_clone_server` holds the server in a
kill-on-close job (Windows), so the server and its pool children die with the
test process even when no `finally` runs. The paragraph above is the history
that motivates both.

POSTURE -- DISCOVER BY ROOT, SIGNAL BY DESCENT. `reap_processes_under` finds
holders by where a process is ROOTED (cmdline or cwd resolving under the
clone): a double-forked, reparented grandchild is no longer a child of anything
this fixture can enumerate, yet still runs out of a directory unique to this
run, so only a root predicate sees it. But it SIGNALS only descendants of
`os.getpid()`: the suite's foreign-kill tripwire (`conftest.py`) refuses a
signal to anything outside the test's tree, and a detached daemon is exactly
that. Root-matched non-descendants are returned on `.foreign` and left alone;
`rmtree_or_raise` names them if they pin the clone. `fresh_git_clone` prevents
the common case at the source (`core.fsmonitor=false`, `gc.auto=0`,
`maintenance.auto=false`), and `_reap_stale_clones` never signals a stale
clone's holders (none are descendants) and leaves a pinned dir for a later attempt.

SAFE BY THE UNIQUENESS OF `mkdtemp`. Both functions take a root that is a
freshly `mkdtemp`'d path unique to THIS run, so nothing outside this run's own
processes can resolve under it -- the same construction argument the
`breadcrumb.svc_dir` removal in these fixtures already relies on. Never point
either function at a real engine root.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

_TERMINATE_TIMEOUT_SECS = 5.0
_KILL_TIMEOUT_SECS = 3.0

_SCRATCH_RELPATH = ("scratch", "benchmark-clones")


class RootlessCloneDestination(RuntimeError):
    pass


def _assert_under_git_root(destination: Path) -> None:
    """Raise unless some ancestor of `destination` contains `.git`.

    Walks the RESOLVED path, so a symlink into a rootless location is caught
    rather than credited to the link's own parentage. Accepts `.git` as either
    a directory or a file (a worktree or submodule checkout writes the latter),
    since both mean "a git root is above this".

    Deliberately does NOT shell out to `git rev-parse`: process creation is the
    cost on this box (`CLAUDE.md` -- `git --version` alone is 25.3ms), and this
    fires on a path that has not been created yet, where `rev-parse` would need
    an existing cwd to run in. A `Path` walk is a handful of `stat` calls.
    """
    resolved = Path(os.path.realpath(str(destination)))
    for ancestor in (resolved, *resolved.parents):
        if (ancestor / ".git").exists():
            return
    raise RootlessCloneDestination(
        f"clone destination resolves under no git root: {resolved} -- refusing to "
        f"mint it. Pass a source_root inside a repository; a destination outside "
        f"every repo is invisible to write confinement and to every sweep."
    )


_STALE_CLONE_AGE_SECS = 3600.0


def _reap_stale_clones(scratch: Path) -> None:
    """Remove clone directories left behind by earlier runs.

    SELF-LIMITING BY CONSTRUCTION, and that is the point. `scratch/` is
    gitignored at any depth, so nothing accumulating here shows up in
    `git status`, in a review, or anywhere a human would notice -- the same
    invisibility that let 68 directories and ~14GB build up on the drive root
    for two days. Moving the clones inside the repo fixed WHERE they land; it
    did not, on its own, fix that nobody is watching. This does: the bound is
    enforced every time a clone is minted, by the same helper that mints it,
    so it cannot drift out of use the way a one-shot census or a scheduled
    sweep can.

    Best-effort throughout: a clone another process still holds open simply
    survives to the next attempt (its own teardown's `reap_processes_under`
    is what frees it). This runs on the mint path, so it must never raise and
    never block -- a reaper that can fail a benchmark is worse than the
    residue it removes.
    """
    try:
        entries = list(scratch.iterdir())
    except OSError:
        return
    now = time.time()
    for entry in entries:
        try:
            if not entry.is_dir():
                continue
            if now - entry.stat().st_mtime < _STALE_CLONE_AGE_SECS:
                continue
        except OSError:
            continue
        reap_processes_under(entry)
        _rmtree_clearing_readonly(entry)


def _rmtree_clearing_readonly(root: Path) -> None:
    """`shutil.rmtree` that clears the read-only bit and retries once per entry.

    Git writes loose objects read-only, and Windows refuses to unlink a
    read-only file; a bare `rmtree(ignore_errors=True)` then leaves the clone
    on disk with no live holder, where every repo-walking test globs it and
    trips on it vanishing mid-run. Anything still failing after the chmod
    retry is left for `rmtree_or_raise` to report.
    """

    def _retry(func, path, _exc):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except OSError:
            pass

    if sys.version_info >= (3, 12):
        shutil.rmtree(root, onexc=_retry)
    else:
        shutil.rmtree(root, onerror=_retry)


def mkdtemp_for_clone(source_root: Path, *, prefix: str) -> Path:
    scratch = source_root.joinpath(*_SCRATCH_RELPATH)
    _assert_under_git_root(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    _reap_stale_clones(scratch)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=str(scratch)))


_SHORT_PREFIX_MAX = 6


def _short_clone_prefix(prefix: str) -> str:
    """Clip a caller's descriptive prefix: the clone's deepest repo paths must fit Windows MAX_PATH."""
    return prefix[:_SHORT_PREFIX_MAX].rstrip("-") + "-"


@contextlib.contextmanager
def fresh_git_clone(source_root: Path, *, prefix: str, label: str) -> Iterator[Path]:
    """Yield the path of a throwaway clone of `source_root`'s HEAD; take it back down on exit.

    The clone borrows `source_root`'s objects (`--shared`, no object copy), so the source must
    outlive it; `remote=True` only lifts the seam's ceiling to fit a full-tree checkout. Teardown reaps processes rooted
    in the clone before removing it, and raises `CloneTeardownLeak` if anything survives.
    """
    from coordinator_core.git.run import run_git

    tmp = mkdtemp_for_clone(source_root, prefix=_short_clone_prefix(prefix))
    try:
        dest = tmp / "c"
        result = run_git(
            [
                "-c", "core.fsmonitor=false", "-c", "core.longpaths=true",
                "clone", "-q", "--shared", str(source_root), str(dest),
            ],
            cwd=str(tmp),
            remote=True,
        )
        if not result.ok:
            raise RuntimeError(
                f"{label}: git clone of {source_root} failed rc={result.returncode}: "
                f"{result.stderr.strip()}"
            )
        _silence_clone_git_daemons(dest)
        yield dest
    finally:
        reaped = reap_processes_under(tmp)
        rmtree_or_raise(tmp, label=label, reaped=reaped)


def _normalized(path: Path) -> str:
    resolved = os.path.realpath(str(path))
    return resolved.casefold() if sys.platform in ("win32", "darwin") else resolved


def _process_is_rooted_under(proc, root_norm: str) -> bool:
    import psutil

    try:
        cmdline = " ".join(proc.cmdline() or ())
    except (psutil.Error, OSError):
        cmdline = ""
    if cmdline:
        probe = cmdline.casefold() if sys.platform in ("win32", "darwin") else cmdline
        idx = probe.find(root_norm)
        while idx != -1:
            end = idx + len(root_norm)
            if end == len(probe) or probe[end] in (os.sep, "/"):
                return True
            idx = probe.find(root_norm, idx + 1)
    try:
        cwd = proc.cwd()
    except (psutil.Error, OSError):
        return False
    if not cwd:
        return False
    try:
        probe = _normalized(Path(cwd))
    except OSError:
        return False
    return probe == root_norm or probe.startswith(root_norm + os.sep)


class Reaped(list):
    """Pids signalled by `reap_processes_under`; `.foreign` holds the root-matched pids that
    were NOT in the caller's process tree and were therefore left running."""

    foreign: List[int]

    def __init__(self, signalled=(), foreign=()):
        super().__init__(signalled)
        self.foreign = list(foreign)


_CLONE_GIT_CONFIG = (
    "\n[core]\n\tfsmonitor = false\n[gc]\n\tauto = 0\n[maintenance]\n\tauto = false\n"
)


def _silence_clone_git_daemons(dest: Path) -> None:
    """Append the no-daemon settings to the clone's config (in-process; no git spawn).

    A detached `fsmonitor--daemon` or auto-gc/maintenance process is outside the test's process
    tree, so the suite's foreign-kill tripwire refuses to let teardown signal it.
    """
    with (dest / ".git" / "config").open("a", encoding="utf-8") as fh:
        fh.write(_CLONE_GIT_CONFIG)


def reap_processes_under(root: Path) -> List[int]:
    """Terminate live processes rooted under `root` that descend from this process; return the
    pids signalled, with root-matched non-descendants on the result's `.foreign` (never signalled).

    DISCOVERY is by root (cmdline or cwd under `root`), SIGNALLING only by descent from
    `os.getpid()`: a process outside the tree is another run's or a detached daemon's, and the
    suite's foreign-kill tripwire refuses to let a test signal it. Callers: `fresh_git_clone` and
    `booted_warm_engine` teardowns (their workers are descendants, so reaped as before; anything
    detached is reported, and `rmtree_or_raise` then names it), and `_reap_stale_clones`
    (a stale clone's holders are never descendants, so it signals nothing and leaves a pinned dir
    for a later attempt).

    Escalates `terminate()` to `kill()` for anything still alive after
    `_TERMINATE_TIMEOUT_SECS`. Best-effort per process; never raises.
    """
    import psutil

    root_norm = _normalized(root)
    try:
        tree = {p.pid for p in psutil.Process(os.getpid()).children(recursive=True)}
    except psutil.Error:
        # Fail safe: an unreadable tree signals nothing; every holder is reported on `.foreign`.
        tree = set()
    doomed = []
    foreign: List[int] = []
    for proc in psutil.process_iter():
        if proc.pid == os.getpid():
            continue
        try:
            if _process_is_rooted_under(proc, root_norm):
                if proc.pid in tree:
                    doomed.append(proc)
                else:
                    foreign.append(proc.pid)
        except psutil.Error:
            continue

    for proc in doomed:
        try:
            proc.terminate()
        except (psutil.Error, OSError):
            pass
    _, alive = psutil.wait_procs(doomed, timeout=_TERMINATE_TIMEOUT_SECS)
    for proc in alive:
        try:
            proc.kill()
        except (psutil.Error, OSError):
            pass
    psutil.wait_procs(alive, timeout=_KILL_TIMEOUT_SECS)

    return Reaped([proc.pid for proc in doomed], foreign)


class WarmEngineBootTimeout(RuntimeError):
    pass


def _link_tree(src: Path, dst: Path, *, skip_dirs: tuple = ("__pycache__",)) -> None:
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        out = dst / Path(root).relative_to(src)
        out.mkdir(parents=True, exist_ok=True)
        for name in files:
            os.link(Path(root) / name, out / name)


def clone_engine(source_root: Path, root: Path, *, entrypoint: str) -> None:
    """Hardlink the engine package plus the one CLI script `entrypoint` and its `lib`, which the
    entrypoint resolver reads, into `root`; stamp it so the clone is its own engine identity."""
    _link_tree(source_root / "coordinator_core", root / "coordinator_core")
    bin_src = source_root / "coordinator" / "bin"
    bin_dst = root / "coordinator" / "bin"
    bin_dst.mkdir(parents=True)
    os.link(bin_src / f"{entrypoint}.py", bin_dst / f"{entrypoint}.py")
    _link_tree(bin_src / "lib", bin_dst / "lib")
    (root / "coordinator_core" / "_engine_stamp").write_text(
        f"sha:isolated-clone-{time.time_ns()}\n", encoding="utf-8"
    )


def engine_env(engine_root: Path) -> dict:
    env = dict(os.environ)
    env["COORDINATOR_WARM"] = "1"
    env["COORDINATOR_DOOR_ENGINE_ROOT"] = str(engine_root)
    env["COORDINATOR_WARM_LISTENER_AUTOSTART"] = "0"
    env.pop("VIRTUAL_ENV", None)
    return env


clone_server_env = engine_env


def spawn_clone_server(
    root: Path, env: Optional[dict] = None
) -> Tuple[subprocess.Popen, Optional[object]]:
    """Start `coordinator_core.warm.server` in `root`; return `(proc, job)`.

    On Windows the server is born CREATE_SUSPENDED, assigned to a kill-on-close
    job, then resumed, so pool children spawned at boot are inside the job and
    the whole tree dies with the holder of `job` even when no `finally` runs.
    Pass `job` to `close_clone_job` at teardown; `job` is None off Windows.
    """
    env = engine_env(root) if env is None else env
    args = [sys.executable, "-m", "coordinator_core.warm.server"]
    common = dict(
        cwd=str(root),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    if sys.platform != "win32":
        return subprocess.Popen(args, **common), None

    import ctypes
    from ctypes import wintypes

    from coordinator_core.benchmarks import process_time as pt

    job = pt._windows_create_kill_on_close_job()
    try:
        proc = subprocess.Popen(
            args, creationflags=pt._CREATE_SUSPENDED | pt._CREATE_NO_WINDOW, **common
        )
    except BaseException:
        pt._k32.CloseHandle(wintypes.HANDLE(job))
        raise
    try:
        if not pt._k32.AssignProcessToJobObject(
            wintypes.HANDLE(job), wintypes.HANDLE(int(proc._handle))
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        pt._resume_all_threads(proc.pid)
    except BaseException:
        proc.kill()
        pt._k32.CloseHandle(wintypes.HANDLE(job))
        raise
    return proc, job


def close_clone_job(job: Optional[object]) -> None:
    """Close the kill-on-close job from `spawn_clone_server`, killing anything left in it."""
    if job is None:
        return
    from ctypes import wintypes

    from coordinator_core.benchmarks import process_time as pt

    pt._k32.CloseHandle(wintypes.HANDLE(job))


@contextlib.contextmanager
def booted_warm_engine(
    source_root: Path, *, prefix: str, entrypoint: str, label: str, boot_wait_secs: float = 90.0
) -> Iterator[Tuple[Path, int, dict]]:
    """Yield `(clone_root, server_pid, env)` for a warm engine booted from a hardlink clone of
    `source_root`, with no dispatch fired, so a `LiveTreeAccountant` attached to `server_pid`
    precedes the pool workers (job membership is fixed at process creation). Raises
    `WarmEngineBootTimeout` when no breadcrumb appears; tears the clone down on exit."""
    from coordinator_core.warm import breadcrumb

    tmp_parent = mkdtemp_for_clone(source_root, prefix=prefix)
    root = tmp_parent / "clone"
    proc: Optional[subprocess.Popen] = None
    job = None
    try:
        clone_engine(source_root, root, entrypoint=entrypoint)
        proc, job = spawn_clone_server(root)
        deadline = time.time() + boot_wait_secs
        crumb = None
        while time.time() < deadline:
            crumb = breadcrumb.read_breadcrumb(engine_root=root)
            if crumb and crumb.get("pid"):
                break
            time.sleep(0.2)
        if not (crumb and crumb.get("pid")):
            raise WarmEngineBootTimeout(f"isolated warm engine did not boot within {boot_wait_secs}s")
        yield root, int(crumb["pid"]), engine_env(root)
    finally:
        crumb = breadcrumb.read_breadcrumb(engine_root=root)
        if crumb and crumb.get("pid"):
            try:
                import psutil

                p = psutil.Process(int(crumb["pid"]))
                # Workers are signalled while their parent is alive: an orphan has left the
                # test's process tree and the suite's foreign-kill tripwire refuses it.
                workers = p.children(recursive=True)
                for w in workers:
                    w.terminate()
                p.terminate()
                psutil.wait_procs([p, *workers], timeout=5)
            except Exception:
                pass
        elif proc is not None and proc.poll() is None:
            proc.terminate()
        close_clone_job(job)
        reaped = reap_processes_under(tmp_parent)
        shutil.rmtree(breadcrumb.svc_dir(engine_root=root), ignore_errors=True)
        rmtree_or_raise(tmp_parent, label=label, reaped=reaped)


class CloneTeardownLeak(RuntimeError):
    pass


def rmtree_or_raise(root: Path, *, label: str, reaped: Optional[List[int]] = None) -> bool:
    """Remove `root`; RAISE `CloneTeardownLeak` if anything survives.

    The `ignore_errors=True` this replaces is precisely how a leaking fixture
    reads as a clean one. A survivor now names the path, the processes still
    rooted there, and the pids this teardown already reaped -- everything
    needed to tell "the reaper missed one" from "something outside this run
    holds it open".

    RAISES, AND THE EARLIER NON-BLOCKING BAR WAS WRONG. This first shipped
    emitting `warnings.warn` and returning `bool`, on the argument that a
    teardown failure must not mask an otherwise-valid benchmark verdict. That
    argument does not survive contact with the incident it was written for:
    `pyproject.toml` sets no `filterwarnings`, so the warning lands only in
    pytest's end-of-run summary and the run still reports success -- which is
    the SAME observable outcome as the `ignore_errors=True` this function
    replaced, differing only in whether a human scrolling the summary might
    notice. A leaked clone means a live orphaned process on a box whose load
    norm is 50-70 concurrent LLMs; that is a defect, and a defect that turns
    nothing red is one nobody fixes for two days (2026-08-27: 68 directories,
    ~14GB, found by eye).

    The masking worry is real but small and separately handled: the raise
    happens in teardown, AFTER the gate's own assertions have already run and
    reported, so a genuine measurement failure still surfaces as itself. The
    mint-path `_reap_stale_clones` bounds residue; this raise is what makes a
    leak impossible to ship unnoticed. Returns True when the tree is gone, so
    the signature stays usable, but the False path no longer exists.
    """
    _rmtree_clearing_readonly(root)
    if not root.exists():
        return True

    import psutil

    root_norm = _normalized(root)
    holders = []
    for proc in psutil.process_iter():
        try:
            if _process_is_rooted_under(proc, root_norm):
                holders.append(f"{proc.pid}:{proc.name()}")
        except psutil.Error:
            continue

    raise CloneTeardownLeak(
        f"{label}: isolated clone survived teardown at {root} -- "
        f"still-rooted processes={holders or 'none found'}, "
        f"reaped_this_teardown={reaped or []}. "
        f"Left on disk; the holder above is what to investigate."
    )
