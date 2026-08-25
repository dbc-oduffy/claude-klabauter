"""coordinator_core.git.repo_root -- the single shared, cwd-keyed memoized
repo-root resolution seam.

Purpose: the census behind
docs/plans/2026-08-07-spawn-storm-culprit-taxonomy-and-detectors.md
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
  - `show_toplevel()` -- WALKS, and ONLY walks. Climbs `cwd` looking for a
    `.git` entry (dir OR file -- submodules and `--separate-git-dir` clones
    use a `.git` FILE, which the walk still recognizes as "found", matching
    git's own behavior of accepting either shape at the boundary). Returns
    None when the walk finds nothing; it never spawns. It DID have a spawn
    fallback, justified as "the ground truth of last resort" for the
    `GIT_DIR`/ceiling-directory/bare-repo cases the walk does not
    replicate. Measured 2026-08-19, that fallback had no case in which it
    returned a right answer the walk had not already produced -- see the
    function's own docstring for the four-way probe. Deleted rather than
    budgeted.
  - `git_dir()` -- WALKS. Delegates the `.git`-is-a-file indirection
    (submodule / `--separate-git-dir`) to
    `coordinator_core.git.git_dir.resolve_git_dir`, which returns the
    worktree-PRIVATE gitdir (never chasing the `commondir` file). Never
    spawns: the bare repo, its one former spawn case, is recognized by
    `_looks_like_git_dir` from git's own `is_git_directory()` markers.
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
  - `absolute_git_dir()` -- WALKS. It spawned until 2026-08-19 on the claim
    that `--absolute-git-dir` "is not merely make git_dir() absolute --
    inside a linked worktree it names the worktree-PRIVATE gitdir, which the
    walk-based pair does not separately expose". `git_dir()` exposes exactly
    that, and the walk builds it from an already-absolute anchor; measured
    agreement in four shapes, including a submodule. See its own docstring.
  - `is_inside_work_tree()` -- SPAWNS, deliberately never derived from
    "did `show_toplevel()` return a value". See its own docstring.

Negative caching: a failed WALK (no `.git` found) is NEVER memoized in
`_memo`, and a failed SPAWN is never memoized in `_spawn_memo`. Only a
successful resolution is cached there, and `None` is reserved EXCLUSIVELY
for "failed" -- a successful spawn that legitimately emits empty stdout
(`--show-prefix` at the toplevel itself) is memoized as the empty string,
never coerced to `None`. `_spawn_rev_parse` returns `(succeeded, value)`
precisely so this distinction survives the boundary into `_spawn_cached`,
which branches on `succeeded` (not on `value is None`) to decide whether to
memoize -- a prior version of this module collapsed a successful empty
result to `None` before that boundary, which made `show_prefix()` (and
`absolute_git_dir()`) indistinguishable between "you are at the repo
root" and "resolution failed", so a caller's `if prefix is None: bail`
silently bailed at the repo root. A "not found" WALK outcome is not repo
identity, it is the absence of one at this moment, and can flip to
present later in the same process (e.g. a caller resolves before `git
init` has run at that cwd) -- caching it would permanently poison that
key for the rest of the process. Each failed walk re-walks; a call after
the repo starts existing resolves correctly instead of being served a
stale `None`.

NOTHING is negatively cached any more, and the reason is worth keeping:
between 2026-08-19's two passes this module briefly DID cache the
deterministic "not a git repository" (exit 128) outcome for the walk-backed
forms, because collapsing it with a timeout meant the ordinary not-a-repo
case re-spawned on every call. The second pass deleted those fallbacks
outright, so the only spawners left (`show_prefix`, `is_inside_work_tree`)
are unconditional -- and for THEM a cached failure would outlive a `git
init` at the same cwd, which is the hazard above. Removing the spawn beat
making its failure cheap. Do not reintroduce negative caching without first
reintroducing a form whose spawn is gated behind an already-failed walk.

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
    - Does NOT cache a failed resolution (`None`) at any cwd in `_memo` or
      `_spawn_memo` -- see "Negative caching" above. A future "simplify by
      caching whatever the walk/spawn returned, including None" change
      would reintroduce this exact bug.
    - Does NOT cache ANY failure, transient or deterministic -- see the
      "NOTHING is negatively cached" paragraph above for the shape that was
      tried, why it was correct at the time, and why it stopped being
      needed.
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

from pathlib import Path
from typing import Dict, Optional, Tuple

from coordinator_core.git.git_dir import resolve_git_common_dir, resolve_git_dir

_TIMEOUT_SECS = 2.0

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


def _looks_like_git_dir(candidate: Path) -> bool:
    """True if `candidate` IS a git directory (the bare-repo case), by the
    same filesystem markers git's own `is_git_directory()` uses: a `HEAD`,
    an `objects` directory and a `refs` directory. No spawn.

    This is what lets the bare case be answered by the walk instead of by
    `git rev-parse`. Verified 2026-08-19 against a real `git init --bare`:
    all three markers present, and `--git-dir`/`--git-common-dir` there
    report the directory itself -- which is exactly what this returns.

    Accepted limitation, stated rather than implied: this checks `HEAD` for
    EXISTENCE only, where git additionally validates that it parses as a ref
    or a sha. A directory holding three entries literally named `HEAD`,
    `objects/` and `refs/`, none of them real git artifacts, is misread as a
    bare repo here and would not be by git. Contrived enough to accept, but
    the parity claim above is "the same markers", not "the same
    validation", and this is the difference.
    """
    try:
        return (
            (candidate / "HEAD").exists()
            and (candidate / "objects").is_dir()
            and (candidate / "refs").is_dir()
        )
    except OSError:
        return False


def _walk_for_repo(start: Path) -> Optional[Tuple[str, Path]]:
    """Climb from `start` looking for a repo. Returns `("worktree", dir)`
    for a directory containing a `.git` entry (directory OR file -- see
    module docstring), `("bare", dir)` for a directory that IS a git dir,
    or None if neither is found before the filesystem root.

    The `.git` check comes first at each level: a worktree is identified by
    its `.git` entry, and only a directory with no `.git` is tested for
    being a bare repo itself (which is also how a cwd INSIDE a bare repo --
    `<bare>/refs`, say -- resolves, by climbing to the bare root).
    """
    current = start
    while True:
        if (current / ".git").exists():
            return "worktree", current
        if _looks_like_git_dir(current):
            return "bare", current
        if current.parent == current:
            return None
        current = current.parent


def _spawn_rev_parse(args: list, cwd: str) -> Tuple[bool, Optional[str]]:
    """Returns `(succeeded, value)`. `succeeded` is False only for an
    actual resolution failure (spawn error, timeout, non-zero exit) --
    distinct from a SUCCESSFUL spawn that legitimately emits empty stdout
    (e.g. `--show-prefix` at the toplevel itself), which is `(True, "")`
    and DOES get memoized. `value` is `None` iff `succeeded` is False, so a
    caller can tell "you are at the repo root" apart from "resolution
    failed" without the two collapsing into the same `None` at the public
    API boundary.

    Reached only by `show_prefix()` and `is_inside_work_tree()` -- the two
    forms with no walk-derived answer at all. It briefly distinguished exit
    128 from a timeout so the deterministic failure could be negatively
    cached; that split had exactly one consumer, the walk-backed forms'
    spawn fallbacks, and those are gone. Both remaining callers spawn
    unconditionally, and for THEM a cached failure would survive a `git
    init` at the same cwd -- so there is nothing here to cache and nothing
    the split would serve.
    """
    # Function-local: this is the spawn FALLBACK — the walk answers the ordinary
    # case without it, so on the common path `subprocess` and its ~10 transitive
    # modules (select/selectors/signal/threading/locale/math) never load. This
    # module sits on `coordinator_core.ipc`'s cold-start path, measured against a
    # module-count ceiling in
    # `coordinator_core/benchmarks/import-budget-manifest.json`. Do not hoist.
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=_TIMEOUT_SECS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
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
        found = _walk_for_repo(Path(resolved))
        if found is None:
            # Deliberately NOT stored in `_memo` -- see "Negative caching"
            # in the module docstring. A "no `.git` entry found" outcome is
            # not repo IDENTITY, it is the absence of one, and can flip to
            # present at any time (e.g. `git init` runs at `resolved` after
            # this call). Caching it would poison every subsequent call at
            # this cwd for the rest of the process.
            return resolved, (None, None, None)
        kind, found_path = found
        if kind == "bare":
            # A bare repo has NO worktree, so `show_toplevel` correctly has
            # nothing to report (real git: exit 128, "must be run in a work
            # tree"). Its git dir and common dir are the directory itself --
            # `--git-dir`/`--git-common-dir` both answer `.` there.
            entry = (None, str(found_path), str(found_path))
        else:
            entry = (
                str(found_path),
                str(resolve_git_dir(found_path)),
                str(resolve_git_common_dir(found_path)),
            )
        _memo[resolved] = entry
    return resolved, entry


def _spawn_cached(resolved: str, form: str, args: list) -> Optional[str]:
    key = (resolved, form)
    if key in _spawn_memo:
        return _spawn_memo[key]
    succeeded, value = _spawn_rev_parse(args, resolved)
    if succeeded:
        # Only a SUCCESSFUL spawn is memoized -- see "Negative caching" in
        # the module docstring. Both callers of this helper spawn
        # unconditionally, so a memoized failure would outlive a `git init`
        # at the same cwd. A successful spawn with empty output (e.g.
        # `--show-prefix` at the toplevel) is still memoized here.
        _spawn_memo[key] = value
    return value


def show_toplevel(cwd: Optional[str] = None) -> Optional[str]:
    """Mirror `git rev-parse --show-toplevel`: the enclosing repo's
    worktree root, or None. WALKS ONLY -- never spawns, on any path.

    This form had a spawn fallback for a walk that found no `.git`, on the
    stated rationale that git's own discovery additionally consults
    `GIT_DIR`, ceiling directories and bare-repo ancestor markers. Measured
    2026-08-19, that rationale does not survive contact with the binary:

      - no `.git` on the walk and no env: exit 128. The spawn costs a
        subprocess to learn what the walk established one line earlier.
      - bare repo: exit 128, "this operation must be run in a work tree".
        A bare repo has no toplevel to report, so the bare-repo marker case
        the fallback was justified by cannot be answered by this form at
        all (`--git-dir`/`--git-common-dir` DO answer there, which is why
        those two keep their fallbacks).
      - `GIT_WORK_TREE` alone: exit 128. Not a discriminator.
      - `GIT_DIR` set: exit 0 -- and the answer is WRONG. Git reports the
        CWD as the toplevel, because the work tree defaults to cwd when
        `GIT_DIR` is given. With `GIT_DIR` pointing at an unrelated repo,
        this returned a directory that is in no worktree of that repo as if
        it were that repo's root.

    So every reachable outcome was either a failure the walk already knew
    or an actively wrong answer, and hooks are exactly where `GIT_DIR` is
    set. Ceiling directories cannot rescue it either -- they only RESTRICT
    discovery, so they can never turn a failed walk into a success.

    Negative-spec: do NOT reintroduce a spawn fallback here. It is not a
    cost/benefit tradeoff that could be re-decided under a different budget;
    the fallback had no case in which it returned a right answer the walk
    had not already produced.
    """
    _resolved, entry = _memo_entry(cwd)
    return entry[0]


def git_dir(cwd: Optional[str] = None) -> Optional[str]:
    """Mirror `git rev-parse --git-dir`: the repo's (private) gitdir, or
    None. WALKS ONLY -- never spawns.

    The spawn fallback here existed for the one case the walk could not
    answer: a BARE repo, where there is no `.git` entry to find and real
    git still reports `.`. `_walk_for_repo` now recognizes that case
    directly from the filesystem markers git itself uses, so the fallback
    has nothing left to answer.
    """
    _resolved, entry = _memo_entry(cwd)
    return entry[1]


def git_common_dir(cwd: Optional[str] = None) -> Optional[str]:
    """Mirror `git rev-parse --git-common-dir`: the repo's COMMON dir
    (worktree/submodule-indirection-resolved), or None. WALKS ONLY --
    never spawns. Delegates to
    `coordinator_core.git.git_dir.resolve_git_common_dir`, whose result is
    always absolute; a bare repo resolves to the bare directory itself.

    Like `git_dir`, this had a spawn fallback for the bare-repo case, and
    `_walk_for_repo` now answers that case from the filesystem. Deleting it
    also retired two guards that only ever protected against shapes the
    SPAWN could produce and the walk cannot: a successful spawn emitting
    empty stdout (which would have made `Path("")` normalize to `Path(".")`
    and silently return the CALLER'S CWD as the common dir), and a
    `resolved`-relative answer with literal `..` segments needing lexical
    normalization. Neither is reachable from `resolve_git_common_dir`,
    which returns an absolute path or nothing.

    Negative-spec: do NOT reintroduce a spawn fallback to "be safe" without
    also reinstating those two guards -- they were load-bearing for the
    spawn's output shape specifically, not for this function's contract.
    """
    _resolved, entry = _memo_entry(cwd)
    return entry[2]


def absolute_git_dir(cwd: Optional[str] = None) -> Optional[str]:
    """Mirror `git rev-parse --absolute-git-dir`: the WORKTREE-PRIVATE
    gitdir as an absolute path (differs from `git_common_dir()` inside a
    linked worktree). WALKS ONLY -- never spawns.

    This function previously always spawned, on the stated grounds that
    `--absolute-git-dir` is "not merely make `git_dir()` absolute -- inside
    a linked worktree it names the worktree-PRIVATE gitdir, which the
    walk-based `git_dir()`/`git_common_dir()` pair does not separately
    expose." The second half of that is not true of `git_dir()`: it returns
    exactly the worktree-private gitdir (that is
    `resolve_git_dir`'s whole contract, and
    `test_git_dir_via_worktree_indirection_returns_private_not_common`
    pins it), and the walk builds it from an already-absolute resolved cwd.

    Measured 2026-08-19 against real git in four shapes -- plain repo root,
    a nested subdirectory, a linked worktree created by `git worktree add`,
    and a SUBMODULE (whose `.git` is a file carrying a relative `gitdir:`
    pointer, the shape most likely to disagree) -- `git rev-parse
    --absolute-git-dir` and this walk-based value agree in all four. The
    justification for the spawn was a docstring claim about a sibling
    function, not a property of this form.

    Known theoretical edge, shared with the pre-existing code and NOT
    introduced by making this walk-only: an ABSOLUTE `gitdir:` pointer
    containing literal `..` segments comes back un-normalized, because
    `resolve_git_dir` normpaths only the relative form. Git writes these
    pre-normalized in practice.
    """
    _resolved, entry = _memo_entry(cwd)
    return entry[1]


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
