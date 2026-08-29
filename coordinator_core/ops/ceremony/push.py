"""
coordinator_core.ops.ceremony.push -- the push-with-retry subsystem,
extracted (C1, docs/plans/2026-08-29-the-push-subsystem-leaves-and-then-the-
pipeline-can-go.md) from `commit_pipeline.py`, which it previously shared a
file with rather than a dependency. IMPORT-ONLY move: no signature changes,
no renames, no behaviour change from the code as it stood in
`commit_pipeline.py`.

Hosts: the `PUSH_MODE_*` / `PUSH_STATUS_*` vocabularies, `PushOutcome`,
`push_with_retry` (reject-detect -> fetch -> rebase --onto -> re-push,
bounded, never `--force`), `derive_push_status` / `derive_pushed_tristate`,
`resolve_post_push_sha`, the GH013 push-protection sub-classification
(`_is_push_reject` and its secret-scanning / rule-violation sub-checks --
the spinoff's Finding 3, discharged here rather than in a separate chunk
because the predicate travels with the retry ladder it guards), and
`_drain_pending_push_after_sync`.

`commit_pipeline.py` re-imports what it still needs from this module; its
own callers are unaffected by this file existing.
"""

from __future__ import annotations

import logging
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple, Union

_MAX_DIAGNOSTIC_CHARS = 2000

_GIT_NOISE_LINE_RE = re.compile(r"^\s*(?:warning|hint):", re.IGNORECASE)


def condense_git_diagnostic(text: str, *, limit: int = _MAX_DIAGNOSTIC_CHARS) -> str:
    """Reduce a raw git stdout/stderr blob to the part that names the failure.

    2026-08-10 fix (live incident: four consecutive `scoped-git-commit`
    refusals reported nothing but CRLF line-ending warnings, hiding the
    then-installed pre-commit gate's own BLOCK that was the actual cause --
    that gate is deleted 2026-08-25, "the staged rollback gate dies without
    blocking a commit"; the condensation logic below is generic to ANY
    pre-commit hook's BLOCKED verdict, not specific to the deleted gate).
    Two properties of git's output defeat a naive head-truncation:

      - It leads with per-path advisory noise. `git add` on a batch of N
        LF-in-worktree files emits N `warning: ... LF will be replaced by
        CRLF` lines BEFORE anything diagnostic, so the first 200 characters
        of a large batch's stderr are noise by construction.
      - The diagnosis lands LAST. `fatal:`/`error:` from git, and a
        pre-commit hook's own BLOCKED verdict, are the final lines -- so
        when a blob must be cut, the tail is the half worth keeping.

    Hence: drop advisory lines when any non-advisory line survives (never
    return empty -- an all-advisory blob is preserved verbatim, since an
    empty reason is what the incident produced), then keep the TAIL under
    *limit*, marking the cut so a reader knows output preceded it.

    AC3/AC11 (C3b): this function adds NO block of its own. A `pre-commit`
    hook refusal is the hook's own verdict, reached before `git commit`
    ever returns to this pipeline -- this function only reformats stderr/
    stdout git already produced so the verdict is legible instead of
    hidden behind advisory noise (the incident above). Nothing here holds,
    retries, or waits; the outlet for a genuine hook BLOCK is whatever the
    hook itself grants (fix the flagged condition and re-commit), unowned
    by this module and out of this audit's scope.
    """
    stripped = text.strip()
    if not stripped:
        return ""

    lines = stripped.splitlines()
    signal = [ln for ln in lines if not _GIT_NOISE_LINE_RE.match(ln)]
    condensed = "\n".join(signal if signal else lines).strip()

    if len(condensed) <= limit:
        return condensed
    return "...(truncated) " + condensed[-limit:]


from coordinator_core.ipc import CEREMONY_BUDGET_SECS
from coordinator_core.lifecycle import git_common_dir
from coordinator_core.ops.ceremony import git_native

PUSH_MODE_SYNC = "sync"
PUSH_MODE_DEFERRED = "deferred"
PUSH_MODE_NONE = "none"
PUSH_MODE_NEVER = "never"

#: `push_mode` values under which `commit()` is told to stand the
#: `post-commit` hook's detached push down. Two different reasons land in
#: one set: "sync" because this pipeline publishes the commit itself a few
#: lines later (two publishers for one branch tip is what makes
#: `integrity_breach` racy -- `git_native._sole_publisher_env`), and "never"
#: because the commit is not to be published by anyone. Deliberately NOT
#: "deferred"/"none", where the hook is the only publisher there is.
_PUSH_MODES_SUPPRESSING_POST_COMMIT_HOOK = frozenset({PUSH_MODE_SYNC, PUSH_MODE_NEVER})

from coordinator_core.hooks.auto_push import branch_gate, classify_error, resolve_branch

#: Retry-worthy `auto_push.classify_error()` classifications for THIS
#: pipeline's specific recovery shape (fetch + `git rebase --onto` +
#: re-push) -- deliberately NOT the same set `auto_push.run_push_with_retry`
#: treats as retryable (`_RETRYABLE_CLASSES = {"ref-lock", "network",
#: "gh-transient"}`), because that retry is a bare re-send with backoff,
#: never a rebase. Only "non-fast-forward" is REBASE-recoverable: it means a
#: concurrent commit already landed upstream, and rebasing this session's
#: own commit range onto the fetched ref is the correct fix. "ref-lock" is a
#: LOCAL lock contention on the push-ref -- resolves in milliseconds on its
#: own; a fetch+rebase cycle does nothing for it and only adds a rebase this
#: pipeline would then have to run for no reason. "gh-transient" is a
#: server-side 5xx/disconnect -- also not a divergence, so rebasing is not
#: the fix (a bare retry with backoff would be, which this pipeline does not
#: implement here; see module docstring's push-with-retry scope). Every
#: other classification (gh-push-protection, gh-size-limit, gh-lfs-quota,
#: auth, gh-server-reject, network, spawn-error, unknown, empty-stderr) names a failure
#: no rebase addresses, so none of them belong in THIS set -- which is a
#: narrower question than whether `auto_push` re-sends them; it does re-send
#: several. See `classify_error`'s docstring in
#: `coordinator_core.hooks.auto_push` for the
#: ordered ladder this reuses verbatim, rather than a duplicated/patched
#: marker tuple (2026-08-07 fix: the old substring-based
#: `_PUSH_REJECT_MARKERS`, including a bare "rejected", over-matched a
#: GH013 push-protection refusal -- `remote: error: GH013 ... push
#: declined` is always accompanied by `! [remote rejected]` and `failed to
#: push some refs` -- driving three full fetch+rebase+re-push cycles for
#: something no rebase can ever fix).
#:
#: Import-direction note: `auto_push` lives under `coordinator_core.hooks`,
#: this module under `coordinator_core.ops.ceremony` -- checked for a cycle
#: back into `ops.ceremony` (none: no `hooks/*.py` module imports
#: `ops.ceremony` anything) before taking this dependency. The one real cost
#: is that importing `coordinator_core.hooks.auto_push` also runs
#: `coordinator_core.hooks/__init__.py` (Python always executes a parent
#: package's `__init__` before a submodule), which registers the `hooks.*`
#: ops as a side effect of that import. That side effect is idempotent,
#: additive (registers ops into a dict, no I/O, no mutation of shared state),
#: and already paid by any
#: process that imports `coordinator_core.hooks` for any other reason -- not
#: a new hazard this import introduces, just a real (small) cost worth
#: naming rather than a hard blocker; a shared module hosting only the
#: classification ladder (never importing `ops.ceremony`) would avoid even
#: that, but splitting `auto_push` for this one function is out of this
#: fix's two-file scope.
#:
#: `branch_gate`/`resolve_branch` (push-leg branch-policy fix, 2026-08-08)
#: ride the same import and the same rationale above: same module, same
#: acyclic direction, same already-paid `hooks/__init__.py` side effect.
#: `push_with_retry()` below consults `branch_gate` as a read-only oracle --
#: it does not extend it (see that function's own docstring for the
#: `work/*`-only policy this module now enforces on its own push leg).
_PUSH_RETRY_CLASSES = frozenset({"non-fast-forward"})
_PUSH_MAX_RETRIES = 3

