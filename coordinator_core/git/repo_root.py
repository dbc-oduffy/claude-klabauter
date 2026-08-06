"""coordinator_core.git.repo_root -- the single shared, cwd-keyed memoized
repo-root resolution seam.

Purpose: the census behind
docs/plans/2026-08-06-eliminate-claude-klabauter-s-non-test-subprocess-spawn-population.md
found 254 call sites shelling out to `git rev-parse` for one of six forms
(`--show-toplevel`, `--git-dir`, `--is-inside-work-tree`,
`--git-common-dir`, `--show-prefix`, `--absolute-git-dir`), split across two
existing resolvers that both forbid caching for DIFFERENT (and both, in
their own scope, CORRECT) reasons:

  - `coordinator_core.ops._git_root_util.git_root` -- one-shot CLI
    processes, caching buys nothing THERE. True for its callers; false for
    the hook/guard callers this module absorbs, which already had to build
    a private memo (`bash_guards.dispatch_checks._new_git_memo`) to
    compensate -- this module generalizes that exact shape instead of
    re-deriving it.
  - `coordinator_core.session.core.git_root` -- NOT cached because cwd can
    legitimately change mid-process for a long-lived import. That
    constraint is real and is preserved here: the memo below is keyed on
    the RESOLVED ABSOLUTE cwd, so two calls with different cwds never share
    an entry, and a caller whose cwd changes mid-process simply gets a
    fresh resolution under the new key.

This module does NOT modify, deprecate, or replace either existing
resolver -- their migration is a later chunk (C2-C4) in the plan above.

Memo lifetime and invalidation: process-lifetime, per-cwd dict, populated
lazily. There is no TTL and no explicit invalidation hook. This is safe
because git repo IDENTITY (the toplevel/gitdir/commondir for a given
absolute working directory) does not change over the lifetime of a single
`coordinator_core` invocation -- these are spawn-per-call CLI processes
(DR-215; no resident daemon), so "process lifetime" is inherently short
(one command dispatch), unlike the hook/guard callers that motivated the
memo, whose whole point is many resolutions within that one short process.
If a future caller genuinely needs a fresh resolution for a cwd it has
already resolved (e.g. a repo was just created under that path), it must
call `clear_memo()` explicitly -- this module never invalidates on its own.

Non-spawning parent-walk vs spawn, honestly per form:
  - `show_toplevel()` -- WALKS (no spawn) for the ordinary case: climbs
    `cwd` looking for a `.git` entry (dir OR file -- submodules and
    `--separate-git-dir` clones use a `.git` FILE, which the walk still
    recognizes as "found", matching git's own behavior of accepting either
    shape at the boundary). Falls back to a SPAWN only if no `.git` entry
    is found on the walk up to `cwd.anchor` -- git's own toplevel discovery
    additionally consults `GIT_DIR`/ceiling directories and bare-repo
    ancestor markers this module does not attempt to replicate walking, so
    the spawn fallback is the ground truth of last resort.
  - `git_dir()` -- WALKS. Delegates the `.git`-is-a-file indirection
    (submodule / `--separate-git-dir`) to
    `coordinator_core.git.git_dir.resolve_git_dir`, which returns the
    worktree-PRIVATE gitdir (never chasing the `commondir` file). Never
    spawns.
  - `git_common_dir()` -- WALKS. Same `.git`-is-a-file indirection, but
    delegates to `coordinator_core.git.git_dir.resolve_git_common_dir`,
    which resolves the COMMON dir (not the private one) through the
    linked-worktree `commondir` file. Never spawns.
  - `show_prefix()` -- SPAWNS. The prefix is CWD-relative-to-toplevel and a
    parent-walk that already knows the toplevel can compute this with plain
    path arithmetic in the ordinary case, but git's own `--show-prefix`
    also normalizes symlink components exactly as git resolves them
    internally, which is meaningfully different from Python's `Path`
    resolution in edge cases (case folding, `..` inside symlinked
    ancestors). Spawns to stay byte-identical to what call sites depend on.
  - `absolute_git_dir()` -- SPAWNS. `--absolute-git-dir` is not merely
    "make git_dir() absolute" -- inside a linked worktree it names the
    worktree-PRIVATE gitdir (`.git/worktrees/<name>`), not the common dir,
    which the walk-based `git_dir()`/`git_common_dir()` pair does not
    separately expose. Spawns rather than growing a second walk path for a
    form with only 3 call sites in the census.
  - `is_inside_work_tree()` -- SPAWNS, deliberately never derived from
    "did `show_toplevel()` return a value". See its own docstring.

Negative caching: a failed resolution (no `.git` found by the walk, or a
spawn that fails -- not a repo, git missing, timeout, non-zero exit) is
NEVER memoized, in `_memo` or `_spawn_memo`. Only a successful resolution
is cached, and `None` is reserved EXCLUSIVELY for "failed" -- a successful
spawn that legitimately emits empty stdout (`--show-prefix` at the
toplevel itself) is memoized as the empty string, never coerced to `None`.
`_spawn_rev_parse` returns `(succeeded, value)` precisely so this
distinction survives the boundary into `_spawn_cached`, which branches on
`succeeded` (not on `value is None`) to decide whether to memoize -- a
prior version of this module collapsed a successful empty result to
`None` before that boundary, which made `show_prefix()` (and
`absolute_git_dir()`) indistinguishable between "you are at the repo
root" and "resolution failed", so a caller's `if prefix is None: bail`
silently bailed at the repo root. A "not found" outcome is not repo
identity, it is the absence of one at this moment, and can flip to
present later in the same process (e.g. a caller resolves before `git
init` has run at that cwd) -- caching it would permanently poison that
key for the rest of the process. Each failed call re-walks/re-spawns; a
call after the repo starts existing resolves correctly instead of being
served a stale `None`.

`cwd=None` decision: resolved to the process's CURRENT absolute cwd (via
`Path.cwd()`) at call time and keyed on THAT resolved value, same as any
explicit `cwd` -- not left uncached wholesale. This is safe (not the
"key on the literal string `None`" bug) because the memo key is always the
POST-resolution absolute path, so a process cwd that changes between two
`cwd=None` calls simply produces two different keys and two independent
(correctly separate) resolutions; a process whose cwd is unchanged between
two `cwd=None` calls is legitimately served the same cached repo identity,
matching `_resolve_cwd`'s existing per-resolved-cwd contract. This is a
DELIBERATELY WEAKER cache policy than
`coordinator_core.subagent_sandbox.engine.resolve_git_root`'s, which never
caches ANY `cwd=None` call (even same-cwd repeats) -- that module's guard
callers prioritize per-call freshness over spawn elimination for the
`cwd=None` branch specifically; this module's callers do not have that
requirement, so it keeps `cwd=None` cacheable once negative results can no
longer poison it.

Negative-spec:
    - Does NOT cache across DIFFERENT cwds -- the memo key is the resolved
      absolute cwd; `session.core.git_root`'s "cwd can legitimately change
      mid-process" correctness property is preserved by construction.
    - Does NOT cache a failed resolution (`None`) at any cwd, in either
      `_memo` or `_spawn_memo` -- see "Negative caching" above. A future
      "simplify by caching whatever the walk/spawn returned, including
      None" change would reintroduce this exact bug.
    - Does NOT key `cwd=None` on the literal string `"None"` or otherwise
      collapse it to one shared entry -- it resolves to the process's
      actual current cwd at call time and keys on that, per "cwd=None
      decision" above.
    - Does NOT collapse `is_inside_work_tree()` into a truthiness check on
      `show_toplevel()` -- see that function's docstring. This is a
      semantic trap, not a simplification opportunity.
    - Does NOT raise on any failure path (not a repo, git missing, timeout,
      malformed `.git` pointer) -- every function returns `None` (or
      `False` for `is_inside_work_tree()`) so callers degrade the way the
      pre-existing resolvers already do.
    - Does NOT touch `coordinator_core.ops._git_root_util` or
      `coordinator_core.session.core.git_root` -- their propagation is
      later chunks (C2-C4) of the same plan; touching them here collides
      with work not yet dispatched.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Dict, Optional, Tuple

from coordinator_core.git.git_dir import resolve_git_common_dir, resolve_git_dir

_TIMEOUT_SECS = 2.0
_NO_CONSOLE = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}

# cwd-keyed memo: absolute-cwd -> (toplevel_or_None, gitdir_or_None,
# common_dir_or_None), populated by the WALK-based path. Deliberately
# module-level (unlike `bash_guards.dispatch_checks._new_git_memo`, which
# is per-call-scoped because it must not outlive one hook invocation) --
# this module IS the process-lifetime seam those per-call memos would
# otherwise reinvent. See module docstring for the lifetime/invalidation
# story.
_MemoEntry = Tuple[Optional[str], Optional[str], Optional[str]]
_memo: Dict[str, _MemoEntry] = {}

# (resolved-cwd, form) -> spawned value, for the forms that always spawn
# (`--show-prefix`, `--absolute-git-dir`, `--is-inside-work-tree`) or that
# fall back to a spawn when the walk finds nothing. Kept separate from
# `_memo` because those forms have no walk-derived value to key off. Values
# are never `None` -- only a SUCCESSFUL spawn is memoized here (see
# `_spawn_cached`), and a successful spawn's value is always a `str` (the
# empty string included); a genuine failure is never written to this dict
# at all, so the type is `str`, not `Optional[str]`.
_spawn_memo: Dict[Tuple[str, str], str] = {}


def clear_memo() -> None:
    """Drop every memoized resolution. Not called automatically -- see the
    module docstring's "Memo lifetime and invalidation" section. Exists for
    the rare caller (or test) that needs a fresh resolution after the
    on-disk repo identity at a previously-resolved cwd has changed.
    """
    _memo.clear()
    _spawn_memo.clear()


def _resolve_cwd(cwd: Optional[str]) -> str:
    base = Path(cwd) if cwd is not None else Path.cwd()
    try:
        return str(base.resolve())
    except OSError:
        return str(base)


def _walk_for_dot_git(start: Path) -> Optional[Path]:
    """Climb from `start` looking for a `.git` entry (directory OR file --
    see module docstring). Returns the directory CONTAINING `.git` (i.e.
    the toplevel), or None if none is found before the filesystem root.
    """
    current = start
    while True:
        candidate = current / ".git"
        if candidate.exists():
            return current
        if current.parent == current:
            return None
        current = current.parent


def _spawn_rev_parse(args: list, cwd: str) -> Tuple[bool, Optional[str]]:
    """Returns `(succeeded, value)`. `succeeded` is False only for an
    actual resolution failure (spawn error, timeout, non-zero exit) --
    distinct from a SUCCESSFUL spawn that legitimately emits empty stdout
    (e.g. `--show-prefix` at the toplevel itself), which is `(True, "")`
    and DOES get memoized. `value` is `None` iff `succeeded` is False --
    a successful empty result is the empty string, never `None`, so a
    caller (or `_spawn_cached`) can tell "you are at the repo root" apart
    from "resolution failed" by checking `succeeded`/`value is None`
    directly, without the two collapsing into the same `None` at the
    public API boundary. Collapsing "succeeded with empty output" into
    "failed" would either wrongly cache a real failure or wrongly refuse to
    cache a legitimate empty-string result -- see module docstring's
    "Negative caching" section.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=_TIMEOUT_SECS,
            **_NO_CONSOLE,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, None
    if result.returncode != 0:
        return False, None
    return True, result.stdout.strip()


