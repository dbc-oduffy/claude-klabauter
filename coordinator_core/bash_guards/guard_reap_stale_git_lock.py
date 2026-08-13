"""coordinator_core.bash_guards.guard_reap_stale_git_lock -- stat-gated
pre-op self-heal for an orphaned ``.git/index.lock`` (and its sibling
``next-index-*.lock`` / ``objects/maintenance.lock``) on the raw ``git``
invocation path.

Subject: the fleet-wide `.git/index.lock` contention problem
``guard_no_optional_locks.py`` documents already has a correctly-gated
reaper (``coordinator_core.ops.reap_stale_locks``, aged-AND-stable, never a
live lock) and a ceremony-code caller (``coordinator_core.git_lock_retry``,
landed separately) -- but neither sits on the PreToolUse hot path an agent
hits when it just types ``git commit`` at the tool seam. This guard is that
third caller: a raw lock-taking ``git`` invocation self-heals an orphaned
lock instead of failing with
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
this guard is a no-subprocess check for the overwhelming common case (no
lock file present) -- it never spawns ``git`` itself. Its path resolution
is plain ``os.path``/``pathlib`` work, not a ``git rev-parse
--absolute-git-dir`` subprocess call:

    - when the matched segment carries ``-C <dir>`` and/or
      ``--git-dir=<dir>``/``--git-dir <dir>``, the lock directory is
      resolved from those overrides directly (a relative ``-C`` value is
      joined against ``cwd`` first, matching real git's own chdir-then
      -discover semantics);
    - otherwise this guard walks upward from ``cwd`` looking for an
      enclosing ``.git`` entry, bounded by ``_MAX_UPWARD_WALK`` directory
      hops and the filesystem root -- still zero spawns, since a directory
      stat is not a subprocess.

This is still a deliberate heuristic narrowing versus ``reap_stale_locks``'s
own worktree/submodule-aware ``git rev-parse`` resolution: a linked
worktree or a `.git`-file submodule whose real git-dir differs from the
plain ``<repo>/.git`` path this guard finds is not covered by this guard's
cheap path and falls through to ``git`` itself raising the lock-exists
error unaided, same as before this guard existed. Widening to the fully
general case (dereferencing a `.git`-file's ``gitdir:`` pointer, or a
linked worktree's shared-common-dir split) would reintroduce exactly the
per-invocation subprocess this guard exists to avoid paying in the common
(no-lock) case.

Only once a lock file is confirmed to exist by a plain stat does this guard
call into ``reap_stale_locks.do_reap`` -- the existing age-and-stability
gate is the sole authority on whether the lock is safe to remove; this
module does not re-derive or weaken that threshold. A fresh or
actively-mutating lock (the common "a peer is mid-commit right now" case)
is left untouched, exactly as ``do_reap`` already guarantees for every
other caller.

SUBCOMMAND COVERAGE (decided 2026-08-12, not inferred): the closed set
below now covers every git subcommand documented as index-writing, and the
guard offers ``next-index-*.lock``/``objects/maintenance.lock`` to
``do_reap`` in addition to ``index.lock`` -- matching the set
``reap_stale_locks`` itself already implements. Widening was previously
reserved as "a future decision" in this docstring; the P1/P2 backlog
entries below made that ruling. See
state/bug-backlog/2026-08-12-reap-stale-git-lock-resolves-cwd-git-and-939c65ee3472.yaml
(the ``-C``/path-resolution defect) and
state/bug-backlog/2026-08-12-reap-stale-git-lock-s-subcommand-list-om-edb6c786408f.yaml
(the subcommand/lock-file coverage defect).

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
import shlex
from pathlib import Path
from typing import List, Optional, Tuple

from coordinator_core.bash_guards.dispatch_checks import (
    _GIT_GLOBAL_OPT_NO_ARG_SIMPLE,
    _GIT_GLOBAL_OPT_WITH_ARG,
    _bt_exceeds_tokenizable_ceiling,
    _crlf_strip,
    _normalize_executable_basename,
)

#: Subcommands that take the worktree ``index.lock`` -- git's index-writing
#: set (decided 2026-08-12 per the P2 backlog entry cited in the module
#: docstring's SUBCOMMAND COVERAGE section). Deliberately still an explicit
#: closed list, not derived from git's own subcommand vocabulary: an
#: unlisted subcommand simply does not fire this guard, fail-closed, rather
#: than this guard guessing at git's internals.
_LOCK_TAKING_SUBCOMMANDS = frozenset({
    "add", "commit", "status", "diff", "mv", "stash",
    "checkout", "switch", "restore", "reset", "merge", "rebase",
    "cherry-pick", "revert", "pull", "am", "apply", "rm", "clean",
    "submodule", "read-tree", "update-index", "sparse-checkout",
})

#: Separator characters that split ``cmd`` into candidate git segments --
#: same vocabulary ``guard_no_optional_locks._SEP_TOKEN_CHARS`` uses, not
#: imported from there since that name is that module's own private detail.
_SEP_CHARS = frozenset(";&|")

#: Bound on the upward directory walk used to find an enclosing ``.git``
#: when no ``-C``/``--git-dir`` override is present (see module docstring's
#: COST DISCIPLINE section) -- large enough for any real repo nesting depth,
#: small enough to guarantee termination even on a pathological ``cwd``.
_MAX_UPWARD_WALK = 64


def _split_segments(cmd: str) -> List[str]:
    """Cheap, non-tokenizing segment split on top-level ``;``/``&&``/``||``/
    ``|`` -- good enough to locate a *candidate* git segment for the
    subcommand check below, which itself re-parses the segment properly via
    ``_seg_git_invocation``. Does not attempt quote-awareness -- a
    false-positive candidate segment simply fails the subsequent resolution
    (fail-closed, no-op) rather than mis-firing."""
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


def _seg_git_invocation(seg: str) -> Optional[Tuple[str, Optional[str], Optional[str]]]:
    """Positionally resolve ``seg``'s git SUBCOMMAND together with any
    ``-C``/``--git-dir`` override value present ahead of it, in one token
    walk mirroring ``dispatch_checks._seg_resolved_git_subcommand``'s own
    walk (same global-option vocabulary, same fail-closed discipline: an
    unresolvable case returns ``None``, never a guess). Returns
    ``(subcommand, dash_c_value, git_dir_value)`` when the subcommand is
    positively resolved, either override value ``None`` if absent.

    A `<<` heredoc marker anywhere in ``seg`` also forces ``None`` -- a
    heredoc body is not part of the invoking command's own argv in real
    shell semantics, so treating it as ordinary follow-on argv would be
    confidently wrong (same rationale as the sibling walk this mirrors)."""
    if "<<" in seg:
        return None
    if _bt_exceeds_tokenizable_ceiling(seg):
        return None
    try:
        tokens = shlex.split(seg, posix=True)
    except ValueError:
        return None
    if not tokens or _normalize_executable_basename(tokens[0]) != "git":
        return None
    dash_c: Optional[str] = None
    git_dir: Optional[str] = None
    i = 1
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok in _GIT_GLOBAL_OPT_WITH_ARG:
            if i + 1 >= n:
                return None
            value = tokens[i + 1]
            if tok == "-C":
                dash_c = value
            elif tok == "--git-dir":
                git_dir = value
            i += 2
            continue
        if tok.startswith("--git-dir="):
            git_dir = tok[len("--git-dir="):]
            i += 1
            continue
        if tok.startswith("--") and "=" in tok:
            i += 1
            continue
        if tok.startswith("-"):
            if tok in _GIT_GLOBAL_OPT_NO_ARG_SIMPLE:
                i += 1
                continue
            # Unrecognized flag -- consumption shape unknown, do not guess.
            return None
        return tok, dash_c, git_dir
    return None


def _find_lock_taking_git_invocation(cmd: str) -> Optional[Tuple[Optional[str], Optional[str]]]:
    """Return ``(dash_c_value, git_dir_value)`` for the first segment of
    ``cmd`` that resolves to a lock-taking git subcommand (constraint 6 --
    fire only for git invocations that take the index lock, not every Bash
    call), else ``None``."""
    for seg in _split_segments(cmd):
        if "git" not in seg:
            continue
        resolved = _seg_git_invocation(seg)
        if resolved is None:
            continue
        subcommand, dash_c, git_dir = resolved
        if subcommand in _LOCK_TAKING_SUBCOMMANDS:
            return dash_c, git_dir
    return None


def _walk_up_for_git_dir(start: Path) -> Optional[Path]:
    """Zero-subprocess upward walk (constraint 2) from ``start`` looking for
    an enclosing ``.git`` entry -- a plain ``Path.exists()`` at each hop, not
    a ``git rev-parse`` subprocess. Bounded by ``_MAX_UPWARD_WALK`` and the
    filesystem root; returns ``None`` if neither is reached with a hit."""
    current = start
    for _ in range(_MAX_UPWARD_WALK):
        candidate = current / ".git"
        if candidate.exists():
            return candidate
        parent = current.parent
        if parent == current:
            return None
        current = parent
    return None


def _resolve_git_dir(
    cwd: str, dash_c: Optional[str], git_dir_override: Optional[str]
) -> Optional[Path]:
    """Resolve the lock-holding git directory for the matched invocation
    (the P1 fix): honour an explicit ``--git-dir`` directly, honour ``-C``
    as git itself does (chdir there, then discover), and otherwise walk
    upward from ``cwd`` for an enclosing ``.git`` (see module docstring's
    COST DISCIPLINE section). Returns ``None`` when ``cwd`` is empty/absent
    and no absolute override was given -- nothing to resolve against."""
    base = Path(cwd) if cwd else None
    if dash_c:
        dash_c_path = Path(dash_c)
        if dash_c_path.is_absolute():
            base = dash_c_path
        elif base is not None:
            base = base / dash_c_path
        else:
            base = dash_c_path
    if git_dir_override:
        git_dir_path = Path(git_dir_override)
        if git_dir_path.is_absolute():
            return git_dir_path
        if base is None:
            return None
        return base / git_dir_path
    if base is None:
        return None
    return _walk_up_for_git_dir(base)


def _reap_candidate_locks(git_dir: Path) -> None:
    """Offer every lock ``reap_stale_locks`` itself already understands --
    ``index.lock``, every ``next-index-*.lock``, and the shared-object-store
    ``objects/maintenance.lock`` -- to ``do_reap`` (constraints 3-4): the
    existing, already-tested age-and-stability gate, imported in-process and
    never reimplemented or weakened here. Uses ``reap_stale_locks``'s own
    ``_DEFAULT_MAINT_AGE_SEC`` threshold for the maintenance lock rather
    than inventing a new one (the P2 backlog entry's explicit instruction)."""
    from coordinator_core.ops.reap_stale_locks import (
        _DEFAULT_AGE_SEC,
        _DEFAULT_MAINT_AGE_SEC,
        _DEFAULT_STABILITY_SEC,
        _env_float,
        _env_int,
        do_reap,
    )

    age_sec = _env_int("COORDINATOR_LOCK_REAP_AGE_SEC", _DEFAULT_AGE_SEC)
    maint_age_sec = _env_int("COORDINATOR_LOCK_REAP_MAINT_AGE_SEC", _DEFAULT_MAINT_AGE_SEC)
    stability_sec = _env_float("COORDINATOR_LOCK_REAP_STABILITY_SEC", _DEFAULT_STABILITY_SEC)
    no_sleep = bool(os.environ.get("COORDINATOR_LOCK_REAP_NO_SLEEP", ""))
    reap_log = git_dir / "lock-reap.log"

    index_lock = git_dir / "index.lock"
    if index_lock.exists():
        do_reap(index_lock, age_sec, "index.lock", reap_log, stability_sec, no_sleep)

    for next_lock in sorted(git_dir.glob("next-index-*.lock")):
        do_reap(next_lock, age_sec, "next-index lock", reap_log, stability_sec, no_sleep)

    maint_lock = git_dir / "objects" / "maintenance.lock"
    if maint_lock.exists():
        do_reap(maint_lock, maint_age_sec, "maintenance.lock", reap_log, stability_sec, no_sleep)


def check_reap_stale_git_lock(cmd: str, cwd: str, session_id: str = "") -> Optional[dict]:
    """PreToolUse guard: self-heals an orphaned ``.git/index.lock`` (and its
    ``next-index-*.lock``/``objects/maintenance.lock`` siblings) ahead of a
    raw lock-taking git invocation. Always returns ``None`` (allow, command
    unchanged) -- this guard's only effect is the on-disk reap side effect
    performed before returning; see module docstring for why it is never a
    rewrite/advisory.

    Fail-open (constraint 5): any exception in the body below is swallowed
    and treated identically to "nothing to reap" -- the git command
    proceeds either way, reaped or not.
    """
    try:
        if not cmd or "git" not in cmd:
            return None
        cmd = _crlf_strip(cmd)
        match = _find_lock_taking_git_invocation(cmd)
        if match is None:
            return None
        dash_c, git_dir_override = match
        git_dir = _resolve_git_dir(cwd, dash_c, git_dir_override)
        if git_dir is None:
            return None
        _reap_candidate_locks(git_dir)
    except Exception:
        pass
    return None