#: END-TO-END deadline for one `push_with_retry` ladder, opt-in per call.
#:
#: THE JOB THIS DOES, and why a dispatch timeout could not do it. Before this
#: constant the ONLY bound on a push ladder was `ipc._timeout_for`'s wall-clock
#: `asyncio.wait_for` guard, and that guard can only fire MID-LEG -- inside a
#: `git push` whose outcome is then never observed. A dispatch timeout does not
#: abort server-side execution (`ipc.py`'s reconcile-before-retry negative spec),
#: so cutting a push there does not buy a decided failure, it manufactures a
#: `PushOutcome.unconfirmed` that may still land afterwards. That is the worst of
#: the three states and it is the one a runaway guard reaches for first.
#:
#: A deadline the ladder owns is checked BETWEEN attempts, where the state is
#: known: the last push was OBSERVED as rejected and nothing is in flight. So an
#: exhausted budget reports a genuine `failed`, never `unconfirmed`, and the
#: caller is not left with "unknown" (`docs/wiki/close-ceremony-residue.md`).
#:
#: SIZING, measured 2026-08-26 against `github.com` from this box, quiet:
#:     git ls-remote origin HEAD        p50 669.5ms  (min 610.3, max 701.3)
#:     git push --dry-run origin HEAD   p50 753.9ms  (min 610.1, max 796.4)
#: The full ladder is 3 pushes + 2 fetches = 5 network legs ~= 3.8s, plus a local
#: rebase and the ladder's own `rev-parse`/`rev-list` spawns. 12.0s is ~3x that
#: network cost, leaving room for the per-spawn scheduling tax the 50-70-session
#: load norm imposes -- a real cost, but NOT a term plugged into this number:
#: 12.0 is a flat literal chosen as ~3x the network estimate, not
#: `network + headroom`. Cited as why the multiplier is 3x rather than 1.5x.
#:
#: A CEILING, NEVER A TARGET, and deliberately well under the 30s dispatch guard
#: so THIS is what stops the ladder and the guard stays a backstop whose breach
#: means a real defect -- the same shape `ipc.py`'s `_OP_TIMEOUT_OVERRIDES` block
#: records for `percolate.build_token_index`. Ratchets DOWN only: the remedy for a
#: ladder that does not fit is a cheaper ladder, never a wider number here. The
#: network floor above is the one term nothing in this repo can shrink.
PUSH_RETRY_BUDGET_SECS: float = 12.0

#: The push budget for a CEREMONY op, which is a different job from the cadence
#: ladder above and deliberately carries a different number.
#:
#: `ipc.CEREMONY_BUDGET_SECS` bounds a whole `ceremony.*` op end to end at 2.0s
#: (DR-348, a ratchet with no per-op exception). The push is the LAST leg of that
#: op -- staging, gates, the commit itself and the reporting tail are already
#: paid by the time it runs -- so it cannot have the whole budget. The headroom
#: below is what those earlier legs and the tail need; the remainder is what the
#: push may spend.
#:
#: WHAT THIS NUMBER BUYS, read against the same 2026-08-26 measurements behind
#: `PUSH_RETRY_BUDGET_SECS` (~600-750ms for one network leg, quiet): exactly ONE
#: honest push attempt and no retry ladder. That is the correct shape for a
#: ceremony op rather than a shortfall in it. A ceremony op must either land the
#: push inside its budget or report cleanly and leave -- it may never occupy the
#: box retrying, because the ~50 peers queued behind it are the load norm
#: (`docs/wiki/machine-load-norm.md`). Work that genuinely needs the retry ladder
#: belongs on a cadence surface, which is what `push.outstanding` and
#: `PUSH_RETRY_BUDGET_SECS` exist to serve.
#:
#: THE DEFECT THIS CLOSES, and why it is not merely a tighter number: before it,
#: these ceremony push paths were bounded ONLY by the 2.0s dispatch clamp, which
#: `asyncio.wait_for` can fire only mid-leg -- inside a `git push` whose outcome
#: is then never observed, leaving an `unconfirmed` push that may still land
#: (`ipc.py`'s reconcile-before-retry negative spec). Landing under the ladder's
#: own deadline instead means the stop happens between attempts, where the state
#: is known and the outcome is decided. Same reasoning as
#: `PUSH_RETRY_BUDGET_SECS`, sized for a different caller.
_CEREMONY_PUSH_HEADROOM_SECS: float = 0.8
CEREMONY_PUSH_BUDGET_SECS: float = CEREMONY_BUDGET_SECS - _CEREMONY_PUSH_HEADROOM_SECS

def _ceremony_push_budget(pre_push_elapsed: Optional[float]) -> float:
    """The push ladder's budget for a ceremony op, measured from what the op
    has ALREADY SPENT rather than from a fixed slice of its ceiling.

    The fixed slice was wrong and the review caught it (2026-08-26, slice-3
    reviewer, P2). `push_with_retry` stamps its deadline at ITS OWN entry, so
    a flat `CEREMONY_PUSH_BUDGET_SECS` handed the push a full 1.2s ladder no
    matter how long staging, gates and the commit had already taken. Pre-push
    work over its 0.8s allowance therefore pushed the TOTAL past
    `CEREMONY_BUDGET_SECS`, the outer `asyncio.wait_for` fired mid-push, and
    the mid-flight cut this budget exists to prevent came back -- precisely in
    the slow-box case it was written for. The 0.8s figure was also never
    measured: it is `2.0 - 1.2`, the residual left after picking the push
    number, and the reviewer was right to refuse it as a load-tested ceiling.

    Measuring from actual elapsed time removes the need for that figure to be
    true. A slow pre-push phase now TIGHTENS the ladder instead of silently
    overrunning the ceiling, which is the direction that keeps the op inside
    its budget under exactly the load that threatens it.

    `None` (no measurement in hand) falls back to the flat slice -- the
    pre-2026-08-26 behaviour, and still bounded.

    Never returns <= 0: an op already over its ceiling gets `_CEREMONY_PUSH_
    FLOOR_SECS`, one honest attempt at the remote rather than a zero budget
    that refuses to try. The outer clamp is the backstop for a genuinely
    overrunning op; this function's job is to stop being the CAUSE of one.
    """
    if pre_push_elapsed is None:
        return CEREMONY_PUSH_BUDGET_SECS
    return max(CEREMONY_BUDGET_SECS - pre_push_elapsed, _CEREMONY_PUSH_FLOOR_SECS)


#: The least a ceremony push ladder is ever given. Below one network round
#: trip (~600-750ms measured 2026-08-26) a budget cannot buy an attempt, only
#: a guaranteed refusal, and a refusal that never touched the remote is worse
#: information than an attempt that failed.
_CEREMONY_PUSH_FLOOR_SECS: float = 0.5

#: Matches `git_native._git()`'s own synthesized `TimeoutExpired` stderr
#: ("git push ...: timed out after {timeout}s (...)") -- the ONE reason
#: string that means the push's true outcome was never observed at all,
#: as opposed to every other reason string here, which is git itself
#: reporting a result. `classify_error()` has no bucket for this (it
#: classifies real git stderr, and this text is synthesized by the
#: subprocess wrapper on a kill, not emitted by git) so it falls through
#: to "unknown" and is treated as a confirmed failure unless checked for
#: separately -- see `push_with_retry`'s own use of this pattern.
_PUSH_TIMEOUT_RE = re.compile(r"timed out after \d")


def _is_indeterminate_push_result(result: "git_native.GitResult") -> bool:
    """True iff *result* is a subprocess timeout, not an observed git failure.

    `returncode == -1` alone is not enough -- `_git()` also returns -1 for an
    `OSError` (git not on PATH), which IS a definite, observed failure, just
    not one git itself reported. Only the timeout text names a result that
    was never observed.
    """
    return result.returncode == -1 and bool(_PUSH_TIMEOUT_RE.search(result.stderr or ""))


