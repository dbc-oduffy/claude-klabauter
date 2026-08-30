"""coordinator_core.git.action_guard -- the guarded seam every Python-side
git-writing caller routes through, evaluating a deny-capable guard's own
predicate at the ACTION rather than at the harness.

Built for `docs/plans/2026-08-30-every-deny-capable-guard-fires-on-a-tool-
call-the-op-route-never-makes.md` § C2. `commit_paths`
(`coordinator_core.git.commit`) is the first caller, and today (measured at
HEAD before this row) consults nothing: no ownership check, no `dispatch_
checks` import, no claim this seam existed at all.

ONE PREDICATE THIS MODULE EXPOSES: sweeping-pathspec + orphan-adoption +
out-of-repo-absolute shape checking
(`block_subagent_commit._pathspec_shape_permitted`) -- the payload-shape-
agnostic, IDENTITY-FREE core extracted (pure motion, C2's own `writes:`)
out of `_git_commit_agent_may_commit`, which previously took a raw
command STRING and could not be called from a seam that already holds
`paths`. The ~5500 lines of command-string tokenizing / shell unwrapping /
`python -c` AST constant-folding that RESOLVE a command string into
`(paths, include_orphans)` are NOT lifted -- at this seam the caller
already holds that resolved shape, so there is nothing for that bulk to
do.

CALLER IDENTITY IS NOT LIFTABLE (`state/dispatch-briefs/.../C2a.md`,
referenced in the escape table). `block_subagent_commit`'s ownership leg
grants on a HARNESS-SUPPLIED, non-cooperative `agent_id` read off the
PreToolUse payload -- its own module docstring rejects the cooperative
`COORDINATOR_AGENT_CONTEXT` alternative for exactly the reason this seam
must respect: an environment variable, or any other self-reported value,
is something the very caller the guard exists to police can set. C2a
measured, at HEAD, ZERO occurrences of `agent_id`, `session_id`, or
`subagent` in `coordinator_core/git/commit.py` or `coordinator_core/ops/
ceremony/commit_v2.py` -- there is no non-cooperative identity input
reaching this op route at any price payable in-process today.

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

NOTE (review: overengineering-reviewer Finding 1, 2026-08-30): this module
used to also expose `assert_ownership_scope` and `assert_git_commit_agent_
pathspec_permitted`, two seams over an ownership-scoped predicate that
took an unverified `session_id`. Both had zero callers repo-wide -- their
own docstrings cited a future caller the section above shows is
permanently impossible on this op route -- and were deleted rather than
kept as seams for a caller that cannot exist. A future verified-identity
caller re-adding a five-line wrapper over `assert_paths_in_session_scope`
or `_git_commit_agent_pathspec_permitted` is the cheap half of that work.

NEGATIVE SPEC:
  - Does NOT accept a `session_id` parameter anywhere in this module that is
    treated as verified.
  - Does NOT shell out. Every function in this module is a plain in-process
    call over already-imported predicates -- no `subprocess`, no `run_git`,
    zero process spawns per call (DR-378's kill bar is not re-earned by a
    predicate that shells out; see the module docstring of `commit.py` for
    the plan that killed `run_commit_pipeline` at 1513ms/31 procs for
    exactly that shape of cost).
  - Does NOT re-implement `_pathspec_element_is_sweeping`, `_repo_
    relativize_pathspec`, or the SC-DR-022 orphan-adoption block --
    `assert_pathspec_shape_permitted` calls the extracted
    `block_subagent_commit._pathspec_shape_permitted` directly.
"""

from __future__ import annotations


def assert_pathspec_shape_permitted(
    paths,
    include_orphans: bool,
    git_root: str,
) -> None:
    """Consults ONLY the sweeping/orphan/out-of-repo legs
    (`block_subagent_commit._pathspec_shape_permitted`), which need no
    caller identity -- this wrapper takes no `session_id` parameter at
    all, so the ownership leg (`assert_paths_in_session_scope`) is never
    reachable through it. That leg fails closed on an empty/unverified
    `session_id`, so calling the ownership-scoped predicate on a route
    with no verified identity would deny every op-route commit; passing
    it a caller-supplied `session_id` would launder an unverified identity
    into the ownership check this seam's negative spec forbids.

    Raises `coordinator_core.git.commit.CommitDeniedByActionGuard` on a
    sweeping pathspec, orphan adoption, or out-of-repo absolute element --
    the three shape-only LEG 3 refusals, minus ownership scope. Raised
    directly as that `CommitRefused` subclass, via a function-body lazy
    import (review: overengineering-reviewer Finding 7, 2026-08-30) --
    `commit.commit_paths` calls this with no try/except of its own, and
    the deny is still catchable both as `CommitDeniedByActionGuard`
    specifically and as `CommitRefused` generically by every existing
    caller.
    """
    from coordinator_core.bash_guards.block_subagent_commit import (
        _pathspec_shape_permitted,
    )
    from coordinator_core.git.commit import CommitDeniedByActionGuard

    allowed, reason = _pathspec_shape_permitted(paths, include_orphans, git_root)
    if not allowed:
        raise CommitDeniedByActionGuard(reason or "pathspec shape denied")


def assert_noncooperative_identity_available() -> None:
    """The seam's answer to the guard leg C2a found NOT liftable: caller
    identity. Raises `coordinator_core.git.commit.CommitDeniedByActionGuard`
    unconditionally -- there is no non-cooperative identity channel
    reaching any Python-side git-writing op route today (C2a, measured at
    HEAD: zero occurrences of `agent_id` / `session_id` / `subagent` in
    `coordinator_core/git/commit.py` or `coordinator_core/ops/ceremony/
    commit_v2.py`), so this is decidable with no argument at all (review:
    overengineering-reviewer Finding 2, 2026-08-30 -- the prior `session_id:
    object = None` parameter was never read and its only live behaviour
    was to be ignored before the unconditional raise; dropped along with
    the `commit_paths(restrict_to_session=...)` parameter that was its only
    caller).

    This is not "no channel exists today" -- a wording that invites a future
    reader to go looking for one. Per the binding spike verdict
    (`docs/research/spike-verdicts/2026-08-30-non-cooperative-caller-
    identity-at-the-in-process-op-route.md`, verdict **not-viable**): no
    non-cooperative caller identity CAN exist for an in-process caller of
    `commit_paths` at any price payable in-process -- the harness-stamped
    `agent_id` is presented only on the tool-call channel and reaches
    `warm/hook_http.py`, no subagent marker exists in the process
    environment, and a subagent shares its EM's `CLAUDE_PID` /
    `CLAUDE_CODE_SESSION_ID` / process tree, so ancestry is not even a layer
    here. This function's unconditional raise is therefore the correct
    permanent answer for this op route, not a stub awaiting wiring.

    Matches the prime exit criterion's third disjunct: "for a guard whose
    predicate depends on an input the op route has no non-cooperative
    source for ... the op route refuses in the absence of that input rather
    than acting on an unresolved or caller-supplied substitute."
    """
    from coordinator_core.git.commit import CommitDeniedByActionGuard

    raise CommitDeniedByActionGuard(
        "caller identity is unresolvable on this op route -- no "
        "non-cooperative identity channel can exist for an in-process "
        "caller of coordinator_core.git (C2a measurement; spike verdict "
        "docs/research/spike-verdicts/2026-08-30-non-cooperative-caller-"
        "identity-at-the-in-process-op-route.md, not-viable), so this seam "
        "refuses rather than trust a caller-supplied session_id as a "
        "substitute for a harness-stamped agent_id"
    )
