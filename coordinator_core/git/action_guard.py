"""coordinator_core.git.action_guard -- the guarded seam every Python-side
git-writing caller routes through, evaluating a deny-capable guard's own
predicate at the ACTION rather than at the harness.

Built for `docs/plans/2026-08-30-every-deny-capable-guard-fires-on-a-tool-
call-the-op-route-never-makes.md` § C2. `commit_paths`
(`coordinator_core.git.commit`) is the first caller, and today (measured at
HEAD before this row) consults nothing: no ownership check, no `dispatch_
checks` import, no claim this seam existed at all.

TWO PREDICATES, TWO DIFFERENT ANSWERS TO "IS IT LIFTABLE":

1. **Ownership scope** (`assert_paths_in_session_scope`,
   `coordinator_core.ops.session.scope_report`) -- payload-free, already
   importable, LIFTED DIRECTLY. It takes `(session_id, paths, cwd)`, which is
   already the shape a seam holding a resolved pathspec has in hand. No
   second implementation is authored here.

2. **Sweeping-pathspec + orphan-adoption + ownership composite**
   (`block_subagent_commit._git_commit_agent_pathspec_permitted`) -- the
   payload-shape-agnostic core extracted (pure motion, C2's own `writes:`)
   out of `_git_commit_agent_may_commit`, which previously took a raw
   command STRING and could not be called from a seam that already holds
   `paths`. The ~5500 lines of command-string tokenizing / shell unwrapping /
   `python -c` AST constant-folding that RESOLVE a command string into
   `(paths, include_orphans)` are NOT lifted -- at this seam the caller
   already holds that resolved shape, so there is nothing for that bulk to
   do. `commit_paths()` does not (and structurally cannot, per point 3
   below) reach this leg today; it is exposed here so a future caller that
   *can* supply the needed identity has a seam to call rather than a reason
   to re-implement.

3. **Caller identity is NOT LIFTABLE, and this is the point C2a exists to
   make** (`state/dispatch-briefs/.../C2a.md`, referenced in the escape
   table). `block_subagent_commit`'s ownership leg grants on a HARNESS-
   SUPPLIED, non-cooperative `agent_id` read off the PreToolUse payload --
   its own module docstring rejects the cooperative `COORDINATOR_AGENT_
   CONTEXT` alternative for exactly the reason this seam must respect: an
   environment variable, or any other self-reported value, is something the
   very caller the guard exists to police can set. C2a measured, at HEAD,
   ZERO occurrences of `agent_id`, `session_id`, or `subagent` in
   `coordinator_core/git/commit.py` or `coordinator_core/ops/ceremony/
   commit_v2.py` -- there is no non-cooperative identity input reaching this
   op route at any price payable in-process today.

   So this module does NOT expose a function that takes a caller-supplied
   `session_id` and trusts it as if it were the harness-stamped `agent_id`.
   That would reproduce, at the seam, the exact fail-open shape the guard
   exists to close (a caller self-reporting the identity a check is meant to
   verify IT against). Instead, `assert_noncooperative_identity_available`
   below is the seam's answer to "does this call carry a trustworthy
   identity": it always returns `False` today, because no non-cooperative
   channel exists to make it return anything else, and any caller consulting
   it before acting is following the prime exit criterion's third disjunct
   verbatim -- "the op route refuses in the absence of that input rather
   than acting on an unresolved or caller-supplied substitute."

NEGATIVE SPEC:
  - Does NOT accept a `session_id` parameter anywhere in this module that is
    treated as verified. `assert_ownership_scope` and `assert_git_commit_
    agent_pathspec_permitted` both take `session_id` as an explicit,
    UNVERIFIED argument -- calling either with a self-reported value is the
    caller's own choice to make, not something this seam launders into a
    trust claim it cannot back.
  - Does NOT shell out. Every function in this module is a plain in-process
    call over already-imported predicates -- no `subprocess`, no `run_git`,
    zero process spawns per call (DR-378's kill bar is not re-earned by a
    predicate that shells out; see the module docstring of `commit.py` for
    the plan that killed `run_commit_pipeline` at 1513ms/31 procs for
    exactly that shape of cost).
  - Does NOT re-implement `_pathspec_element_is_sweeping`, `_repo_
    relativize_pathspec`, or the SC-DR-022 orphan-adoption block --
    `assert_git_commit_agent_pathspec_permitted` calls the extracted
    `block_subagent_commit._git_commit_agent_pathspec_permitted` directly.
"""