def resolve_post_push_sha(worktree_root: Union[str, Path], pre_push_sha: Optional[str]) -> Optional[str]:
    """Re-resolve HEAD after a landed push, WITHOUT trusting a bare read.

    state/bug-backlog/2026-08-11-run-commit-pipeline-reports-a-concurrent-
    0a91ea7dc77b.yaml (P1): all three post-push call sites in this ceremony
    (`run_commit_pipeline` here, `consumed_handoff_stamp.
    _commit_and_push_follow_up`, `post_commit_tail.
    _commit_and_push_origin_stub_close`) used to adopt a bare
    `git rev_parse_head()` unconditionally once a push landed, to cover the
    case where `push_with_retry()` fetched + `git rebase --onto` the commit
    on a rejected push before re-pushing (which genuinely rewrites its sha).
    But that bare read fires on EVERY landed push, not just a rebase-retry --
    on a busy shared branch a peer's push landing in the window between our
    push completing and this read running was silently adopted as ours (3 of
    5 calls wrong, observed live). Shape (b) from the filed analysis: keep
    the re-read, but VERIFY it names our own commit before adopting it,
    rather than gating on a "did a rebase actually fire" signal that
    `PushOutcome` does not expose (shape (a) -- would need a new field
    threaded out of `push_with_retry`, more invasive, not less).

    Verification is by TREE identity, not the token-trailer `git log --grep`
    the agree branch (`commit()`, above) uses for its OWN pre-push
    resolution: that mechanism depends on the per-call `Commit-Token:`
    trailer minted inside `commit()`, which `CommitOutcome` does not carry
    back to any caller, and threading it out to three call sites (one of
    which -- the two `consumed_handoff_stamp.py` / `post_commit_tail.py`
    follow-up commits -- doesn't mint a token at all, using
    `git_native.commit_scoped` directly) is exactly the "new plumbing" (b)
    is supposed to avoid. Tree identity needs none: a `rebase --onto` that
    only moves a commit's PARENT (the only kind `push_with_retry` performs)
    reapplies the identical diff and so produces an identical tree; a
    concurrent peer commit landing in the race window carries a DIFFERENT
    diff and so a different tree. Tree identity is checked SECOND, behind an
    ancestry check, because it cannot see an EMPTY peer commit: an empty
    commit inherits its parent's tree verbatim, so a peer `--allow-empty`
    landing on top of ours matches our tree exactly and a tree-only check
    would adopt it. Ancestry separates the two cleanly in every case — a
    rebase rewrites our commit and so drops it out of the new tip's history,
    while anything built on top of ours necessarily keeps it as an ancestor.
    `pre_push_sha` is the caller's own
    already-verified value (`commit_outcome.committed_sha` in
    `run_commit_pipeline`; the pre-push `rev_parse_head()` capture in the
    other two) -- the anchor this function either confirms or falls back to,
    never a value it invents.

    Returns `pre_push_sha` unchanged (the safe default) when: `pre_push_sha`
    is `None` (nothing to anchor against); the post-push read fails or is
    blank (today's existing fallback, unaffected by this fix); or the two
    trees disagree (peer race -- keep our own known-good value rather than
    adopting a stranger's). Returns the freshly-read HEAD sha only when it
    equals `pre_push_sha` outright (the ordinary non-rebase case) or its
    tree matches `pre_push_sha`'s (a genuine rebase-retry).
    """
    if pre_push_sha is None:
        return pre_push_sha
    post_push = git_native.rev_parse_head(worktree_root)
    if not post_push.ok:
        return pre_push_sha
    post_push_sha = post_push.stdout.strip()
    if not post_push_sha:
        return pre_push_sha
    if post_push_sha == pre_push_sha:
        return post_push_sha
    # Ancestry first, because the tree check below cannot see the empty-commit
    # case: an empty commit inherits its parent's tree verbatim, so a peer's
    # `--allow-empty` (or otherwise no-op) commit landing on top of ours has a
    # tree IDENTICAL to ours and a tree-only check would adopt its sha. The
    # rebase this whole re-read exists for REWRITES our commit, so the pre-push
    # sha stops being reachable from the new tip; anything that still carries it
    # as an ancestor was built ON TOP of ours and is therefore not ours.
    base = git_native.merge_base(worktree_root, pre_push_sha, post_push_sha)
    if base.ok and base.stdout.strip() == pre_push_sha:
        return pre_push_sha
    pre_tree = git_native.rev_parse(worktree_root, f"{pre_push_sha}^{{tree}}")
    post_tree = git_native.rev_parse(worktree_root, f"{post_push_sha}^{{tree}}")
    if not pre_tree.ok or not post_tree.ok:
        return pre_push_sha
    pre_tree_sha = pre_tree.stdout.strip()
    post_tree_sha = post_tree.stdout.strip()
    if pre_tree_sha and pre_tree_sha == post_tree_sha:
        return post_push_sha
    return pre_push_sha


# ---------------------------------------------------------------------------
# Push-with-retry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PushOutcome:
    """Typed result of `push_with_retry()`.

    Fields:
        exit_code -- 0 iff the push genuinely LANDED on the remote, or was
            skipped because no remote is configured, or was DECLINED by
            branch policy. `exit_code == 0` no longer by itself implies
            "landed or nothing to sync" -- a policy decline is also 0, and
            is a genuinely new "did not push, on purpose" outcome. Check
            `skipped` to tell the three apart.
        acted -- `["push"]` on a landed push, else empty.
        skipped -- now has more than one member: `["push:no-remote"]` when
            no remote is configured; `["push:branch-policy"]` when
            `auto_push.branch_gate()` declined the current branch;
            `["push:branch-unresolvable"]` when the branch itself could not
            be resolved (detached HEAD, or the git call failed) -- treated
            as a decline, not a silent proceed-to-push, because a push leg
            that cannot name its own branch is exactly the case the policy
            gate exists to catch. Otherwise empty.
        failed -- non-empty only on a genuine, OBSERVED push failure (git
            itself reported a reject after exhausting retries, or a
            fetch/rebase retry step itself failed with a real result).
            Mutually exclusive with `unconfirmed` -- exactly one of the two
            is non-empty on a non-landed, non-declined, non-no-remote
            outcome.
        unconfirmed -- non-empty ONLY when the push's true outcome was never
            observed at all -- the git subprocess itself timed out
            (`_is_indeterminate_push_result`), not merely rejected. This is
            NOT a softened `failed`: the commit may already be on the
            remote (the transport leg can outlive the killed parent, see
            `state/bug-backlog/2026-08-19-push-retry-reports-push-failed-
            on-a-subp-4400dc2697d0.yaml`), so reporting it as a confirmed
            failure invites a re-push/amend/force-push that is more
            dangerous than the uncertainty itself. A GENUINE git-reported
            reject (non-fast-forward after retries exhausted, permission
            denied, and the rest of `classify_error`'s ladder) still lands
            in `failed`, never here.
        message -- the verbatim `branch_gate()` skip message on a
            `push:branch-policy` decline, carried unaltered so a later
            consumer can print exactly what `branch_gate` produced without
            regenerating or rewording it (the two surfaces must not drift).
            `None` on every other outcome, including
            `push:branch-unresolvable` (there is no `branch_gate` message
            to carry -- the branch was never resolved to hand it one).
        pushed_range -- (C3b, AC7) on a landed push (`acted == ["push"]`),
            the `<old-sha>..<new-sha>` range this call actually pushed,
            where `<old-sha>` is the upstream tip resolved AFTER the branch
            policy/no-remote checks passed but BEFORE this call's first
            `git push` -- or, if a reject triggered the fetch+rebase retry
            loop, the upstream tip as re-resolved right after that fetch
            (see `push_with_retry`'s own docstring for why: commits between
            the original pre-loop tip and the freshly-fetched one already
            reached the remote via someone else's push and were not landed
            by THIS call). `None` when the range could not be resolved --
            most notably a first push with no upstream tracking ref yet --
            which is a distinct, explicit "unknown", never a stand-in for
            "nothing pushed" (that case is `acted == []`, not this field).
            Always `None` on every non-landed outcome.
        pushed_count -- (C3b, AC7) the commit count for `pushed_range`
            (`git rev-list --count <pushed_range>`), or `None` under the
            exact same "unknown, not zero, not omitted" rule as
            `pushed_range` -- read together, never independently.
    """

    exit_code: int
    acted: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    unconfirmed: List[str] = field(default_factory=list)
    message: Optional[str] = None
    pushed_range: Optional[str] = None
    pushed_count: Optional[int] = None


# ---------------------------------------------------------------------------
# Canonical push_status vocabulary
#
# Disk already carries two prior vocabularies for this idea before this
# module claims ownership: `scoped_git_commit.py`'s `PUSH_STATE_*` constants
# (`PUSH_STATE_PUSHED = "pushed"`, `PUSH_STATE_FAILED = "push-failed"` --
# kebab-case, NOT `"failed"` -- `PUSH_STATE_UNCONFIRMED = "unconfirmed"`,
# `PUSH_STATE_NO_REMOTE = "no-remote"`) and `wsc_tail.py`'s own `push_status`
# field (`"deferred" | "pushed" | "failed" | "unknown_resumed"`, snake_case
# on the last member). This module owns the canonical set below; both of
# those modules import from here rather than re-deriving their own spelling.
#
# Mapping table (the spec C5/C6 execute against, not a suggestion):
#
#   canonical PUSH_STATUS_*  | scoped_git_commit.PUSH_STATE_*  | wsc_tail.push_status
#   ------------------------ | -------------------------------- | ---------------------
#   "pushed"                 | PUSH_STATE_PUSHED                | "pushed"
#   "push-failed"            | PUSH_STATE_FAILED                | "failed"
#   "declined"               | PUSH_STATE_DECLINED (new, C5)     | new member (C6b)
#   "no-remote"               | PUSH_STATE_NO_REMOTE            | no counterpart today (C6b adds one)
#   "not-attempted"           | PUSH_STATE_UNCONFIRMED (closest existing meaning --
#                               NOT identical: PUSH_STATE_UNCONFIRMED also covers the
#                               detached-auto-push-race remote-probe outcome, which
#                               "not-attempted" never does)
#                                                                | "deferred" (under the
#                                                                  async push_mode contract)
#   "unconfirmed" (new, FIX-I)| PUSH_STATE_UNCONFIRMED           | no counterpart today
#                               -- the push subprocess itself timed out, so its
#                               true outcome was never observed. Distinct from
#                               "not-attempted": a push WAS attempted here.
#   "cadence-pending" (new,    | no counterpart                  | new member,
#    C5, DR-329 AC9c)          |                                  | `wsc_tail`-owned
#                               -- a commit made under the cadence regime
#                               (`push_mode=PUSH_MODE_NEVER`) whose publish
#                               obligation is deliberately deferred to a
#                               NAMED future cadence checkpoint's own
#                               `push_outstanding()` call, never to this
#                               pipeline's own push leg. Distinct from
#                               "not-attempted": that spelling is silent on
#                               WHETHER anything will ever publish this
#                               commit; "cadence-pending" asserts a
#                               checkpoint will. Distinct from "deferred"
#                               (the async post-commit-hook race window,
#                               `compute_push_landed_gate`'s own docstring):
#                               "deferred" means a detached push CHILD may
#                               already be in flight and a re-check will
#                               resolve it soon; "cadence-pending" means no
#                               push has been attempted or started at all,
#                               and none will be until the next checkpoint.
#                               This pipeline itself never returns this
#                               value -- `run_commit_pipeline` under
#                               `push_mode=PUSH_MODE_NEVER` still reports
#                               `PUSH_STATUS_NOT_ATTEMPTED` (unchanged, see
#                               the module docstring's retired-instruction
#                               note above) -- the cadence-aware surfaces
#                               that KNOW a checkpoint will follow (`wsc_
#                               tail.py` / `directives_commit_tail.py`) are
#                               the ones that promote `not-attempted` to
#                               this richer member at their own reporting
#                               layer.
#
# `wsc_tail.py`'s `"unknown_resumed"` (any resumed/crash-recovered pass) has
# NO counterpart in this canonical set -- it is preserved as its own member
# on that module's side, derived from resumption bookkeeping this pipeline
# does not have visibility into, not from `push_status`. It must not be
# silently dropped when C6 reconciles that module's vocabulary against this
# one.
# ---------------------------------------------------------------------------

