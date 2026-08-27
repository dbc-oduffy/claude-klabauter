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

POSTURE -- REAP BY ROOT, NOT BY PARENTAGE. `reap_processes_under` matches on
where a process is ROOTED (cmdline or cwd resolving under the clone), never on
its position in the process table. This is the only predicate that survives the
detach: a double-forked, reparented grandchild is by construction no longer a
child of anything this fixture can enumerate downward from, but it is still
running out of a directory unique to this run.

SAFE BY THE UNIQUENESS OF `mkdtemp`. Both functions take a root that is a
freshly `mkdtemp`'d path unique to THIS run, so nothing outside this run's own
processes can resolve under it -- the same construction argument the
`breadcrumb.svc_dir` removal in these fixtures already relies on. Never point
either function at a real engine root.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import warnings
from pathlib import Path
from typing import List, Optional

_TERMINATE_TIMEOUT_SECS = 5.0
_KILL_TIMEOUT_SECS = 3.0

# `scratch/` and not a new name: `.gitignore` already ignores it AT ANY DEPTH
# ("`scratch/` matches at any depth (top-level scratch/, tasks/scratch/, etc.)"
# -- that file's own comment), so this needs no ignore-rule edit to stay out of
# `git status`, and a peer reading the tree already knows what it is. The
# `benchmark-clones/` leaf keeps these separable from anything else that lands
# in `scratch/`, so a sweep can target them by name.
_SCRATCH_RELPATH = ("scratch", "benchmark-clones")


class RootlessCloneDestination(RuntimeError):
    """A clone destination that resolves under no git root.

    Its own class rather than a bare `AssertionError` so a caller can catch it
    narrowly, and so `-O` (which strips `assert`) cannot silently disarm the
    check -- this is a correctness guard, not a debug aid.
    """


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


def mkdtemp_for_clone(source_root: Path, *, prefix: str) -> Path:
    """Make a fresh throwaway directory for an isolated engine clone, on the
    same volume as `source_root` and under a git root.

    Same volume is a HARD requirement, not a preference: `os.link` cannot
    cross volumes, and these clones are hardlinked. That is what rules out
    `tempfile.gettempdir()` and why this takes `source_root` rather than a
    destination -- callers must not be able to answer the volume question
    wrongly.

    Returns the `mkdtemp`'d parent. The caller owns removing it, and should do
    so through `reap_processes_under` + `rmtree_or_warn` below rather than a
    bare `rmtree` -- see this module's docstring for what a bare one hides.

    RAISES `RootlessCloneDestination` when no `.git` is found above the
    resolved destination. This is the one place the question can be answered
    honestly: "resolves under no git root" is a fact about where this box has
    the tree checked out, not a property of the source text, so it is decidable
    at runtime here and NOT decidable by any static reading of the call.
    """
    scratch = source_root.joinpath(*_SCRATCH_RELPATH)
    _assert_under_git_root(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=str(scratch)))


def _normalized(path: Path) -> str:
    """Comparison form for a root: absolute, symlink-resolved, and
    case-folded on the platforms whose paths are case-insensitive, so a
    cmdline spelling that differs only in case or in 8.3-vs-long form still
    matches the root it actually runs out of."""
    resolved = os.path.realpath(str(path))
    return resolved.casefold() if sys.platform in ("win32", "darwin") else resolved


def _process_is_rooted_under(proc, root_norm: str) -> bool:
    """True when this process's command line or working directory resolves
    under `root_norm`.

    Both axes are needed and neither subsumes the other: the detached
    supervisor names its script path in `cmdline` (matching there), while a
    child it spawns in turn may carry only an inherited `cwd`. Every psutil
    accessor here can raise on a process that exits mid-inspection or that
    this user cannot open -- all of which mean "not demonstrably ours", so
    they resolve False rather than propagating.
    """
    import psutil

    try:
        cmdline = " ".join(proc.cmdline() or ())
    except (psutil.Error, OSError):
        cmdline = ""
    if cmdline:
        probe = cmdline.casefold() if sys.platform in ("win32", "darwin") else cmdline
        if root_norm in probe:
            return True
    try:
        cwd = proc.cwd()
    except (psutil.Error, OSError):
        return False
    if not cwd:
        return False
    try:
        return _normalized(Path(cwd)).startswith(root_norm)
    except OSError:
        return False


def reap_processes_under(root: Path) -> List[int]:
    """Terminate every live process rooted under `root`; return the pids reaped.

    Escalates `terminate()` to `kill()` for anything still alive after
    `_TERMINATE_TIMEOUT_SECS`, because a surviving process is not merely
    untidy here -- it is what makes the subsequent `rmtree` fail. Best-effort
    per process (a benchmark teardown must never raise past its own gate's
    result), but NOT silent in aggregate: the returned list is what
    `rmtree_or_warn` and the fixtures report.
    """
    import psutil

    root_norm = _normalized(root)
    doomed = []
    for proc in psutil.process_iter():
        if proc.pid == os.getpid():
            continue
        try:
            if _process_is_rooted_under(proc, root_norm):
                doomed.append(proc)
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

    return [proc.pid for proc in doomed]


def rmtree_or_warn(root: Path, *, label: str, reaped: Optional[List[int]] = None) -> bool:
    """Remove `root`, and WARN -- never silently pass -- if anything survives.

    The `ignore_errors=True` this replaces is precisely how a leaking fixture
    reads as a clean one. The removal itself stays best-effort (teardown must
    not raise past the gate's own verdict), but a survivor now surfaces as a
    warning naming the path, the processes still rooted there, and the pids
    this teardown already reaped -- everything needed to tell "the reaper
    missed one" from "something outside this run holds it open".
    """
    shutil.rmtree(root, ignore_errors=True)
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

    warnings.warn(
        f"{label}: isolated clone survived teardown at {root} -- "
        f"still-rooted processes={holders or 'none found'}, "
        f"reaped_this_teardown={reaped or []}. "
        f"Left on disk; remove it and investigate the surviving holder.",
        stacklevel=2,
    )
    return False