from __future__ import annotations

from typing import Optional, Sequence, Tuple

from coordinator_core.ops.session.scope_report import assert_paths_in_session_scope


class CommitActionDenied(Exception):
    """A seam-level guard predicate denied this action. Nothing was written:
    every caller of this module raises before any git object/index/ref
    mutation, matching `CommitRefused`'s own "always safe to retry after
    reconciling" contract in `coordinator_core.git.commit`."""


def assert_ownership_scope(
    session_id: str,
    paths: Sequence[str],
    cwd: Optional[str] = None,
    *,
    allow_orphans: bool = False,
) -> None:
    """Lifts `assert_paths_in_session_scope` directly -- see module
    docstring point 1. Raises `CommitActionDenied` unless every element of
    `paths` classifies as this session's own scope (or, with `allow_orphans`,
    an unclaimed path clearing the positive-evidence check that function
    itself performs). `session_id` is taken as given: this function does not
    resolve or verify identity -- see `assert_noncooperative_identity_
    available` for the seam's answer to whether a caller HAS a trustworthy
    one to pass here at all.
    """
    allowed, reason = assert_paths_in_session_scope(
        session_id, paths, cwd, allow_orphans=allow_orphans
    )
    if not allowed:
        raise CommitActionDenied(reason or "path(s) outside caller's session scope")


def assert_git_commit_agent_pathspec_permitted(
    paths: Sequence[str],
    include_orphans: bool,
    git_root: str,
    session_id: str,
    cwd: Optional[str] = None,
) -> None:
    """Consults the SAME predicate `block_subagent_commit`'s dispatched-
    committer path consults -- see module docstring point 2. Raises
    `CommitActionDenied(deny_reason)` on any of that predicate's LEG 3
    refusals (sweeping pathspec, orphan adoption, out-of-repo absolute
    element, or ownership scope). `session_id` is UNVERIFIED here for the
    same reason `assert_ownership_scope` leaves it unverified -- this
    function does not resolve, and must not be handed, a caller-supplied
    substitute for the harness-stamped `agent_id` the guard itself relies
    on. No caller in this row supplies one (see point 3); this function
    exists so a future caller that legitimately holds a verified identity
    has a seam to call instead of a reason to re-derive the predicate.
    """
    from coordinator_core.bash_guards.block_subagent_commit import (
        _git_commit_agent_pathspec_permitted,
    )

    allowed, reason = _git_commit_agent_pathspec_permitted(
        paths,
        include_orphans,
        git_root,
        session_id,
        cwd,
        assert_paths_in_session_scope=assert_paths_in_session_scope,
    )
    if not allowed:
        raise CommitActionDenied(reason or "git-commit-agent pathspec denied")


def assert_noncooperative_identity_available(session_id: object) -> None:
    """The seam's answer to the guard leg C2a found NOT liftable: caller
    identity. Raises `CommitActionDenied` unconditionally -- there is no
    non-cooperative identity channel reaching any Python-side git-writing op
    route today (C2a, measured at HEAD: zero occurrences of `agent_id` /
    `session_id` / `subagent` in `coordinator_core/git/commit.py` or
    `coordinator_core/ops/ceremony/commit_v2.py`), so this is decidable
    without inspecting `session_id` at all -- ANY value reaching this
    function is, by construction, either absent or a caller-supplied
    substitute, and both refuse identically. `session_id` is accepted only
    so a future non-cooperative wiring has a call site to widen rather than
    a function to invent from scratch; widening this function's verdict is
    the moment a real non-cooperative channel exists, not before.

    Matches the prime exit criterion's third disjunct: "for a guard whose
    predicate depends on an input the op route has no non-cooperative
    source for ... the op route refuses in the absence of that input rather
    than acting on an unresolved or caller-supplied substitute."
    """
    raise CommitActionDenied(
        "caller identity is unresolvable on this op route -- no "
        "non-cooperative identity channel reaches coordinator_core.git "
        "today (C2a), so this seam refuses rather than trust a "
        "caller-supplied session_id as a substitute for a harness-stamped "
        "agent_id"
    )