PUSH_STATUS_PUSHED = "pushed"
PUSH_STATUS_FAILED = "push-failed"
PUSH_STATUS_DECLINED = "declined"
PUSH_STATUS_NO_REMOTE = "no-remote"
PUSH_STATUS_NOT_ATTEMPTED = "not-attempted"
#: FIX-I (2026-08-19): the push subprocess timed out -- its true outcome was
#: never observed, distinct from `PUSH_STATUS_FAILED` (git itself reported a
#: reject). See `PushOutcome.unconfirmed`'s docstring for why this must not
#: collapse into `push-failed`.
PUSH_STATUS_UNCONFIRMED = "unconfirmed"
#: C5 (2026-08-25, DR-329 AC9c) -- the canonical member for a commit whose
#: publish obligation is deliberately deferred to a named future cadence
#: checkpoint (see the mapping-table comment above for the full contract
#: and how this differs from both `PUSH_STATUS_NOT_ATTEMPTED` and
#: `compute_push_landed_gate`'s own `"deferred"` short-circuit). Never
#: returned by `run_commit_pipeline` itself -- see the same comment.
PUSH_STATUS_CADENCE_PENDING = "cadence-pending"


def derive_push_status(push_outcome: Optional[PushOutcome]) -> str:
    """Derive the canonical `push_status` from a `PushOutcome` (or None).

    This is the supported way to map a `PushOutcome` onto the canonical
    `push_status` vocabulary -- promoted from a leading-underscore private
    (2026-08-08, C7a) once `post_commit_tail.py` and `consumed_handoff_
    stamp.py` were found importing it across the module boundary despite the
    underscore; a private name crossing a module boundary is a contract
    with no name.

    Rule: `push:branch-policy` or `push:branch-unresolvable` in `skipped` ->
    `declined`; `push:no-remote` in `skipped` -> `no-remote`; `acted ==
    ["push"]` -> `pushed`; non-empty `unconfirmed` -> `unconfirmed` (checked
    BEFORE `failed` -- see below); non-empty `failed` -> `push-failed`; push
    never reached (outcome is `None`) -> `not-attempted`.

    `unconfirmed` is checked before `failed` on purpose, though
    `PushOutcome`'s own docstring documents the two fields as mutually
    exclusive (`push_with_retry` never populates both on the same outcome):
    ordering the indeterminate check first means a future caller that
    accidentally sets both never has the indeterminate case silently lost
    to the failure branch -- the direction of error this fix exists to
    close, so the safer of two equally-cheap orderings is deliberate here.
    """
    if push_outcome is None:
        return PUSH_STATUS_NOT_ATTEMPTED
    if "push:branch-policy" in push_outcome.skipped or (
        "push:branch-unresolvable" in push_outcome.skipped
    ):
        return PUSH_STATUS_DECLINED
    if "push:no-remote" in push_outcome.skipped:
        return PUSH_STATUS_NO_REMOTE
    if push_outcome.unconfirmed:
        return PUSH_STATUS_UNCONFIRMED
    if push_outcome.failed:
        return PUSH_STATUS_FAILED
    if "push" in push_outcome.acted:
        return PUSH_STATUS_PUSHED
    return PUSH_STATUS_NOT_ATTEMPTED


def _pushed_range_diagnostic(push_outcome: PushOutcome) -> str:
    """Format the AC7 landed-push diagnostics line from a `PushOutcome`.

    Only called when `derive_push_status(push_outcome) == PUSH_STATUS_PUSHED`
    -- i.e. `push_outcome.pushed_range`/`pushed_count` are the landed-push
    fields, not the always-`None` defaults every other outcome carries.
    """
    if push_outcome.pushed_range is None:
        return "run_commit_pipeline: push landed -- pushed range could not be resolved"
    count_part = (
        str(push_outcome.pushed_count) if push_outcome.pushed_count is not None else "unknown"
    )
    return (
        f"run_commit_pipeline: push landed -- {count_part} commit(s), "
        f"range {push_outcome.pushed_range}"
    )


def _is_push_reject(reason: str) -> bool:
    """True iff `reason` classifies as a rebase-recoverable push reject.

    Routes through `auto_push.classify_error()` -- the same ordered,
    most-specific-first ladder that already correctly distinguishes a GitHub
    push-protection/branch-protection refusal (GH013) from a genuine
    non-fast-forward reject -- rather than a bespoke substring test. Only
    `_PUSH_RETRY_CLASSES` (see that constant's own docstring) trigger the
    fetch+rebase+re-push cycle below; a GH013 stderr still contains
    "failed to push some refs", so a bare-marker test previously misfired
    here (2026-08-07 fix).
    """
    return classify_error(reason) in _PUSH_RETRY_CLASSES


#: Sub-classifies a `gh-push-protection` rejection (C2, docs/plans/2026-08-27-
#: the-merge-gate-gets-a-remote-authority-layer.md § C2). `auto_push.
#: classify_error()` deliberately folds a GH013 RULE-VIOLATION refusal (the
#: coverage-gate ruleset this plan adds) and a GH013 SECRET-SCANNING refusal
#: into the SAME "gh-push-protection" class -- correct for that module's own
#: purpose (both are equally non-rebase-recoverable), but wrong for THIS
#: pipeline's recovery: a secret-scanning refusal must never be re-pushed
#: (the secret is still in the commit), while a rule-violation refusal is
#: exactly the case C1's status-poster exists to recover -- posting a fresh
#: coverage-gate status and re-pushing the SAME, already-uploaded objects.
#: Matched on the same phrasing GitHub's own secret-scanning refusal uses
#: ("push cannot contain secrets", "Secret detected", "Push Protection");
#: anything classified `gh-push-protection` that does NOT match this is
#: treated as the rule-violation sub-class, since those are the only two
#: refusals `_PAT_GH_PUSH_PROTECTION` (auto_push.py) matches in the first
#: place.
_PAT_SECRET_SCANNING_REJECT = re.compile(
    r"(push cannot contain secrets|Secret detected|Push Protection)"
)


def _is_secret_scanning_reject(reason: str) -> bool:
    """True iff a `gh-push-protection` rejection is GitHub secret scanning,
    never rebase- or recovery-eligible (see `_PAT_SECRET_SCANNING_REJECT`)."""
    return classify_error(reason) == "gh-push-protection" and bool(
        _PAT_SECRET_SCANNING_REJECT.search(reason)
    )


def _is_rule_violation_reject(reason: str) -> bool:
    """True iff a `gh-push-protection` rejection is the coverage-gate RULE
    VIOLATION sub-class (GH013 from the ruleset this plan adds), eligible for
    the post-and-re-push recovery below -- never the secret-scanning
    sub-class (see `_is_secret_scanning_reject`)."""
    return classify_error(reason) == "gh-push-protection" and not _is_secret_scanning_reject(
        reason
    )


#: Parses `owner/repo` out of a `git remote get-url origin` value -- both the
#: HTTPS (`https://github.com/<owner>/<repo>.git`) and SSH
#: (`git@github.com:<owner>/<repo>.git`) forms, `.git` suffix optional.
_PAT_GITHUB_REMOTE = re.compile(
    r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$"
)


