"""coordinator_core.ops.push_outstanding -- decide, at ZERO git
spawns, whether this worktree has anything to push, and reuse
`push_with_retry` for the actual push when it does.

WHY `workday_drain_pending_push.drain_pending_push` CANNOT BE REUSED for
this: it acts only on a pending-hold record the post-commit hook itself
wrote when a commit ran under a deferred push mode. A commit routed through
`push_mode=NEVER` never writes that record, so `drain_pending_push` finds
nothing to act on even when the branch is genuinely ahead of its upstream.
This module answers a different question -- "is HEAD ahead of its upstream
tracking ref, right now" -- without depending on any prior commit having
left a marker behind.

THE DECISION COSTS ZERO SPAWNS, by construction:
    1. HEAD's branch name and sha come from
       `coordinator_core.git.git_state.head_branch`/`head_sha` -- both
       in-process reads of `.git/HEAD` (plus the loose ref or
       `packed-refs` for the sha), never a `git` invocation.
    2. The upstream tracking ref's sha is read directly off
       `<common-dir>/refs/remotes/<remote>/<branch>`, falling back to
       `<common-dir>/packed-refs` when no loose ref exists for that
       remote/branch pair (this repo has both forms on disk; a
       loose-only reader passes here and silently misses elsewhere).
       Every configured remote's `refs/remotes/<remote>/` directory is
       tried -- this module does not assume a single remote is named
       "origin".
    3. Shas equal -> nothing outstanding -> returns without pushing, at
       zero spawns.
    4. Shas differ (or no upstream ref exists at all -- see below) ->
       delegates to `push_with_retry(worktree_root, **kwargs)`, which
       resolves the branch itself, consults `auto_push.branch_gate()` as
       a read-only oracle, declines `main` and an unresolvable HEAD, and
       treats a missing remote as a skip rather than a failure. None of
       that is re-implemented here.

THE NO-UPSTREAM CAVEAT: a branch with no upstream tracking ref at all (a
genuine first push on a fresh branch) has no sha to compare against HEAD.
That MUST read as "outstanding, push it" -- never as "nothing to do" --
because the alternative failure mode is silently never publishing a brand
new branch. This module treats "no upstream ref found" as "differs",
falling through to `push_with_retry` exactly as a genuine sha mismatch
would.

An exact outstanding COMMIT COUNT is deliberately not computed here -- it
is only ever needed for reporting, never for this go/no-go decision, and
computing it would cost a spawn (`git rev-list --count`) this module exists
to avoid paying on the hot path. A caller that wants a count spends that
spawn itself, separately.

THE LFS GATE (C1b, AC7b) -- a RANGE-TOUCHES-LFS-PATHS predicate, not a
cadence tier. The tier split ("LFS push only on the slow-cadence surface")
was researched, measured, and ruled against by the PM on 2026-08-25 in
favour of this narrower mechanism -- see
`docs/plans/2026-08-25-push-re-homes-onto-the-cadence-surfaces.md` §
"LFS participates only where LFS is used -- and the tier split is not what
ships". When this module finds something outstanding to push (the branch
above, one sha short of the fast-return), it decides -- in cost order,
taking the cheapest arm that answers -- whether the `upstream_sha..
current_sha` range being pushed touches any LFS-tracked path:

    1. `.gitattributes` declares no `filter=lfs` at all -> nothing in this
       repo can be LFS-tracked. ZERO spawns, a file read.
    2. Otherwise: `git diff --name-only <range>` (one spawn) piped into
       ONE batched `git check-attr filter --stdin` (one spawn) -- never a
       per-path invocation, which pays ~15ms each and destroys the win.
    3. The range touches an LFS-tracked path -> push proceeds exactly as
       `push_with_retry` would run it unmodified, LFS hook and all.
       Correctness first: no ref reaches the remote without its objects,
       ever.

When arm 1 or arm 2 finds nothing LFS-tracked in the range, this module
RECORDS that verdict on the returned `PushOutcome.skipped`
(`push:lfs-range-clean` / `push:lfs-range-touched`) and pushes via
`push_with_retry` UNMODIFIED. It does NOT set `GIT_LFS_SKIP_PUSH`.

Why the env-var arm was removed (EM, 2026-08-25, during execution):

  1. It mutated `os.environ` around a `push_with_retry` call. This engine is
     warm and shared -- the load norm is 50-70 concurrent sessions -- so a
     peer's push overlapping that window would inherit `GIT_LFS_SKIP_PUSH=1`
     and strand objects it knew nothing about. Passing the variable to the
     child instead would need an `env` parameter on `push_with_retry`, which
     lives in `commit_pipeline.py` -- out of this run's scope.
  2. It was redundant. The LFS cost this plan targets is `.git/hooks/pre-push`
     shelling into `git lfs pre-push` (267.2ms / 20 spawns measured), and C1's
     hook gate already removes it at the hook.
  3. Its correctness rested entirely on the predicate below, which shipped a
     Windows CRLF defect that answered "range is clean" for a range carrying a
     genuine LFS object -- the precise input that strands. A mechanism whose
     only safeguard is a predicate that just failed open does not earn the
     strand window the PM ruled against on 2026-08-25.

The predicate itself is kept and is live: it is the AC7b answer, and the hook
is where a future chunk should consume it.

`git lfs ls-files` is deliberately NOT used for this predicate -- measured
at 214.1ms / 16 spawns, it pays the same git-lfs startup cost this
predicate exists to avoid, for no better an answer.

WHY THIS MODULE LIVES OUTSIDE `coordinator_core/ops/ceremony/`, AND WHY
MOVING IT THERE IS BREAK-CLASS (2026-08-26). Directory placement is a
BUDGET declaration here, not a filing convention: `ipc.is_ceremony_method`
resolves ceremony membership as a union of the `ceremony.` method-name
prefix, `_CEREMONY_PACKAGE_ALIASES`, and the owning module living under
`coordinator_core.ops.ceremony.`, and every member is clamped to
`CEREMONY_BUDGET_SECS` (2.0s) -- a one-directional ratchet with no per-op
exception, no env override, and no widening (DR-348).

This op cannot be born inside that ceiling and must not pretend to be.
`push_with_retry` is push -> on reject fetch -> `rebase --onto` -> re-push,
up to `_PUSH_MAX_RETRIES` times; that is one to three genuinely-remote round
trips plus a local rebase. The 2.0s clamp is enforced by `asyncio.wait_for`,
i.e. WALL CLOCK, on a box whose design condition is 50-70 concurrent
sessions -- `coordinator_core/git/run.py` measures a bare `git --version` at
a 2,891ms p95 wall from spawn scheduling alone, which is why local git work
there carries `_SPAWN_SCHEDULING_HEADROOM_SECS` (10.0s) on top of its
process-time budget. A remote leg under a 2.0s wall ceiling therefore fails
on peer load rather than on any property of the push, and DR-349's
`REMOTE_BUDGET_SECS` (30.0s) is the runaway guard that actually fits the
job. Outside the package this op resolves to the global 30s guard,
identically cold and warm.

It shipped in the ceremony package on 2026-08-25 and inherited the 2.0s
ceiling by accident of placement -- it has never been named `ceremony.*`.
The observed cost: a load-order-dependent budget (30.0s in a cold
interpreter, 2.0s once anything had imported `_registry_map`, because the
package signal is the only one it matched and that signal never imports),
and real timeouts at every named push checkpoint on a branch that merely
needed a fetch first -- reported twice on 2026-08-26,
`state/bug-backlog/2026-08-26-push-outstanding-times-out-at-its-own-2s-*`
and `...-push-outstanding-s-budget-is-load-order-*`.

This is NOT the rename bypass `_CEREMONY_PACKAGE_PREFIX` exists to close.
That bypass is silent by construction; this is the diff that signal was
designed to force into the open, and the ceremony budget is untouched by it
-- no widened row, no alias, no exception. The op reaching for a remote is
the fact; where it sits is the honest declaration of that fact.

NO UPSTREAM REF, OR AN UNRESOLVABLE BRANCH/HEAD: exactly the same "cannot
be decided at zero spawns" cases the outstanding-work decision above
already falls through on. This module does not open a second, more
expensive path just to establish a range in those cases -- it pushes
normally, unmodified, deferring entirely to `push_with_retry`/the
installed hook, the same "correctness first" default as arm 3 above.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Sequence, Union

from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.ipc import register_op
from coordinator_core.git.git_state import head_branch, head_sha
from coordinator_core.ops.ceremony.commit_pipeline import (
    PUSH_RETRY_BUDGET_SECS,
    PushOutcome,
    push_with_retry,
)

__all__ = ["push_outstanding"]

#: `.gitattributes`, repo-root only -- this predicate answers "can this
#: repo's push range carry an LFS-tracked path at all", not the precise
#: per-path attribute-chain question `git_native._clean_filter_may_apply`
#: answers elsewhere; a nested `.gitattributes` declaring `filter=lfs`
#: with none at the root is the one case this arm under-detects, and arm 2
#: (a real `check-attr` call against the actual changed paths) is exactly
#: the safety net that catches it -- arm 1 only ever short-circuits to
#: "definitely nothing", never asserts "definitely something".
_LFS_FILTER_MARKER = "filter=lfs"


def _gitattributes_declares_lfs_filter(root: Path) -> bool:
    """Zero-spawn arm 1: does this repo's root `.gitattributes` declare
    `filter=lfs` anywhere (comments stripped)? `False` when the file is
    absent, empty, or unreadable -- absence of the file means absence of
    any LFS declaration, not an unknown."""
    try:
        text = (root / ".gitattributes").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for line in text.splitlines():
        code = line.split("#", 1)[0]
        if _LFS_FILTER_MARKER in code:
            return True
    return False


def _run_git(args: Sequence[str], cwd: Path, *, input_data: Optional[str] = None) -> Optional[str]:
    """One `git <args>` spawn, Windows-safe (`CREATE_NO_WINDOW`), returning
    stdout on a zero exit code or `None` on any failure (non-zero exit,
    spawn error, timeout) -- `None` is the caller's single "could not
    establish this, fall back to the safe default" signal, never
    distinguished further, since every caller here treats "could not tell"
    identically to "range touches LFS" (correctness first).

    RUNS IN BYTES MODE DELIBERATELY -- do not "simplify" this back to
    `text=True`. Python's text mode translates "
