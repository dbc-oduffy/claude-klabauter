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
import time
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


# A clone older than this is not a live fixture's -- the gates that mint them
# run in seconds, and a stuck one is bounded by pytest's own --timeout=300.
# An hour is far past both, so nothing in flight is ever reaped.
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
            # Race (deleted between iterdir() and here) or a Windows
            # reparse-point/permission error -- either way "not demonstrably
            # a stale clone", never a reason to fail the mint path.
            continue
        reap_processes_under(entry)
        shutil.rmtree(entry, ignore_errors=True)


def mkdtemp_for_clone(source_root: Path, *, prefix: str) -> Path:
    """Make a fresh throwaway directory for an isolated engine clone, on the
    same volume as `source_root` and under a git root.

    Same volume is a HARD requirement, not a preference: `os.link` cannot
    cross volumes, and these clones are hardlinked. That is what rules out
    `tempfile.gettempdir()` and why this takes `source_root` rather than a
    destination -- callers must not be able to answer the volume question
    wrongly.

    Returns the `mkdtemp`'d parent. The caller owns removing it, and should do
    so through `reap_processes_under` + `rmtree_or_raise` below rather than a
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
    _reap_stale_clones(scratch)
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
        # Not a bare substring test: a cmdline is not a path, so it cannot use
        # a pure path-boundary check, but "root_norm in probe" alone would
        # also match a sibling whose path merely appears as a longer prefix
        # (`...warm-door-gate-AAAA` inside `...warm-door-gate-AAAAX`). Require
        # the root to be followed by a path separator or to end the string --
        # same boundary strength as the cwd axis below, different mechanism
        # because a cmdline can embed the root anywhere, not just as a prefix.
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


def reap_processes_under(root: Path) -> List[int]:
    """Terminate every live process rooted under `root`; return the pids reaped.

    Escalates `terminate()` to `kill()` for anything still alive after
    `_TERMINATE_TIMEOUT_SECS`, because a surviving process is not merely
    untidy here -- it is what makes the subsequent `rmtree` fail. Best-effort
    per process (a benchmark teardown must never raise past its own gate's
    result), but NOT silent in aggregate: the returned list is what
    `rmtree_or_raise` and the fixtures report.
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


class CloneTeardownLeak(RuntimeError):
    """A clone survived its own teardown, and something is still holding it."""


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

    raise CloneTeardownLeak(
        f"{label}: isolated clone survived teardown at {root} -- "
        f"still-rooted processes={holders or 'none found'}, "
        f"reaped_this_teardown={reaped or []}. "
        f"Left on disk; the holder above is what to investigate."
    )