def _resolve_github_owner_repo(root: Path) -> Optional[Tuple[str, str]]:
    """Resolve `(owner, repo)` from this worktree's `origin` remote URL.

    Zero ambient config, no env var -- reads the one remote this pipeline
    already pushes to. Returns `None` (never guesses) when the remote is
    missing, unreadable, or not a github.com remote -- the C2 recovery path
    below must fail closed on that, exactly like C1's own token resolution.
    """
    result = git_native._git(["remote", "get-url", "origin"], cwd=root)
    if not result.ok:
        return None
    url = result.stdout.strip()
    match = _PAT_GITHUB_REMOTE.search(url)
    if not match:
        return None
    return match.group(1), match.group(2)


def _recover_rule_violation_reject(
    root: Path, pre_push_upstream_sha: Optional[str]
) -> Optional[str]:
    """Post a fresh coverage-gate status and clear the way for a re-push
    (C2, the rule-violation sub-class only -- see `_is_rule_violation_reject`).

    The two-phase sequence the plan's spike found: a GH013 rule-violation
    refusal still uploads the objects server-side, so the fix is not a
    rebase -- it is (re-)posting the `coverage-gate` status for the CURRENT
    tip, immediately before the next re-push, so the status is always a
    function of the sha about to be pushed.

    Returns `None` on a posted, PASS-mapped ("success") status -- the
    caller may proceed to re-push. Returns a non-`None` failure reason
    string for every other outcome (posting failed/unpostable, no owner/repo
    resolvable, no upstream tip to range from, or a posted `failure` state)
    -- NEVER a silent skip, and the caller must route that reason to
    `PushOutcome.failed`, never `unconfirmed` (this outcome was observed,
    not indeterminate -- see `push_with_retry`'s own docstring on that
    distinction).
    """
    from coordinator_core.ops import post_coverage_status as _post_coverage_status_mod

    head_result = git_native.rev_parse_head(root)
    if not head_result.ok or not head_result.stdout.strip():
        return "rule-violation recovery: could not resolve HEAD sha to post a status for"
    head_sha = head_result.stdout.strip()

    if not pre_push_upstream_sha:
        return "rule-violation recovery: no upstream tip resolvable to compute a coverage range from"

    owner_repo = _resolve_github_owner_repo(root)
    if owner_repo is None:
        return "rule-violation recovery: could not resolve owner/repo from the origin remote"
    owner, repo = owner_repo

    commit_range = f"{pre_push_upstream_sha}..{head_sha}"
    result = _post_coverage_status_mod.post_coverage_status(
        owner, repo, head_sha, commit_range, repo_root=str(root)
    )
    if not result.posted:
        return f"rule-violation recovery: coverage status unpostable ({result.reason})"
    if result.state != "success":
        return (
            f"rule-violation recovery: coverage-gate verdict is red for {commit_range} "
            f"({result.reason})"
        )
    return None


def _rebase_onto_fetched_ref(worktree_root: Path, upstream_ref: str) -> Tuple[int, str]:
    """Rebase THIS session's own commit range onto the freshly-fetched `upstream_ref`.

    Scoping is load-bearing: computes `merge-base(HEAD, upstream_ref)` BEFORE
    the rebase and passes it as the exclusive lower bound to
    `git rebase --onto <upstream_ref> <merge-base> HEAD` -- replays only the
    commits unique to HEAD since it diverged from upstream. Never a bare
    `git pull --rebase` / bare `git rebase` on the shared branch.

    Returns `(exit_code, reason)`. `exit_code == 0` -> rebase landed cleanly.
    On a rebase failure, aborts the half-applied rebase so the working tree
    is left clean for the caller.

    Dirty-worktree pre-check (2026-08-07 fix): `git rebase --onto` refuses
    outright on a dirty worktree, and on a shared-fleet box the worktree is
    essentially never clean (peer sessions churn state ledgers
    continuously) -- so the rebase recovery always failed, surfacing only
    an opaque `git rebase: <raw git stderr>` that named the SYMPTOM (a
    rebase refusal), not the actual, distinctly-diagnosable CAUSE (dirty
    worktree). Detected here, BEFORE attempting the rebase, via
    `git_native.status_porcelain()` (the same native seam
    `commit_gates.dirty_tree_gate` already uses for this exact question --
    never a raw `git status` shell-out). A porcelain check that itself
    fails to run (indeterminate) falls through to the ordinary rebase
    attempt below rather than guessing dirty/clean -- the pre-existing
    rebase-failure path still covers that case, just without this
    pre-check's more specific reason.
    """
    status_result = git_native.status_porcelain(worktree_root)
    if status_result.ok and status_result.stdout.strip():
        return (
            1,
            "rebase recovery cannot run: worktree has uncommitted changes "
            "(git rebase --onto refuses on a dirty worktree)",
        )

    mb_result = git_native.merge_base(worktree_root, "HEAD", upstream_ref)
    if not mb_result.ok:
        reason = condense_git_diagnostic(mb_result.stderr) or "merge-base failed"
        return mb_result.returncode or 1, f"git merge-base: {reason}"
    merge_base_sha = mb_result.stdout.strip()
    if not merge_base_sha:
        return 1, "git merge-base: empty output"

    rebase_result = git_native.rebase_onto(worktree_root, upstream_ref, merge_base_sha)
    if rebase_result.ok:
        return 0, ""
    reason = (
        condense_git_diagnostic(rebase_result.stderr)
        or condense_git_diagnostic(rebase_result.stdout)
        or f"exit_code={rebase_result.returncode}"
    )
    git_native.rebase_abort(worktree_root)
    return rebase_result.returncode or 1, f"git rebase: {reason}"


def _emit_push_policy_line(
    kind: str,
    *,
    branch: Optional[str] = None,
    message: Optional[str] = None,
    reason: Optional[str] = None,
) -> None:
    """Single owner of every push-policy operator-visible stderr line (C3, AC6/AC14).

    `push_with_retry()`'s branch-policy decisions used to be a boolean the
    operator may never read -- `PushOutcome.skipped`/`message`, inspected
    only if a caller happens to log it. `auto_push.main()` already prints
    its own `branch_gate()` skip message to stderr at the moment of the
    decision (see that module's `main()`); this function gives
    `commit_pipeline`'s push leg the same "print at decision time" shape,
    with one arm per case rather than scattering `print(..., file=sys.
    stderr)` calls across `push_with_retry()`:

      "declined-policy" -- `branch_gate()` declined the current branch.
          `message` is REQUIRED and is printed VERBATIM -- the exact string
          `branch_gate()` produced, never rebuilt or reworded here. If this
          module regenerated its own phrasing instead of carrying the
          gate's own text through, the two surfaces would drift in wording
          while agreeing in policy -- the failure this function exists to
          prevent (see `PushOutcome.message`'s own docstring).
      "declined-unresolvable" -- the branch itself could not be resolved
          (detached HEAD, or the `git` call failed), so there was no branch
          to hand `branch_gate()` and therefore no gate message to carry.
          Authors its own line naming that condition -- the ONLY arm that
          does not pass along someone else's exact text, because no such
          text exists on this path.
      "override-exercised" -- NOT YET CALLED as of C3 (this chunk). C4a
          introduces `allow_protected_branch=True`, which skips
          `branch_gate()` entirely and pushes a protected branch on
          purpose; when it lands, its call site fires this arm, naming
          `branch` and the caller-supplied `reason` (if any). Authored now,
          as a real callable arm with real tests, specifically so C4a does
          not have to invent this from scratch -- this is judged the MORE
          serious of the two silences this helper closes: an unlogged
          push TO a protected branch, not merely an unlogged decision not
          to push one. Leaving it for a later chunk to bolt on ad hoc is
          exactly how it would end up silent in practice.

    Every arm prints to stderr only -- this module never blocks a commit or
    a push on operator visibility, matching `auto_push`'s own "always exits
    0" posture for the equivalent decision.
    """
    if kind == "declined-policy":
        # Review: coordinator:code-reviewer -- message is documented REQUIRED
        # for this arm; enforce the precondition instead of letting a future
        # caller silently print the literal string "None" to stderr.
        if message is None:
            raise ValueError(
                "_emit_push_policy_line: kind='declined-policy' requires message"
            )
        print(message, file=sys.stderr)
    elif kind == "declined-unresolvable":
        print(
            "coordinator-ceremony: push declined -- current branch could not be "
            "resolved (detached HEAD, or git failed to report it), so the "
            "work/*-only branch policy could not be evaluated; push manually "
            "if intended.",
            file=sys.stderr,
        )
    elif kind == "override-exercised":
        reason_part = f" -- reason: {reason}" if reason else ""
        print(
            f"coordinator-ceremony: pushing {branch} -- branch policy gate "
            f"OVERRIDDEN for this push{reason_part}",
            file=sys.stderr,
        )
    else:
        raise ValueError(f"_emit_push_policy_line: unknown kind {kind!r}")