def _memo_entry(cwd: Optional[str]) -> Tuple[str, _MemoEntry]:
    resolved = _resolve_cwd(cwd)
    entry = _memo.get(resolved)
    if entry is None:
        toplevel_path = _walk_for_dot_git(Path(resolved))
        if toplevel_path is None:
            # Deliberately NOT stored in `_memo` -- see "Negative caching"
            # in the module docstring. A "no `.git` entry found" outcome is
            # not repo IDENTITY, it is the absence of one, and can flip to
            # present at any time (e.g. `git init` runs at `resolved` after
            # this call). Caching it would poison every subsequent call at
            # this cwd for the rest of the process.
            return resolved, (None, None, None)
        toplevel = str(toplevel_path)
        git_dir_str = str(resolve_git_dir(toplevel_path))
        common_dir_str = str(resolve_git_common_dir(toplevel_path))
        entry = (toplevel, git_dir_str, common_dir_str)
        _memo[resolved] = entry
    return resolved, entry


def _spawn_cached(resolved: str, form: str, args: list) -> Optional[str]:
    key = (resolved, form)
    if key in _spawn_memo:
        return _spawn_memo[key]
    succeeded, value = _spawn_rev_parse(args, resolved)
    if succeeded:
        # See "Negative caching" in the module docstring -- a failed spawn
        # (not a repo, git missing, timeout) must be retried on the next
        # call rather than served a memoized `None` forever. A SUCCESSFUL
        # spawn with empty output (e.g. `--show-prefix` at the toplevel) is
        # still memoized here.
        _spawn_memo[key] = value
    return value


