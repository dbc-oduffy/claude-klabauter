"""coordinator_core.bash_guards.guard_reap_stale_git_lock -- stat-gated
pre-op self-heal for an orphaned ``.git/index.lock`` on the raw ``git``
invocation path.

Subject: the fleet-wide `.git/index.lock` contention problem
``guard_no_optional_locks.py`` documents already has a correctly-gated
reaper (``coordinator_core.ops.reap_stale_locks``, aged-AND-stable, never a
live lock) and a ceremony-code caller (``coordinator_core.git_lock_retry``,
landed separately) -- but neither sits on the PreToolUse hot path an agent
hits when it just types ``git commit`` at the tool seam. This guard is that
third caller: a raw ``git add``/``commit``/``status``/``diff``/``mv``/
``stash`` invocation self-heals an orphaned lock instead of failing with
``fatal: Unable to create '.git/index.lock': File exists``.

WHY ITS OWN MODULE, NOT ``guard_no_optional_locks.py``: that module's whole
contract is "rewrite this command's tokens to a behavior-preserving
equivalent, prompt-free" -- its return value IS the mechanism. This guard
never rewrites ``cmd`` at all; its mechanism is a side effect (an on-disk
lock removal) performed before returning, and it always returns ``None``
(allow, unchanged) whatever it finds. Folding a side-effecting reaper into
a pure-rewrite module would blur that module's own docstring contract for
every future reader tracing what "``git-no-optional-locks``" fires on.

COST DISCIPLINE (the whole design difficulty -- see the dispatching brief):
this guard is a no-subprocess ``os.stat``-only check for the overwhelming
common case (no lock file present). It resolves the lock path as
``<cwd>/.git/index.lock`` -- a plain string join, not a
``git rev-parse --absolute-git-dir`` subprocess call -- and does nothing
further unless that stat succeeds. This is a deliberate heuristic
narrowing versus ``reap_stale_locks``'s own worktree/submodule-aware
``git rev-parse`` resolution: a linked worktree or a `.git`-file submodule
whose real git-dir differs from ``<cwd>/.git`` is simply not covered by
this guard's cheap path and falls through to ``git`` itself raising the
lock-exists error unaided, same as before this guard existed. Widening the
resolution to the general case would reintroduce exactly the per-invocation
subprocess this guard exists to avoid paying in the common (no-lock) case.

Only once ``<cwd>/.git/index.lock`` is confirmed to exist by a plain stat
does this guard call into ``reap_stale_locks.do_reap`` -- the existing
age-and-stability gate is the sole authority on whether the lock is safe to
remove; this module does not re-derive or weaken that threshold. A fresh or
actively-mutating lock (the common "a peer is mid-commit right now" case)
is left untouched, exactly as ``do_reap`` already guarantees for every
other caller.

FAIL-OPEN: any exception anywhere in this guard's body is swallowed and the
guard falls through to its default ``return None`` (allow, command
proceeds unmodified) -- a reap failure must never block the tool call that
triggered the check, per the dispatching brief's constraint 5. This is the
one guard in the package deliberately written to catch a bare ``Exception``
around its entire side-effecting body, rather than relying on
``dispatch.py``'s own crash-routing (``fail_closed=False`` already routes
an uncaught exception here to "allow", but this guard does not want to
lose its own reap side effect's cheap common-case fast return by risking a
mid-body crash propagate past the stat check at all).

Spec backlink: docs/plans/2026-08-07-git-index-lock-contention-campaign.md
(same fleet-wide lock-contention campaign ``guard_no_optional_locks.py``
belongs to -- this guard is the self-heal leg, that one the avoidance leg).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional, Tuple

from coordinator_core.bash_guards.dispatch_checks import (
    _crlf_strip,
    _seg_resolved_git_subcommand,
)

#: Subcommands that take the worktree ``index.lock`` -- the set this guard
#: fires for. Deliberately the brief's own closed list (constraint 6), not
#: derived from git's own subcommand vocabulary: widening it is a future
#: decision, not something this guard infers from a subcommand's name.
_LOCK_TAKING_SUBCOMMANDS = frozenset({"add", "commit", "status", "diff", "mv", "stash"})

#: Separator characters that split ``cmd`` into candidate git segments --
#: same vocabulary ``guard_no_optional_locks._SEP_TOKEN_CHARS`` uses, not
#: imported from there since that name is that module's own private detail.
_SEP_CHARS = frozenset(";&|")


def _split_segments(cmd: str) -> List[str]:
    """Cheap, non-tokenizing segment split on top-level ``;``/``&&``/``||``/
    ``|`` -- good enough to locate a *candidate* git segment for the
    subcommand check below, which itself re-parses the segment properly via
    ``_seg_resolved_git_subcommand``. Does not attempt quote-awareness --
    a false-positive candidate segment simply fails the subsequent
    subcommand resolution (fail-closed, no-op) rather than mis-firing."""
    segs: List[str] = []
    current: List[str] = []
    for ch in cmd:
        if ch in _SEP_CHARS:
            segs.append("".join(current))
            current = []
        else:
            current.append(ch)
    segs.append("".join(current))
    return segs


def _cmd_targets_lock_taking_git(cmd: str) -> bool:
    """Return True iff any segment of ``cmd`` resolves to a git invocation
    of one of ``_LOCK_TAKING_SUBCOMMANDS`` (constraint 6 -- fire only for
    git invocations that take the index lock, not every Bash call)."""
    for seg in _split_segments(cmd):
        if "git" not in seg:
            continue
        subcommand = _seg_resolved_git_subcommand(seg)
        if subcommand in _LOCK_TAKING_SUBCOMMANDS:
            return True
    return False


def _stat_index_lock(cwd: str) -> Optional[Path]:
    """Zero-subprocess check (constraint 2): plain ``Path.exists()`` on the
    heuristic ``<cwd>/.git/index.lock`` path. Returns the path if it
    exists, else ``None`` -- the overwhelming common case, and the only
    case this guard's cost budget is measured against."""
    if not cwd:
        return None
    lock = Path(cwd) / ".git" / "index.lock"
    if lock.exists():
        return lock
    return None