def _read_git_config_text(root: Path) -> str:
    """Best-effort `.git/config` text for *root*'s COMMON dir, `""` on any
    read failure -- never raises. Feeds `_remote_configured_locally` /
    `_resolve_upstream_local` (C2b, `push_with_retry`'s local-half spawn
    elimination): both only need PRESENCE of a section and two scalar keys,
    not a faithful `configparser`-equivalent read, so a tolerant line-based
    scan is sufficient here where `coordinator_core.git.remote_url` (module
    docstring, "Derivation method") correctly declines to build one for the
    URL-with-rewrites case. A `[include]`/`[includeIf]`-sourced remote or
    upstream, or a quoted section name containing `\"`/`\\`, is NOT resolved
    by this scan -- degrades to the same `push:no-remote`/unresolvable-
    upstream decline paths `git`-backed reads would otherwise reach, never a
    silent wrong answer.
    """
    try:
        return (git_common_dir(root) / "config").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


_REMOTE_SECTION_RE = re.compile(r'(?im)^[ \t]*\[remote\s+"')


def _remote_configured_locally(root: Path) -> bool:
    """True iff `.git/config` declares at least one `[remote "..."]` section
    -- the in-process equivalent of `git remote`'s "any output" check,
    0 spawns (module docstring's `_read_git_config_text`).
    """
    return bool(_REMOTE_SECTION_RE.search(_read_git_config_text(root)))


class _UpstreamInfo(Tuple[str, str, str]):
    """`(abbrev, remote_name, ref_path)` -- `abbrev` matches what
    `git rev-parse --abbrev-ref --symbolic-full-name @{u}` reports (e.g.
    `"origin/main"`, the form `_rebase_onto_fetched_ref` and the
    `remote_name = upstream_ref.split(...)` derivation both need),
    `ref_path` is the refs/remotes path `_ref_sha_local` resolves
    (`"refs/remotes/origin/main"`).
    """

    __slots__ = ()

    def __new__(cls, abbrev: str, remote_name: str, ref_path: str) -> "_UpstreamInfo":
        return super().__new__(cls, (abbrev, remote_name, ref_path))

    @property
    def abbrev(self) -> str:
        return self[0]

    @property
    def remote_name(self) -> str:
        return self[1]

    @property
    def ref_path(self) -> str:
        return self[2]


#: NOT `(?i)`. The keyword `branch` is case-insensitive in git config, but a
#: SUBSECTION name is case-sensitive -- `[branch "Main"]` and `[branch "main"]`
#: are two distinct sections. An `(?i)` here spanned the interpolated name and
#: would have resolved whichever section came first, naming the wrong upstream
#: for one of two branches differing only in case: a confidently wrong answer,
#: not the safe decline every other unparseable shape degrades to. The keyword's
#: own case-insensitivity is kept by spelling it as a character class.
#: NOT `(?i)`. The keyword `branch` is case-insensitive in git config, but a
#: SUBSECTION name is case-sensitive -- `[branch "Main"]` and `[branch "main"]`
#: are two distinct sections. An `(?i)` here spanned the interpolated name and
#: would have resolved whichever section came first, naming the wrong upstream
#: for one of two branches differing only in case: a confidently wrong answer,
#: not the safe decline every other unparseable shape degrades to. The keyword's
#: own case-insensitivity is kept by spelling it as a character class.
_BRANCH_SECTION_RE_TEMPLATE = r'(?ms)^[ \t]*\[[bB][rR][aA][nN][cC][hH]\s+"{name}"\][ \t]*$(.*?)(?=^[ \t]*\[|\Z)'
_BRANCH_REMOTE_KEY_RE = re.compile(r'(?im)^[ \t]*remote[ \t]*=[ \t]*(\S+)')
_BRANCH_MERGE_KEY_RE = re.compile(r'(?im)^[ \t]*merge[ \t]*=[ \t]*(\S+)')


def _resolve_upstream_local(root: Path, branch: str) -> Optional[_UpstreamInfo]:
    """`(abbrev, remote_name, ref_path)` for *branch*'s configured upstream,
    read straight from `.git/config`'s `[branch "<branch>"]` section --
    0 spawns, the in-process equivalent of `git rev-parse --abbrev-ref
    --symbolic-full-name @{u}`. `None` when `branch` has no configured
    upstream (or the section/keys are unparseable by this tolerant scan --
    see `_read_git_config_text`'s docstring), same "unresolvable" outcome
    the spawning form reached via a non-zero `rev-parse`.
    """
    section_re = re.compile(_BRANCH_SECTION_RE_TEMPLATE.format(name=re.escape(branch)))
    # LAST wins, at both levels, because that is what git does for a scalar key.
    # A config carrying `[branch "x"]` twice -- hand-edited, or appended to
    # rather than rewritten -- resolves under `@{u}` to the LAST `remote`/`merge`
    # value, so taking the first block (or the first key within a block) reads a
    # stale-but-well-formed value that nothing downstream can flag. That is a
    # wrong answer rather than a safe decline, and a wrong answer is the one
    # failure direction this in-process read is not allowed to have.
    sections = list(section_re.finditer(_read_git_config_text(root)))
    if not sections:
        return None
    section = sections[-1].group(1)
    remote_matches = list(_BRANCH_REMOTE_KEY_RE.finditer(section))
    merge_matches = list(_BRANCH_MERGE_KEY_RE.finditer(section))
    remote_match = remote_matches[-1] if remote_matches else None
    merge_match = merge_matches[-1] if merge_matches else None
    if remote_match is None or merge_match is None:
        return None
    remote_name = remote_match.group(1)
    merge_ref = merge_match.group(1)
    if merge_ref.startswith(_HEADS_PREFIX_LOCAL):
        branch_basename = merge_ref[len(_HEADS_PREFIX_LOCAL):]
    else:
        branch_basename = merge_ref.rsplit("/", 1)[-1]
    if not remote_name or not branch_basename:
        return None
    return _UpstreamInfo(
        f"{remote_name}/{branch_basename}",
        remote_name,
        f"refs/remotes/{remote_name}/{branch_basename}",
    )


_HEADS_PREFIX_LOCAL = "refs/heads/"


def _ref_sha_local(root: Path, ref: str) -> Optional[str]:
    """Resolve *ref* (e.g. `"refs/remotes/origin/main"`) to its sha with no
    `git` spawn: the loose ref file first, `packed-refs` on a miss -- same
    two-step resolution `coordinator_core.git.git_state.head_sha` uses for
    HEAD's own symref target, applied here to a remote-tracking ref instead.
    Read FRESH on every call (no memo), matching `git_state`'s own no-cache
    negative-spec: a caller re-reading immediately after a `git fetch` needs
    the post-fetch value, not a stale pre-fetch one.
    """
    common_dir = git_common_dir(root)
    try:
        content = (common_dir / ref).read_text(encoding="utf-8").strip()
        if content:
            return content
    except OSError:
        pass
    try:
        packed_text = (common_dir / "packed-refs").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in packed_text.splitlines():
        if not line or line[0] in "#^":
            continue
        sha, _, ref_name = line.partition(" ")
        if ref_name == ref:
            return sha
    return None


def _remaining_or_none(deadline: Optional[float]) -> Optional[float]:
    """Seconds left on *deadline*, or `None` when the caller set no budget.

    `None` is the whole "this call is unbudgeted" signal and must stay
    distinguishable from `0.0` -- an unbudgeted ladder keeps
    `git_native.push`/`fetch`'s own defaults, while an exhausted one stops.
    Collapsing the two would silently put every legacy caller on a zero
    timeout.
    """
    if deadline is None:
        return None
    return deadline - time.monotonic()


def _budget_exhausted_reason(
    attempts_made: int, budget_secs: Optional[float], last_reason: Optional[str]
) -> str:
    """The reason string for a ladder stopped by its own deadline.

    Names the budget AND the reject that was actually observed, because the
    two answer different questions for whoever reads this: the budget says
    why we stopped trying, the reject says what the remote last told us. A
    reader who sees only the budget cannot tell a wedged remote from an
    ordinary busy shared branch.

    NEVER routed to `PushOutcome.unconfirmed`: this string is only ever
    produced BETWEEN attempts, where the previous push's outcome was
    observed and nothing is in flight. See `PUSH_RETRY_BUDGET_SECS`.

    Returns the reason ALONE, without the `"git push: "` prefix every
    `PushOutcome` message in this module carries -- the two call sites own
    that, because one of them reaches `PushOutcome` through `last_reason` and
    the shared prefix at the end of the ladder. Embedding it here made the
    fetch-leg exhaustion read `"git push: git push: stopped after ..."` while
    the attempt-0 exhaustion read it once: the same event, spelled two ways,
    depending on which branch produced it (2026-08-26 review, slice 2).
    """
    budget_part = "budget" if budget_secs is None else f"{float(budget_secs):g}s budget"
    tail = f" (last: {last_reason})" if last_reason else ""
    # `attempts_made == 0` is the budget-too-small-to-try-once case: saying
    # "stopped after 0 attempt(s) ... before the remote accepted" implies a
    # remote that declined us, when nothing was ever sent.
    made = (
        "stopped before any attempt"
        if attempts_made == 0
        else f"stopped after {attempts_made} attempt(s)"
    )
    return (
        f"{made} -- the ladder's own {budget_part} was exhausted "
        f"before the remote accepted{tail}. Nothing is in flight; reconcile "
        f"and retry at the next checkpoint."
    )