def show_toplevel(cwd: Optional[str] = None) -> Optional[str]:
    """Mirror `git rev-parse --show-toplevel`: the enclosing repo's
    worktree root, or None. Walks for the ordinary case, spawns only when
    the walk finds no `.git` entry -- see module docstring.
    """
    resolved, entry = _memo_entry(cwd)
    if entry[0] is not None:
        return entry[0]
    return _spawn_cached(resolved, "show-toplevel", ["--show-toplevel"])


def git_dir(cwd: Optional[str] = None) -> Optional[str]:
    """Mirror `git rev-parse --git-dir`: the repo's (private) gitdir, or
    None. Never spawns when a `.git` entry is found by the walk.
    """
    resolved, entry = _memo_entry(cwd)
    if entry[1] is not None:
        return entry[1]
    return _spawn_cached(resolved, "git-dir", ["--git-dir"])


def git_common_dir(cwd: Optional[str] = None) -> Optional[str]:
    """Mirror `git rev-parse --git-common-dir`: the repo's COMMON dir
    (worktree/submodule-indirection-resolved), or None. Delegates to
    `coordinator_core.git.git_dir.resolve_git_common_dir`, so never spawns
    when a `.git` entry is found by the walk (that path is always absolute
    -- see module docstring). The spawn-fallback path below does NOT pass
    `--path-format=absolute`, so `git` can legitimately hand back a
    `resolved`-relative path (e.g. `.git`); every documented caller of this
    seam treats the return value as absolute (see `lifecycle.git_common_dir`'s
    negative-spec), so that relative result is normalized against `resolved`
    (the same cwd the spawn ran under) here, once, at the seam -- rather than
    leaving each caller to re-derive the same join.

    Asymmetry vs the other two walk-backed forms: this is the one consumer
    where a hypothetical spawn-success-with-empty-stdout would degrade to a
    WRONG non-`None` value rather than a benign one -- `Path("")` normalizes
    to `Path(".")`, which is not absolute, so the join below would silently
    return the CALLER'S CWD as the git common dir instead of failing loudly.
    Guarded explicitly below (`spawned == ""` returns `None`) rather than
    left to fall through to the join, even though `--git-common-dir` has been
    verified never to emit empty stdout on success in practice (out-of-band
    probe, code-reviewer sidecar `coordinatorcode-reviewer-6815a476.md` § 2,
    11 repo shapes) -- see `test_git_common_dir_empty_on_success_does_not_
    return_resolved_cwd` in test_repo_root.py, which calls THIS function
    (unlike the real-git blast-radius test) and fails if this guard is
    removed. The module docstring's blast-radius claim that the three
    walk-backed forms are "untouched" does not, on its own, surface this
    one form's different failure mode -- this guard is what keeps that claim
    true even if the "never empty" assumption is ever violated.
    """
    resolved, entry = _memo_entry(cwd)
    if entry[2] is not None:
        return entry[2]
    spawned = _spawn_cached(resolved, "git-common-dir", ["--git-common-dir"])
    if spawned is None:
        return None
    if spawned == "":
        # A successful spawn with EMPTY stdout must not fall through to the
        # join below -- `Path("")` is not absolute, so `Path(resolved) /
        # Path("")` collapses to `resolved` itself, silently returning the
        # CALLER'S CWD as if it were the repo's common dir. Fail loudly
        # instead (return the module's uniform "resolution failed" signal).
        # See the docstring's "Asymmetry" paragraph above.
        return None
    spawned_path = Path(spawned)
    if not spawned_path.is_absolute():
        spawned_path = Path(resolved) / spawned_path
    return str(spawned_path)