def _reap(lock: Path) -> None:
    """Invoke the existing, already-tested age-and-stability gate
    (constraints 3-4) -- imported in-process, not subprocessed (constraint
    3's stated preference), and never reimplemented or weakened here."""
    from coordinator_core.ops.reap_stale_locks import (
        _DEFAULT_AGE_SEC,
        _DEFAULT_STABILITY_SEC,
        _env_float,
        _env_int,
        do_reap,
    )

    age_sec = _env_int("COORDINATOR_LOCK_REAP_AGE_SEC", _DEFAULT_AGE_SEC)
    stability_sec = _env_float("COORDINATOR_LOCK_REAP_STABILITY_SEC", _DEFAULT_STABILITY_SEC)
    no_sleep = bool(os.environ.get("COORDINATOR_LOCK_REAP_NO_SLEEP", ""))
    reap_log = lock.parent / "lock-reap.log"
    do_reap(lock, age_sec, "index.lock", reap_log, stability_sec, no_sleep)


def check_reap_stale_git_lock(cmd: str, cwd: str, session_id: str = "") -> Optional[dict]:
    """PreToolUse guard: self-heals an orphaned ``.git/index.lock`` ahead of
    a raw lock-taking git invocation. Always returns ``None`` (allow,
    command unchanged) -- this guard's only effect is the on-disk reap side
    effect performed before returning; see module docstring for why it is
    never a rewrite/advisory.

    Fail-open (constraint 5): any exception in the body below is swallowed
    and treated identically to "nothing to reap" -- the git command
    proceeds either way, reaped or not.
    """
    try:
        if not cmd or "git" not in cmd:
            return None
        cmd = _crlf_strip(cmd)
        if not _cmd_targets_lock_taking_git(cmd):
            return None
        lock = _stat_index_lock(cwd)
        if lock is None:
            return None
        _reap(lock)
    except Exception:
        pass
    return None