" to "

" on stdin on
    Windows, so `check-attr filter --stdin` received `asset.bin
`, took the
    CR as part of the filename, and answered `filter: unspecified` for a
    genuinely LFS-tracked path. That made the predicate report "range is
    clean" for a range full of LFS objects -- the one answer it must never
    get wrong. Guarded by
    `test_range_touches_lfs_paths_arm3_lfs_tracked_path_present`.
    """
    from coordinator_core.git.run import run_git

    result = run_git(
        args,
        cwd=str(cwd),
        input=None if input_data is None else input_data.encode("utf-8"),
    )
    if result.timed_out or result.returncode != 0:
        return None
    return result.stdout


def _range_touches_lfs_paths(root: Path, base_sha: str, head_sha_value: str) -> bool:
    """The AC7b predicate, in cost order: `False` only when arm 1 or arm 2
    can affirmatively rule LFS out; `True` on arm 3 (a real LFS-tracked
    path in the range) and on any arm that could not be evaluated
    (`_run_git` returning `None`) -- correctness first, never a silent
    skip on an inconclusive answer."""
    if not _gitattributes_declares_lfs_filter(root):
        return False

    diff_range = f"{base_sha}..{head_sha_value}"
    diff_output = _run_git(["diff", "--name-only", diff_range], root)
    if diff_output is None:
        return True
    paths = [line for line in diff_output.splitlines() if line.strip()]
    if not paths:
        return False

    check_output = _run_git(
        ["check-attr", "filter", "--stdin"], root, input_data="\n".join(paths) + "\n"
    )
    if check_output is None:
        return True
    saw_a_verdict = False
    for line in check_output.splitlines():
        line = line.rstrip("\r")
        _, sep, verdict = line.partition(": filter: ")
        if not sep:
            continue
        saw_a_verdict = True
        if verdict.strip() == "lfs":
            return True
    if not saw_a_verdict:
        return True
    return False


def _upstream_sha(repo: Union[str, Path], branch: str) -> Optional[str]:
    """The sha `refs/remotes/<remote>/<branch>` currently points at, for
    whichever configured remote carries that branch, or `None` if no remote
    carries it at all (including: no remotes configured, no `refs/remotes`
    directory yet). Tries every remote's loose ref first, then falls back to
    `packed-refs` for any remote/branch pair not found loose -- matching
    `git_state.head_sha`'s own loose-then-packed fallback shape.
    """
    common_dir = resolve_git_common_dir(repo)
    remotes_dir = common_dir / "refs" / "remotes"

    try:
        remote_dirs = sorted(p.name for p in remotes_dir.iterdir() if p.is_dir())
    except OSError:
        remote_dirs = []

    for remote_name in remote_dirs:
        ref_path = remotes_dir / remote_name / branch
        try:
            content = ref_path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if content:
            return content

    try:
        packed_text = (common_dir / "packed-refs").read_text(encoding="utf-8")
    except OSError:
        return None

    suffix = f"/{branch}"
    for line in packed_text.splitlines():
        if not line or line[0] in "#^":
            continue
        sha, _, ref_name = line.partition(" ")
        if ref_name.startswith("refs/remotes/") and ref_name.endswith(suffix):
            return sha
    return None


def push_outstanding(
    worktree_root: Union[str, Path],
    *,
    allow_protected_branch: bool = False,
    protected_branch_override_reason: Optional[str] = None,
) -> PushOutcome:
    """Push `worktree_root`'s current branch iff it is ahead of its own
    upstream tracking ref -- decided at zero git spawns (see module
    docstring), delegating the actual push to `push_with_retry` unmodified.

    `allow_protected_branch`/`protected_branch_override_reason` pass straight
    through to `push_with_retry` -- see that function's own docstring; this
    module adds no new override surface of its own.

    When HEAD itself is unresolvable (detached, or `.git/HEAD` unreadable),
    or the current branch name cannot be determined, the zero-spawn sha
    comparison cannot be performed -- this falls through to
    `push_with_retry`, which resolves the branch itself and declines with
    `push:branch-unresolvable` exactly as it would for any other caller. No
    outstanding-work decision is silently skipped by returning early here.

    On the outstanding-work path the AC7b range-touches-LFS-paths predicate
    is evaluated and its verdict recorded on `PushOutcome.skipped`
    (`push:lfs-range-clean` / `push:lfs-range-touched`); the push itself
    always runs `push_with_retry` unmodified. See the module docstring for
    why this observes rather than acts. A range with no established upstream
    skips the predicate entirely.

    THE LADDER OWNS ITS OWN DEADLINE (2026-08-26). This call passes
    `budget_secs=PUSH_RETRY_BUDGET_SECS`, so the push/fetch/rebase/re-push
    ladder stops ITSELF between attempts rather than being cut mid-leg by
    `ipc._timeout_for`'s dispatch guard. That guard is wall-clock and does
    not abort server-side execution, so firing it inside a `git push`
    produces an `unconfirmed` push that may still land; the ladder's own
    deadline is checked where the previous push was observed and nothing is
    in flight, so exhaustion reports a decided `failed` instead. The 30s
    dispatch guard remains as the outer backstop, and a breach of IT now
    means a real defect rather than an ordinary slow remote -- see
    `PUSH_RETRY_BUDGET_SECS` for the measurements behind the number.

    PM RULING, 2026-08-25: this call PAYS SYNCHRONOUSLY -- no detach is
    built, and none should be re-proposed without new measurement. The
    basis: post-C1 a push event runs ~146ms / ~9.5 spawns (79.7ms / 3.5 for
    the push itself, plus `push_with_retry`'s 66.4ms / 6 local half),
    falling to ~80ms / ~3.5 once AC4b lands -- comfortable against the
    500ms brightline for a synchronous caller. A detach, measured against
    `auto_push`'s own detached-vs-sync seam (claude-klabauter-59's job-object
    measurement, K=12), costs +130.2ms CPU and +6.8 procs to save 64.1ms of
    ONE session's wall -- under DR-344 (process time, never wall clock) at
    the 50-70-session load norm, that spends CPU everyone shares to buy
    latency only one session notices. Reusing `auto_push._detach_and_run`
    was considered and explicitly retired: it respawns a second Python that
    re-imports the engine to redo work the parent already loaded, which is
    where the +130ms goes. This amends the plan's original "lazy,
    non-blocking" phrasing rather than interpreting it -- the earlier
    justification (that laziness subsumes non-blocking) did not hold, since
    a cadence surface has commits accumulated by definition and the no-op
    case is the rare one; the laziness half survives intact and is free
    (C2, zero spawns).
    """
    root = Path(worktree_root)

    branch = head_branch(root)
    current_sha = head_sha(root) if branch is not None else None
    upstream_sha: Optional[str] = None

    if branch is not None and current_sha is not None:
        upstream_sha = _upstream_sha(root, branch)
        if upstream_sha is not None and upstream_sha == current_sha:
            return PushOutcome(exit_code=0, skipped=["push:nothing-outstanding"])

    lfs_note: list[str] = []
    if upstream_sha is not None and current_sha is not None:
        if _range_touches_lfs_paths(root, upstream_sha, current_sha):
            lfs_note.append("push:lfs-range-touched")
        else:
            lfs_note.append("push:lfs-range-clean")

    outcome = push_with_retry(
        root,
        allow_protected_branch=allow_protected_branch,
        protected_branch_override_reason=protected_branch_override_reason,
        budget_secs=PUSH_RETRY_BUDGET_SECS,
    )
    if lfs_note:
        outcome.skipped.extend(lfs_note)
    return outcome


# ---------------------------------------------------------------------------
# Registered op handler
# ---------------------------------------------------------------------------


@register_op("push.outstanding")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC 'push.outstanding' handler -- the cadence-surface entry point.

    This is the ONLY way the cadence surfaces reach `push_outstanding()`. Four
    of the six live in the DoE-claude repo and can call claude-klabauter solely through
    the op registry, so an unregistered primitive is unreachable by every
    caller this plan exists to serve.

    Parameters (params dict), all optional:
        allow_protected_branch (bool) -- passed straight through.
        protected_branch_override_reason (str) -- passed straight through.

    Neither adds an override surface of its own; see `push_with_retry`'s
    docstring for what they mean and what they still refuse.

    repo_root:
        The git common dir (from `_OP_KEY_SCOPE = "common_dir"`). The worktree
        root is derived via `main_worktree_root`, matching every other ceremony
        op rather than re-deriving it here.

    Returns the `PushOutcome` flattened onto the standard envelope, so a caller
    can tell the three zero-exit outcomes apart (landed / no remote / declined
    by policy) from `skipped` without reading the exit code alone -- and can see
    the AC7b LFS range verdict, which rides in `skipped` as
    `push:lfs-range-clean` or `push:lfs-range-touched`.

    Every `PushOutcome` field is carried, not just the zero-exit three. Until
    2026-08-27 this flattened `exit_code`/`acted`/`skipped` only, which made a
    FAILED push indistinguishable from a benign skip: the envelope read
    `{"exit_code": 1, "acted": [], "skipped": ["push:lfs-range-clean"]}`, whose
    sole legible content is an LFS verdict that has nothing to do with the
    failure, while the condensed git diagnostic sat in the discarded `failed`.
    On a box at the 50-70-concurrent-session load norm a reject from a peer's
    concurrent push is the ORDINARY case, so the unreportable path was also the
    common one -- twice in one session it was read as an ordinary no-op.

    `unconfirmed` matters most of the three added lists and is the reason this
    is not cosmetic: it is the "the transport leg may have outlived the killed
    parent, the commit may ALREADY be on the remote" signal
    (`state/bug-backlog/2026-08-19-push-retry-reports-push-failed-on-a-subp-
    4400dc2697d0.yaml`). Dropping it left every op-registry caller -- which is
    all six cadence surfaces, four of them in DoE-claude -- unable to tell a
    definite reject from an indeterminate one, the exact distinction that
    decides whether re-pushing is safe.

    Negative-spec: does NOT collapse `failed`/`unconfirmed` into one key or
    synthesize a summary string from them -- they are mutually exclusive by
    `PushOutcome`'s own contract and a caller must be able to branch on which
    one is populated.
    """
    if repo_root is None:
        return {
            "exit_code": 1,
            "error": (
                "push.outstanding: repo_root arg is None -- common_dir not supplied by "
                "engine (check _OP_KEY_SCOPE = 'common_dir')"
            ),
        }

    worktree_root = main_worktree_root(Path(repo_root))

    outcome = push_outstanding(
        worktree_root,
        allow_protected_branch=bool(params.get("allow_protected_branch") or False),
        protected_branch_override_reason=(
            params.get("protected_branch_override_reason") or None
        ),
    )

    return {
        "exit_code": outcome.exit_code,
        "acted": list(outcome.acted),
        "skipped": list(outcome.skipped),
        "failed": list(outcome.failed),
        "unconfirmed": list(outcome.unconfirmed),
        "message": outcome.message,
        "pushed_range": outcome.pushed_range,
        "pushed_count": outcome.pushed_count,
    }