def push_with_retry(
    worktree_root: Union[str, Path],
    *,
    allow_protected_branch: bool = False,
    protected_branch_override_reason: Optional[str] = None,
    budget_secs: Optional[float] = None,
) -> PushOutcome:
    """Push with reject-detect -> fetch -> rebase --onto -> re-push, bounded.

    No `--force` at any point. Bounded to `_PUSH_MAX_RETRIES` attempts. When
    no remote is configured, the push is skipped (`exit_code == 0`, nothing
    to sync -- not a failure). Before ever calling `git_native.push()`, the
    current branch is resolved and checked against `auto_push.branch_gate()`
    (imported as a read-only oracle, never extended here) -- `work/*`
    proceeds, everything else (`main` included) is declined with
    `exit_code == 0` and a `push:branch-policy` skip marker carrying the
    gate's own message; an unresolvable branch (detached HEAD, or the git
    call failed) is declined too, under a distinct `push:branch-unresolvable`
    marker, rather than silently proceeding to push a branch it cannot name.
    On a rejected push, fetches the remote, rebases this session's own
    commit range onto the updated ref (never a bare rebase on the shared
    branch), and re-pushes. If the rebase itself refuses, or retries are
    exhausted while still rejected, returns a hard non-zero failure (in
    `PushOutcome.failed`) -- never a silent skip that lets the caller believe
    the push landed. FIX-I (2026-08-19): a push subprocess TIMEOUT is
    reported distinctly, in `PushOutcome.unconfirmed` instead -- it is never
    retryable (a timeout reason never matches `_PUSH_RETRY_CLASSES`) and its
    true outcome was never observed, so it must not collapse into the same
    `failed` bucket as a genuine, git-reported reject. See `PushOutcome`'s
    own docstring for the full reasoning.

    C3b (AC7): on a landed push, resolves what was actually pushed --
    `PushOutcome.pushed_range`/`pushed_count` -- from the upstream tip
    resolved just before this call's first `git push` and the post-push
    `HEAD`. That resolve happens ONLY after both the no-remote check and the
    branch-policy gate above have passed -- never at the top of this
    function -- so a decline or a no-remote skip never pays for a `rev-parse`
    whose answer they will never report (`auto_push.run_push_with_retry`'s
    `cockpit_script` stat models the same "gate first, pay for extra state
    only once you know you need it" ordering).

    A rejected-and-retried push complicates the lower bound: the tip
    captured before the loop began is no longer the right "old" sha once a
    fetch has run, because commits between that original tip and the
    freshly-fetched one already reached the remote via someone else's push
    -- THIS call did not land them, so they must not appear in its own
    reported range. The tracked lower bound is therefore re-pointed at the
    freshly-fetched upstream tip immediately after each successful fetch,
    so the range finally reported on a landed retry names only the commits
    this call itself pushed.

    `budget_secs` (2026-08-26) -- an END-TO-END deadline for this whole
    ladder, stamped at entry, with each REMOTE leg (`push`, `fetch`) sized
    from the remainder and the deadline re-checked BETWEEN attempts. The
    local `rebase --onto` between them is deliberately NOT bounded by it:
    it spawns no network call, and cutting a rebase mid-flight would leave
    the worktree mid-rebase -- a worse state than the overrun it would
    prevent. The between-attempts check is what catches a ladder whose
    rebase ran long. `None` (the
    default) keeps the pre-existing unbudgeted behaviour for every caller
    that has not opted in, so this parameter changes nothing it is not
    passed to. See `PUSH_RETRY_BUDGET_SECS` for why the ladder must own a
    deadline rather than inherit one from the dispatch guard.

    NEGATIVE SPEC -- an exhausted budget is `failed`, never `unconfirmed`.
    The check happens between attempts, where the previous push was
    OBSERVED as rejected and no push is in flight, so the outcome is
    decided and the caller can act on it. Only a leg that actually timed
    out mid-flight (`_is_indeterminate_push_result`) may set `unconfirmed`,
    and that path is untouched here. Routing budget exhaustion into
    `unconfirmed` would re-manufacture the "unknown" state this budget
    exists to remove.

    `allow_protected_branch`/`protected_branch_override_reason` (C4a, AC8/
    AC14) -- a keyword-only, per-call override that, when `True`, skips the
    `branch_gate()` predicate above entirely and lets the push proceed on a
    non-`work/*` branch. NEVER ambient (no env var, no module-level flag --
    see the plan's Anti-scope); the caller must pass it explicitly on the
    one call that needs it. The ONE sanctioned consumer, as of this chunk,
    is DoE-claude's `merging-to-main` SKILL, Step 10 item 5 (the
    post-merge, on-`main`, release-notes bookkeeping commit) -- see
    `run_commit_pipeline`'s own docstring for the full citation. No op in
    this repo passes it. Every exercised override -- the gate actually
    skipped and a push to a protected branch actually attempted -- prints
    via `_emit_push_policy_line("override-exercised", ...)` (C3/AC14); a
    `work/*` branch that would have passed the gate anyway does NOT print
    that line, since nothing was in fact overridden (see that call site
    below for the reasoning).
    """
    root = Path(worktree_root)

    if not _remote_configured_locally(root):
        return PushOutcome(exit_code=0, skipped=["push:no-remote"])

    branch = resolve_branch(str(root))
    if branch is None:
        _emit_push_policy_line("declined-unresolvable")
        return PushOutcome(exit_code=0, skipped=["push:branch-unresolvable"])

    should_push, skip_message = branch_gate(branch)
    if not should_push:
        if allow_protected_branch:
            # AC14 -- the gate would have declined this branch, and the
            # caller explicitly overrode it: this is a genuinely exercised
            # override, so it prints, and the push proceeds below rather
            # than returning the decline outcome.
            _emit_push_policy_line(
                "override-exercised",
                branch=branch,
                reason=protected_branch_override_reason,
            )
        else:
            _emit_push_policy_line("declined-policy", message=skip_message)
            return PushOutcome(
                exit_code=0,
                skipped=["push:branch-policy"],
                message=skip_message,
            )

    # AC7 -- resolved only now, after both gates above passed. `None` (no
    # upstream tracking ref, e.g. a genuine first push) is the explicit
    # "unknown" sentinel `PushOutcome.pushed_range`/`pushed_count` document.
    pre_push_upstream_sha: Optional[str] = None
    upstream_info = _resolve_upstream_local(root, branch)
    if upstream_info is not None:
        pre_push_upstream_sha = _ref_sha_local(root, upstream_info.ref_path)

    last_reason = ""
    last_exit_code = 1
    # True only when the LAST thing that happened was a push subprocess
    # timeout (`_is_indeterminate_push_result`) -- never set by a fetch/
    # rebase failure, which are always definite, observed results reached
    # only after a genuine git-reported reject already put a push attempt
    # through the retry loop (a timeout is never retryable, see below, so
    # control cannot reach the fetch/rebase branches with this still True).
    last_indeterminate = False

    deadline = None if budget_secs is None else time.monotonic() + float(budget_secs)

    for attempt in range(_PUSH_MAX_RETRIES):
        leg_timeout = _remaining_or_none(deadline)
        if leg_timeout is not None and leg_timeout <= 0:
            return PushOutcome(
                exit_code=last_exit_code or 1,
                failed=[
                    f"git push: "
                    f"{_budget_exhausted_reason(attempt, budget_secs, last_reason)}"
                ],
            )
        push_result = (
            git_native.push(root)
            if leg_timeout is None
            else git_native.push(root, timeout=leg_timeout)
        )
        if push_result.ok:
            new_sha: Optional[str] = None
            head_result = git_native.rev_parse_head(root)
            if head_result.ok and head_result.stdout.strip():
                new_sha = head_result.stdout.strip()
            pushed_range, pushed_count = _resolve_pushed_range(
                root, pre_push_upstream_sha, new_sha
            )
            return PushOutcome(
                exit_code=0,
                acted=["push"],
                pushed_range=pushed_range,
                pushed_count=pushed_count,
            )

        reason = condense_git_diagnostic(push_result.stderr) or f"exit_code={push_result.returncode}"
        last_reason = reason
        last_exit_code = push_result.returncode or 1
        # The one place `unconfirmed` can ever become True: this push
        # attempt's own outcome was never observed. A timeout reason never
        # matches `_PUSH_RETRY_CLASSES` (it isn't a git-reported reject at
        # all), so `_is_push_reject` is already False for it and this
        # breaks immediately below -- the flag never survives into the
        # fetch/rebase branches.
        last_indeterminate = _is_indeterminate_push_result(push_result)

        # C2 (docs/plans/2026-08-27-the-merge-gate-gets-a-remote-authority-
        # layer.md § C2): a rule-violation (GH013) refusal is NOT rebase-
        # recoverable -- rebasing changes nothing about coverage -- so it
        # never enters the fetch/rebase ladder below. It has its own,
        # narrower recovery instead: (re-)post the coverage-gate status for
        # the current tip, then re-push the SAME, already-uploaded objects.
        # A secret-scanning refusal (`_is_rule_violation_reject` is False
        # for it) falls straight through to the ordinary `_is_push_reject`
        # check below, which is already False for the whole `gh-push-
        # protection` class -- so it breaks here exactly as it did before
        # this chunk, `failed`, never re-pushed.
        if _is_rule_violation_reject(reason):
            if attempt == _PUSH_MAX_RETRIES - 1:
                break
            recovery_timeout = _remaining_or_none(deadline)
            if recovery_timeout is not None and recovery_timeout <= 0:
                last_reason = _budget_exhausted_reason(attempt + 1, budget_secs, last_reason)
                break
            recovery_failure = _recover_rule_violation_reject(root, pre_push_upstream_sha)
            if recovery_failure is not None:
                # Observed, not indeterminate -- the status posted (or a
                # confirmed unpostable reason) already tells us the outcome,
                # so this is `failed`, never `unconfirmed`.
                last_reason = recovery_failure
                break
            continue

        if not _is_push_reject(reason) or attempt == _PUSH_MAX_RETRIES - 1:
            break

        if upstream_info is None:
            # No `git push: ` prefix here: `last_reason` reaches `PushOutcome`
            # through the ladder's final return, which adds it. Embedding one
            # made this read `"git push: git push: rejected and no upstream
            # ..."` -- the third producer of the doubling the 2026-08-26 review
            # (slice 2 Q3) caught on the budget paths, and the one its fix
            # missed because no test reaches an unresolvable upstream.
            last_reason = f"rejected and no upstream tracking ref resolvable ({reason})"
            break

        fetch_timeout = _remaining_or_none(deadline)
        if fetch_timeout is not None and fetch_timeout <= 0:
            last_reason = _budget_exhausted_reason(attempt + 1, budget_secs, last_reason)
            break

        fetch_result = (
            git_native.fetch(root, upstream_info.remote_name)
            if fetch_timeout is None
            else git_native.fetch(root, upstream_info.remote_name, timeout=fetch_timeout)
        )
        if not fetch_result.ok:
            fetch_reason = condense_git_diagnostic(fetch_result.stderr) or "fetch failed"
            last_reason = f"git fetch: {fetch_reason}"
            last_exit_code = fetch_result.returncode or 1
            break

        # AC7 rebase-retry range fix (see docstring above): re-point the
        # lower bound at the tip this fetch just observed, so a landed
        # retry's reported range excludes commits that reached the remote
        # via someone else's push, not this call's. `_ref_sha_local` reads
        # fresh (no memo) so this reflects the fetch that just ran.
        refetched_sha = _ref_sha_local(root, upstream_info.ref_path)
        if refetched_sha:
            pre_push_upstream_sha = refetched_sha

        rebase_exit_code, rebase_reason = _rebase_onto_fetched_ref(root, upstream_info.abbrev)
        if rebase_exit_code != 0:
            last_reason = rebase_reason
            last_exit_code = rebase_exit_code
            break

    if last_indeterminate:
        return PushOutcome(exit_code=last_exit_code, unconfirmed=[f"git push: {last_reason}"])
    return PushOutcome(exit_code=last_exit_code, failed=[f"git push: {last_reason}"])


