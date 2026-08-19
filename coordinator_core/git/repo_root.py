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

Negative caching: a failed WALK (no `.git` found) is NEVER memoized in
`_memo`, and a failed SPAWN is never memoized in `_spawn_memo`. Only a
successful resolution is cached there, and `None` is reserved EXCLUSIVELY
for "failed" -- a successful spawn that legitimately emits empty stdout
(`--show-prefix` at the toplevel itself) is memoized as the empty string,
never coerced to `None`. `_spawn_rev_parse` returns `(outcome, value)`
precisely so this distinction survives the boundary into `_spawn_cached`,
which branches on the OUTCOME (not on `value is None`) to decide whether to
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

One failure IS cached, in `_spawn_negative_memo`, and the two conditions
it is gated on are what separate it from the hazard above rather than an
exception to it. First, the outcome must be `_SPAWN_NOT_A_REPOSITORY`
(exit 128) and not `_SPAWN_TRANSIENT_FAILURE` -- the earlier text named
"timeout" alongside "not a repo" as one undifferentiated failure, and on
this box (50-70 concurrent agents; `docs/wiki/machine-load-norm.md`) a
timeout is transient by construction, so collapsing the two is precisely
why nothing could be cached. Second, the form must be in
`_WALK_BACKED_FORMS`, whose spawn is reachable only after the walk has
already failed: a `git init` at that cwd is therefore answered by the
re-walk and never consults this memo at all. The key carries
`_git_env_key()` so the one remaining input that can flip the answer --
`GIT_DIR` -- cannot be changed mid-process behind a stale entry. What this
buys: without it, the ordinary not-a-repo case spawns `git` on EVERY call
at that cwd (`_spawn_memo` stores successes only) to be told what the walk
established one line earlier.

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
    - Does NOT cache a TRANSIENT spawn failure (timeout, `OSError`, any
      non-zero exit other than 128) anywhere, and does NOT cache a
      not-a-repository failure for a form outside `_WALK_BACKED_FORMS`.
      `_spawn_negative_memo` is narrow on BOTH axes deliberately;
      "simplify by caching every failure the same way" collapses the
      distinction that makes the narrow case safe.
    - Does NOT drop `_git_env_key()` from the negative-memo key. `GIT_DIR`
      is the measured discriminator between exit 128 and exit 0 at a cwd
      with no `.git` (see that function's docstring); keying without it
      would serve a stale "not a repository" to a caller that has since
      set it.
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

import os
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

# `_spawn_rev_parse` outcomes. Plain module-level strings rather than an
# `enum`: this module is on `coordinator_core.ipc`'s cold-start path and is
# measured against a module-count ceiling in
# `coordinator_core/benchmarks/import-budget-manifest.json`, so it does not
# import `enum` for three constants.
_SPAWN_OK = "ok"
_SPAWN_NOT_A_REPOSITORY = "not-a-repository"
_SPAWN_TRANSIENT_FAILURE = "transient-failure"

# `git rev-parse`'s exit code for "not a git repository" -- the one failure
# this module treats as a deterministic property of (cwd, GIT_DIR,
# GIT_WORK_TREE) rather than as a moment's bad luck.
_GIT_NOT_A_REPOSITORY_RC = 128

# The forms whose spawn is REACHED ONLY WHEN THE WALK ALREADY FAILED. This
# set is the precondition that makes negative caching safe, and it is why
# `--show-prefix`, `--absolute-git-dir` and `--is-inside-work-tree` are
# absent from it: those spawn unconditionally, so a cached
# not-a-repository answer for them WOULD survive a `git init` at the same
# cwd and poison the key -- exactly the hazard the module docstring's
# "Negative caching" section names. For the three forms below the hazard
# cannot occur: reaching `_spawn_cached` at all requires
# `_walk_for_dot_git` to have found nothing, walk failure is deliberately
# never memoized, so a post-`git init` call re-walks, finds `.git`, and
# returns before consulting any memo.
_WALK_BACKED_FORMS = frozenset({"git-dir", "git-common-dir"})

# (resolved-cwd, form, git-env-key) for a walk-backed form measured to be
# deterministically not-a-repository. Membership means "the spawn was run
# and answered 128"; the entry suppresses a RE-spawn that cannot answer
# differently. Keyed with the git env discriminator because that is the one
# input, short of the `.git` the walk already rules out, that can flip the
# answer -- see `_git_env_key`.
_spawn_negative_memo: set = set()


def clear_memo() -> None:
    """Drop every memoized resolution. Not called automatically -- see the
    module docstring's "Memo lifetime and invalidation" section. Exists for
    the rare caller (or test) that needs a fresh resolution after the
    on-disk repo identity at a previously-resolved cwd has changed.
    """
    _memo.clear()
    _spawn_memo.clear()
    _spawn_negative_memo.clear()


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


def _spawn_rev_parse(args: list, cwd: str) -> Tuple[str, Optional[str]]:
    """Returns `(outcome, value)`, where `outcome` is one of `_SPAWN_OK`,
    `_SPAWN_NOT_A_REPOSITORY`, or `_SPAWN_TRANSIENT_FAILURE`.

    `_SPAWN_OK` covers a SUCCESSFUL spawn that legitimately emits empty
    stdout (e.g. `--show-prefix` at the toplevel itself), which is
    `(_SPAWN_OK, "")` and DOES get memoized. `value` is `None` iff the
    outcome is not `_SPAWN_OK` -- a successful empty result is the empty
    string, never `None`, so a caller (or `_spawn_cached`) can tell "you
    are at the repo root" apart from "resolution failed" by checking the
    outcome directly, without the two collapsing into the same `None` at
    the public API boundary. Collapsing "succeeded with empty output" into
    "failed" would either wrongly cache a real failure or wrongly refuse to
    cache a legitimate empty-string result -- see module docstring's
    "Negative caching" section.

    The two failure outcomes are kept APART rather than collapsed into one
    `False`, because they have opposite cache semantics and this module
    used to conflate them: `git rev-parse` exits 128 for "not a git
    repository", which is a DETERMINISTIC property of `cwd` plus the
    ambient `GIT_DIR`/`GIT_WORK_TREE`, whereas a timeout or an `OSError`
    is transient by construction on a box running 50-70 concurrent agents
    (`docs/wiki/machine-load-norm.md`). Caching the former is safe under
    the conditions `_spawn_cached` enforces; caching the latter would make
    one loaded moment permanent for the rest of the process. Any other
    non-zero exit is treated as transient -- the conservative direction,
    since only 128 is documented as the not-a-repository signal.
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
        return _SPAWN_TRANSIENT_FAILURE, None
    if result.returncode == _GIT_NOT_A_REPOSITORY_RC:
        return _SPAWN_NOT_A_REPOSITORY, None
    if result.returncode != 0:
        return _SPAWN_TRANSIENT_FAILURE, None
    return _SPAWN_OK, result.stdout.strip()


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


def _git_env_key() -> str:
    """The ambient git-environment inputs that can change a spawn's answer
    at a cwd whose walk found no `.git`.

    Measured 2026-08-19, four env combinations, in a temp dir with no
    `.git` anywhere up to the root: bare `git rev-parse --show-toplevel`
    exits 128; with `GIT_DIR` set it exits 0 (returning cwd, since the work
    tree defaults to it); with `GIT_WORK_TREE` alone it still exits 128;
    with both it exits 0 returning the work tree. `GIT_DIR` is the sole
    discriminator for SUCCESS, and `GIT_WORK_TREE` changes the VALUE once
    `GIT_DIR` is set -- so both belong in the key even though only one of
    them can flip a failure into a success.
    """
    return "\x00".join(
        (os.environ.get("GIT_DIR", ""), os.environ.get("GIT_WORK_TREE", ""))
    )


def _spawn_cached(resolved: str, form: str, args: list) -> Optional[str]:
    key = (resolved, form)
    if key in _spawn_memo:
        return _spawn_memo[key]
    negative_key = (resolved, form, _git_env_key())
    if form in _WALK_BACKED_FORMS and negative_key in _spawn_negative_memo:
        return None
    outcome, value = _spawn_rev_parse(args, resolved)
    if outcome == _SPAWN_OK:
        # See "Negative caching" in the module docstring -- a TRANSIENT
        # failure (git missing, timeout, unexpected non-zero exit) must be
        # retried on the next call rather than served a memoized `None`
        # forever. A SUCCESSFUL spawn with empty output (e.g.
        # `--show-prefix` at the toplevel) is still memoized here.
        _spawn_memo[key] = value
    elif outcome == _SPAWN_NOT_A_REPOSITORY and form in _WALK_BACKED_FORMS:
        # The one cacheable failure, under the one precondition that makes
        # it cacheable -- see `_WALK_BACKED_FORMS`. Without this, the
        # ordinary not-a-repo case re-spawns `git` on EVERY call at that
        # cwd to learn what the walk one line earlier already established,
        # because `_spawn_memo` stores successes only.
        _spawn_negative_memo.add(negative_key)
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
        # A linked worktree's un-formatted `--git-common-dir` answer is
        # relative to `resolved` and carries literal `..` traversal
        # segments (git's raw output, not lexically collapsed) — e.g.
        # `.git/worktrees/<name>/../..`. `Path.__truediv__` joins
        # lexically too: it does NOT collapse those segments, so a bare
        # join leaves a dangling `..` in the result. `os.path.normpath`
        # collapses them with no filesystem I/O and no symlink
        # resolution — unlike `Path.resolve()`, it can't change identity
        # by following a symlink, which matters because callers (e.g.
        # `lifecycle.main_worktree_root`'s bare `.parent`) assume a
        # lexically clean absolute path with no unresolved segments to
        # walk past.
        #
        # Accepted limitation, stated rather than implied (same tradeoff as
        # `git_dir.py::resolve_git_common_dir`'s identical join): lexical
        # collapse and the kernel disagree whenever ANY symlinked path
        # segment is collapsed past -- `<x>/link/..` normalizes to `<x>`,
        # while the OS would land in the link target's parent. That segment
        # need not be `.git` itself: a symlinked intermediate directory, or
        # a symlinked linked-worktree directory, collapses just as wrongly.
        # Taken deliberately: `.resolve()` would change identity by
        # following a symlink, which is worse than the un-collapsed form
        # was for EVERY `.parent` consumer, symlink or not.
        spawned_path = Path(os.path.normpath(Path(resolved) / spawned_path))
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