def absolute_git_dir(cwd: Optional[str] = None) -> Optional[str]:
    """Mirror `git rev-parse --absolute-git-dir`: the WORKTREE-PRIVATE
    gitdir as an absolute path (differs from `git_common_dir()` inside a
    linked worktree -- see module docstring). Always spawns; not derivable
    from the walk-based memo entry.
    """
    resolved, _ = _memo_entry(cwd)
    return _spawn_cached(resolved, "absolute-git-dir", ["--absolute-git-dir"])


def show_prefix(cwd: Optional[str] = None) -> Optional[str]:
    """Mirror `git rev-parse --show-prefix`: `cwd`'s path relative to the
    repo toplevel, git-normalized (trailing slash, symlink-resolved per
    git's own rules -- see module docstring for why this always spawns
    rather than being computed from `show_toplevel()` with `Path` math).

    Returns the EMPTY STRING (not `None`) when `cwd` IS the repo toplevel
    -- `--show-prefix` legitimately emits empty stdout there, and that is
    a successful resolution, not a failure. Only a genuine failure (not a
    repo, git missing, timeout) returns `None`. A caller branching on
    `is None` to bail relies on this: collapsing the toplevel case to
    `None` would make it indistinguishable from failure and silently bail
    at the repo root -- see module docstring's "Negative caching" section.
    """
    resolved, _ = _memo_entry(cwd)
    return _spawn_cached(resolved, "show-prefix", ["--show-prefix"])


def is_inside_work_tree(cwd: Optional[str] = None) -> bool:
    """Mirror `git rev-parse --is-inside-work-tree`: True iff `cwd` is
    inside a git repo's WORKING TREE specifically.

    This is a SEMANTIC TRAP, not a refactor target: inside a BARE repo (no
    working tree), `show_toplevel()` also fails to produce a root, but the
    reasons are NOT the same "not in a repo" case a truthiness check on
    `show_toplevel()` would conflate them into.
    `coordinator_core.bash_guards.dispatch_checks.check_destructive_rm`
    documents relying on exactly this distinction (a bare-repo target is
    denied via its OWN branch, not the toplevel-resolution branch) -- this
    function exists so that distinction has one canonical, spawn-backed
    answer instead of being re-derived ad hoc per caller. Always spawns:
    collapsing it into cached walk state would be the exact bug this
    docstring warns against.
    """
    resolved, _ = _memo_entry(cwd)
    out = _spawn_cached(resolved, "is-inside-work-tree", ["--is-inside-work-tree"])
    return out == "true"