def _resolve_pushed_range(
    root: Path, old_sha: Optional[str], new_sha: Optional[str]
) -> Tuple[Optional[str], Optional[int]]:
    """Resolve `(pushed_range, pushed_count)` for a landed push (C3b, AC7).

    Returns `(None, None)` -- the documented explicit-unknown sentinel,
    never `("", 0)` or a silently-omitted value -- when either endpoint
    could not be resolved (most notably a first push with no upstream
    tracking ref). When both endpoints resolve but `git rev-list --count`
    itself fails or returns unparseable output, `pushed_range` is still
    reported and only `pushed_count` comes back `None` -- the count is
    unknown, not the range.
    """
    if old_sha is None or new_sha is None:
        return None, None
    pushed_range = f"{old_sha}..{new_sha}"
    count_result = git_native.rev_list_count(root, pushed_range)
    if not count_result.ok or not count_result.stdout.strip():
        return pushed_range, None
    try:
        pushed_count = int(count_result.stdout.strip())
    except ValueError:
        return pushed_range, None
    return pushed_range, pushed_count


def derive_pushed_tristate(push_outcome: Optional[PushOutcome]) -> Optional[bool]:
    """Derive the `pushed` tri-state from a `PushOutcome` (or None -- never attempted).

    Kept as `Optional[bool]` for existing readers; `push_status` (see
    `derive_push_status` / `PipelineResult.push_status`) is the fully
    disambiguated field and should be preferred by new code.

    True  -- the push genuinely synced this run.
    False -- attempted and did not land (a genuine push failure, i.e.
              `push_status == "push-failed"`).
    None  -- NOT SYNCED, and NOT A FAILURE -- read `push_status` for which of
              its several reasons applies. `None` is shared by (at least)
              THREE distinct meanings, and this function cannot tell them
              apart on its own:
                (a) no remote configured (`push_status == "no-remote"`);
                (b) a branch-policy decline, or an unresolvable branch,
                    added by this plan (`push_status == "declined"`);
                (c) the pipeline never reached this function at all, most
                    notably `run_commit_pipeline`'s nothing-to-commit no-op
                    (the `if not commit_paths:` early-return) -- that call
                    site never constructs a `PushOutcome` and sets `pushed`
                    to `None` directly, without going through this
                    function, for the same "nothing to push, no invariant
                    violated" reason;
                (d) the push subprocess timed out and its true outcome was
                    never observed (`push_status == "unconfirmed"`, FIX-I)
                    -- deliberately NOT `False`: a timeout is not an
                    observed failure, so it must not read as one here
                    either.
              This amends the prior docstring, which named ONLY (a) --
              `None` was already double-booked with (c) before this plan
              touched the contract, and (b) is the meaning this plan adds
              on top of both.
    """
    # C-03 (opro-01): expressed over `derive_push_status` rather than reading
    # `PushOutcome`'s fields a second time. Both functions used to walk
    # `skipped`/`failed`/`exit_code`/`acted` independently to answer the same
    # question in two vocabularies, and they DISAGREED: on a `push-failed`
    # outcome this one keyed off `exit_code` while `derive_push_status` keyed
    # off `failed`, so an outcome carrying one without the other resolved
    # differently depending on which caller asked. One reading now, rendered
    # two ways.
    #
    # `push_outcome is None` keeps its own answer, deliberately: `False` here
    # is a pinned contract (test_derive_pushed_tristate_false_when_push_never_
    # attempted), and it is NOT what `derive_push_status(None)` says
    # (`not-attempted`, which renders as the unknown rung). The divergence is
    # real and is left standing rather than quietly harmonised -- this function
    # is documented as the lossy legacy shape for existing readers, and moving
    # a caller from "not pushed" to "unknown" is a semantic change no part of
    # opro-01 asked for. New code reads `push_status`.
    if push_outcome is None:
        return False
    status = derive_push_status(push_outcome)
    if status == PUSH_STATUS_PUSHED:
        return True
    if status == PUSH_STATUS_FAILED:
        return False
    return None



_LOG = logging.getLogger(__name__)


def _drain_pending_push_after_sync(worktree_root: Union[str, Path]) -> None:
    """Re-host `auto_push.drain_pending_push`'s per-commit call site.

    opro-01 C-01. That drain used to ride at the head of every commit's own
    detached `run_push_with_retry` -- "the NEXT commit fires this for free"
    (see its own docstring, call site 1 of 3). Standing the post-commit hook
    down so this op is the sole publisher would have taken that call site
    with it, silently narrowing a durability mechanism to its two remaining
    hosts (session boot, workday-start) as a side effect of a spawn-count
    fix. Re-hosted here instead of accepted as collateral.

    Called only AFTER a confirmed successful synchronous push, for two
    reasons: the branch tip is already published at that point, so a record
    covering this branch is moot rather than raced; and the network cost is
    already paid, so this is not a new blocking call on the commit hot path.
    Costs zero subprocesses when no record exists (`_read_pending_record`
    returns None and it returns immediately), which is the ordinary case.

    Best-effort by the same contract as everything else in that module: a
    drain failure must never turn a landed, pushed commit into a reported
    failure.
    """
    try:
        from coordinator_core.hooks import auto_push

        auto_push.drain_pending_push(str(worktree_root))
    except Exception:
        _LOG.debug("post-sync-push pending-push drain failed", exc_info=True)
