"""
GRAVESTONE NOTICE — READ BEFORE THE DOCSTRING BELOW (2026-08-30).

`wsc_tail.py` DOES NOT EXIST (K-046, `c07062c99`, 2026-08-23). Every
present-tense reference to it below — "`wsc_tail.py` still invokes this op
IN-PROCESS", "the in-process WSC tail", its `_TailTiming` pinned contract, its
`_close_origin_stub_handler` module-global — is HISTORICAL and was never
revised after that kill.

`run()`'s ONE live caller is `execute_plan_assemble/close_out_and_stamp.py::
_reach_post_commit_tail_stub_close` (`/execute-plan`'s close-out), and that
caller suppresses the stamp: `post_commit_stamp_and_ship` has ZERO reachable
invocations anywhere in this tree. Full trace, and why the suppression is
correct but ownerless:
`docs/research/spike-verdicts/2026-08-30-baton-ship-stamp-inside-a-500ms-close.md`
§ CORRECTION; requirement: kill-ledger K-046; retention rationale: K-116.

Prose naming a dead caller in the present tense is not cosmetic debt here — it
previously manufactured a defect that did not exist (see K-046's amendment)
and hid the requirement that does.

coordinator_core.ops.ceremony.post_commit_tail — `ceremony.post_commit_tail`
standalone REGISTERED op composing `wsc_tail`'s two post-commit steps (5c:
the C5 consumed-handoff stamp+ship, 5d: the 2026-07-22 origin-stub-close
fold) into ONE callable unit.

Purpose (C3a, docs/plans/2026-07-23-wsc-tail-slim-down.md § C3a): a PURE
refactor. `wsc_tail.py` used to sequence these two steps inline, duplicated
across its fresh-pass and AC18-resumed-pass branches. This module extracts
that sequencing into one standalone op so C1's per-step timing instrumentation
can attribute the ~3 git commits these two steps together produce to one op,
and so a LATER chunk (C3b — NOT this one) can move the invocation itself
across the repo boundary into the DoE skill occasion without re-deriving the
sequencing logic. `wsc_tail.py` still invokes this op IN-PROCESS at both call
sites, on both the fresh pass and the AC18-resumed pass, exactly as it did
before this extraction — this chunk changes WHERE the sequencing logic lives,
never WHEN it runs or under what lock.

HARD CONSTRAINT: this op MUST NOT acquire any ceremony-wide serialization
lock. Neither composed step has ever held one — see
`consumed_handoff_stamp.py`'s own negative-spec ("Does NOT acquire a
ceremony-wide lock itself, and its caller does not hold one either") and
`wsc_tail.py`'s "Does NOT hold an outer lock around steps 5c/5d" negative-spec
entry — both remain true after this extraction. This was a deliberate
feature removal, first ratified by the PM as DEC-3 (2026-07-22, jettisoning
the outer hold around this op specifically) and made repo-wide by the
2026-08-07 PM ruling that removed the `ceremony_lock` mutex entirely (see
`docs/plans/2026-08-07-excise-the-ceremony-lock.md`). Do NOT add a lock back
here, or anywhere in the commit path — restoration is separately sized and
not planned.

Origin-stub-close handler injection (test-patchability, not DI for its own
sake): `run()` takes `close_origin_stub_handler` as an explicit parameter
instead of importing `coordinator_core.ops.handoff_close_origin_stub._handler`
itself. `wsc_tail.py` keeps its own top-level `_close_origin_stub_handler`
name — it is independently required by
`test_close_origin_stub_standalone_op_still_registered`, which asserts
`get_op_handler("handoff.close_origin_stub") is wsc_tail_mod._close_origin_stub_handler`
— and passes that SAME module-global name into this op at each in-process
call site. Because Python resolves a bare module-global name fresh at every
call, `monkeypatch.setattr(wsc_tail_mod, "_close_origin_stub_handler", _boom)`
(see `test_origin_stub_close_failure_does_not_fail_the_tail` and
`test_origin_stub_close_runs_on_ac18_resume` in
`coordinator_core/ops/ceremony/tests/test_wsc_tail_parity.py`) continues to
take effect after this extraction exactly as it did when the sequencing lived
directly in `wsc_tail.py`. `post_commit_stamp_and_ship` needs no equivalent
treatment: it is already accessed as a `consumed_handoff_stamp` MODULE
attribute (`consumed_handoff_stamp.post_commit_stamp_and_ship(...)`), not a
name-bound alias, so patching
`wsc_tail_mod.consumed_handoff_stamp.post_commit_stamp_and_ship` mutates the
shared module object's own `__dict__` and is visible to any importer of that
module, this one included — no injection needed there.

Timing-span preservation: `wsc_tail.py`'s `_TailTiming` recorder pins
`"stamp_and_ship"` and `"origin_stub_close"` as TWO SEPARATE named steps
(`test_timing_map_covers_every_instrumented_step_with_nonnegative_ms`, C1).
`run()` accepts an optional `timing` object — duck-typed, anything exposing a
`.measure(name)` context manager, mirroring `_run_precommit_tail`'s own
sub-step-recording convention in `wsc_tail.py` — and records its own two
spans into it when supplied. The caller's timing map is therefore
byte-identical whether this op runs standalone via the registry (no timing
passed — the spans simply aren't recorded) or in-process under `wsc_tail`
(its own recorder passed straight through).

Six-surface op registration (this op): impl module (here, `@register_op`);
`coordinator_core/op_scopes.py::_OP_KEY_SCOPE` → `"common_dir"`; no
`_OP_TIMEOUT_OVERRIDES` entry — this is a `ceremony.*` op, so what bounds it
is the 2s ceremony budget (`ipc.CEREMONY_BUDGET_SECS`, DR-348), not the
global 30s runaway guard; this op does strictly less work than
`ceremony.wsc_tail`, itself held to the same budget post-DEC-2;
`coordinator_core/authz/classification.py` →
`OpClass.MUTATING` with the DR-208 five-question affirmation;
`coordinator_core/ops/__init__.py::_EAGER_OP_MODULES` eager-import entry;
`coordinator_core/ops/_registry_map.py::OP_MODULE_MAP` lazy-import entry.

Spec backlink: pln-wsc-tail-slim-down-op-scoped-c-e9a265 § C3a.

Second trigger (C6b, docs/plans/2026-08-04-terminal-state-propagation-join-keys.md
§ C6b): the other half of PM ruling R1 — a handoff concluding
terminally-positive through `/workstream-complete` cascades the same way a
plan stamped `implemented` does. This module is the natural seam because it
is already the registered standalone op composed by `wsc_tail` at every
ceremony that stamps a consumed handoff `shipped` (C5 widened its reach to
`/execute-plan` close-out and `/mise-en-place` tail on top of `wsc_tail`
itself). After `stamp_and_ship` names a handoff `stamped` (i.e. this pass
just flipped it to `deployment_state: shipped`), `run()` reads that handoff's
own `deliverable_id` and calls THE SAME shared entrypoint C6 registers
(`deliverable.cascade_terminal`) with `source_kind="handoff"` — never a
second cascade implementation. Re-entrancy (AC6i) is a property of that
entrypoint's own construction (idempotent re-scan, self-advance guard on
`source_path`), not of this trigger: this module fires the op once per
newly-stamped handoff and never re-invokes it on an artifact the op itself
advanced. A per-handoff cascade failure is soft-failed into
`deliverable_cascade_result["failed"]`, mirroring the origin-stub-close leg's
own soft-fail discipline — one bad cascade call must not fail the whole tail.
This step runs UNTIMED (see "Timing-span preservation" below) — it does not
widen `wsc_tail.py`'s own pinned `_TailTiming` step-name contract.

Third leg (C3, docs/plans/2026-08-18-auto-reconcile-must-fire.md § C3, dlv-
auto-reconcile-must-fire-not-surface-e1e90e): the PM's "a shipped blocker's id
comes out of every dependent's blocked_by the moment it ships" — cascade-
clearing PROMPTLY rather than only on C5's cadence backstop. For the SAME
`stamped` set the deliverable-cascade leg above already reads, this leg calls
`ops.handoff_children.blocked_by_dependents_many` ONCE for the whole stamped
set and fires `handoff.transition`'s `gate-cascade-clear` verb (C1's
MOVE-not-drop writer) once per live dependent whose `blocked_by` names that
baton. That resolver is TRI-STATE — its own "indeterminate" (a non-empty
`scan_errors`, or an unresolvable candidate identifier) is fail-closed-and-
logged here, never read as "no dependents": silently declining a fan-out
because a scan hiccuped is how a shipped-blocker wall survives. Exactly THREE
named `_gate_cascade_clear` `MutateAbort` shapes — dependent not
`awaiting_gate`, requested blocker id no longer in `blocked_by`, blocker's
live state does not clear the gate — classify as a named skip; every other
error (validation failure, lock timeout, malformed frontmatter, a usage
error) propagates into `failed`, never silently swallowed as a no-op.

COST (part (c) of the chunk body): the fan-out resolution walks the full
live+archive handoff corpus ONCE per tail invocation — not once per stamped
baton. The corpus walk and its `blocked_by` normalisation are
candidate-independent, so `blocked_by_dependents_many` hoists them out of the
per-candidate loop and leaves a set lookup behind; a tail that stamps five
batons pays one walk, not five. Measured against this repo's own live corpus
on 2026-08-18: `state/handoffs/*.md` = 142, `archive/handoffs/**/*.md` = 444,
`archive/completed/**/*.md` = 440 — a ~1026-file walk, run synchronously on
the commit hot path `ipc.py::_timeout_for` bounds, at the
50-70-concurrent-session load norm (`docs/wiki/machine-load-norm.md`) this
plan is sized against.

What the walk does NOT cover is the write side: `_gate_cascade_clear`
re-resolves each blocker id against LIVE disk a second time per call (its own
act-time re-verification guard against the shared-worktree carry-forward-
laundering race — Anti-scope: that re-resolution is NOT cached here), so the
per-dependent cost is still linear in the fan-out. Chosen option: (ii) BOUND
the per-tail fan-out (`_MAX_GATE_CASCADE_DEPENDENTS_PER_STAMPED`) with the
remainder left in `blocked_by` — untouched, not dropped — for C5's cadence
backstop to clear on its own pass.

Negative-spec (hard-won):
  - Does NOT acquire any ceremony-wide serialization lock — see HARD
    CONSTRAINT above. Do not add one back; DEC-3 removed the hold around this
    op deliberately, and the 2026-08-07 PM ruling removed the underlying
    `ceremony_lock` mechanism entirely.
  - Does NOT move the `wsc_tail` call site — that is C3b, a separate chunk
    with its own PM-recorded fallback (moving invocation across the repo
    boundary into a DoE skill occasion needs a durable pending-work
    sentinel). `wsc_tail.py` still invokes this op in-process at steps
    5c/5d, on both the fresh and AC18-resumed pass, exactly as before this
    extraction.
  - Does NOT re-implement `post_commit_stamp_and_ship`'s or
    `handoff.close_origin_stub`'s own logic — composes both via their
    existing entry points (a module-attribute call for the former, an
    injected handler callable for the latter). See "Origin-stub-close
    handler injection" above for why the two are treated differently.
  - Does NOT change behavior relative to the pre-extraction `wsc_tail.py`
    inline sequencing — this chunk (C3a) is a pure refactor; the acceptance
    bar is behaviour-identical, not latency-improved (that is what C3b,
    a later chunk, is for).
  - Does NOT reimplement `deliverable.cascade_terminal`'s join/predicate/
    write logic (C6b) — this module only fetches and calls that registered
    op via `get_op_handler`, exactly as it already does for
    `handoff.close_origin_stub`. A second cascade implementation here would
    give the repo two propagation paths that can disagree — precisely the
    dispatch-fragility footgun this repo's conventions warn about.
  - Does NOT reimplement `_gate_cascade_clear`'s MOVE-not-drop write, its
    reverse-dependents resolution, or its act-time re-verification (C3) —
    composes `ops.handoff_children.blocked_by_dependents_many` (read-only) and
    dispatches the SAME `handoff.transition` op every other caller of
    `gate-cascade-clear` uses. A second writer of `no_longer_blocked_by`
    here is exactly the disagreement this repo's two-writers-must-agree
    precedent (C1) exists to prevent.
  - Does NOT treat the fan-out resolver's `"indeterminate"` state as
    `"none"` — see "Third leg (C3)" above. Both fail-closed-and-log; neither
    silently declines the fan-out as if there were nothing to clear.
  - Does NOT cache `_gate_cascade_clear`'s act-time re-resolution, and does
    NOT widen the per-tail fan-out bound past a measured, named option — see
    "Third leg (C3)" § COST above.
  - Does NOT run `git log --grep`, or any other trailer/history scan, to
    find the sha to fold — `committed_sha` is already a required `run()`
    parameter (spike verdict constraint 1, module section "Completion-entry
    commit-ledger fold").
  - Does NOT re-derive `d-complete-entry`'s entry path (chain-slug
    idempotency guard, LoE computation, today's-date filename derivation) —
    `completion_entry_path` is caller-supplied, exactly as
    `directives_completion.py`'s own negative-spec forbids a second
    derivation of that guard.
  - Does NOT acquire a lock for the completion-entry fold — same HARD
    CONSTRAINT as the rest of this module (see top of docstring); this leg
    is the sole in-process caller of its own entry-path rewrite, so no
    concurrent-writer hazard exists to serialize against.
  - Does NOT widen `resolve_chain_commits` or any chain-widening helper
    back into existence — `completion_ops.py`'s own docstring forbids
    resurrecting them, and this leg never reads git history at all.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import tempfile
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from coordinator_core.dag import _read_meta
from coordinator_core.ipc import get_op_handler, register_op
from coordinator_core.op_budget_suspension import OpSuspendedError
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.ceremony import consumed_handoff_stamp
from coordinator_core.ops.completion_ops import _parse_existing_commits
from coordinator_core.ops.handoff_children import blocked_by_dependents_many
from coordinator_core.ops.ceremony.push import (
    _PUSH_MODES_SUPPRESSING_POST_COMMIT_HOOK,
    PUSH_MODE_NEVER,
    PUSH_MODE_SYNC,
    PUSH_STATUS_CADENCE_PENDING,
    PUSH_STATUS_DECLINED,
    PUSH_STATUS_FAILED,
    PUSH_STATUS_NO_REMOTE,
    PUSH_STATUS_NOT_ATTEMPTED,
    PUSH_STATUS_PUSHED,
    PUSH_STATUS_UNCONFIRMED,
    derive_push_status,
    CEREMONY_PUSH_BUDGET_SECS,
    push_with_retry,
    resolve_post_push_sha,
)
from coordinator_core.ops.ceremony.git_native import (
    commit_scoped,
    rev_parse_head,
)
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.session import scope as session_scope

_LOG = logging.getLogger(__name__)

OP_NAME = "ceremony.post_commit_tail"

#: Tail-result label for the origin-stub-close leg — mirrors `wsc_tail.py`'s
#: own `OP_CLOSE_ORIGIN_STUB` constant (kept as a separate copy there for its
#: own `tail_results` dict key and the standalone-op-still-registered test;
#: this module's copy is used only in its own skip-label strings below).
OP_CLOSE_ORIGIN_STUB = "handoff.close_origin_stub"

#: C6b's second trigger — the shared cascade op C6 registers (see
#: `deliverable_cascade.py`). Fetched via `get_op_handler` at call time,
#: never re-implemented here (module docstring "RE-ENTRANCY" addition below).
OP_DELIVERABLE_CASCADE = "deliverable.cascade_terminal"

#: C3's third leg — the registered op `handoff_transition.py` exposes; this
#: leg dispatches its `gate-cascade-clear` verb. Fetched via `get_op_handler`
#: at call time (see module docstring "Third leg (C3)"), never a top-level
#: import of the verb's own implementation function.
OP_HANDOFF_TRANSITION = "handoff.transition"

#: Label prefix for this leg's own skip/fail strings below — mirrors
#: `OP_CLOSE_ORIGIN_STUB`/`OP_DELIVERABLE_CASCADE`'s use as a string prefix,
#: not a second op name.
OP_GATE_CASCADE_CLEAR = "handoff.transition:gate-cascade-clear"

#: COST bound (option (ii), module docstring "Third leg (C3)" § COST) — caps
#: the number of live dependents cascade-cleared per stamped baton, per tail
#: invocation. A dependent past this cap is left in `blocked_by` (untouched,
#: not dropped) for C5's cadence backstop (`handoff.reconcile_open` on
#: `boot_sweep`) to clear on its own pass. Deliberately small: each dependent
#: costs a `_gate_cascade_clear` call that re-resolves its blocker ids against
#: LIVE disk (module docstring "Third leg (C3)" § COST), on the synchronous
#: commit hot path, at the 50-70-concurrent-session load norm. The corpus walk
#: itself is NOT what this bounds — that is hoisted to once per tail by
#: `blocked_by_dependents_many`.
_MAX_GATE_CASCADE_DEPENDENTS_PER_STAMPED = 5


def _measure(timing: Optional[Any], name: str):
    """Return `timing.measure(name)` when a timing recorder was supplied,
    else a no-op context manager. See module docstring "Timing-span
    preservation" for why this exists."""
    if timing is None:
        return nullcontext()
    return timing.measure(name)


# ---------------------------------------------------------------------------
# Origin-stub close (moved verbatim from wsc_tail.py's step-5d helpers,
# 2026-07-22 fold; see module docstring).
# ---------------------------------------------------------------------------


def _compose_origin_stub_close_message(closed_paths: list[str], committed_sha: str) -> str:
    """Compose the origin-stub-close follow-up commit's message body.

    Sibling to `consumed_handoff_stamp._compose_follow_up_message` -- not
    subject to AC4's golden-format parity requirement (scoped to the MAIN
    ceremony commit only).
    """
    lines = [
        f"ceremony: close origin spinoff stub(s) on ship (shipped_in={committed_sha})",
        "",
    ]
    for p in closed_paths:
        lines.append(f"- {p}")
    return "\n".join(lines) + "\n"


def _commit_and_push_origin_stub_close(
    worktree_root: Path,
    closed_paths: list[str],
    committed_sha: str,
    push_mode: str = PUSH_MODE_SYNC,
    sid: Optional[str] = None,
) -> tuple[Optional[str], Optional[bool], str, Optional[str]]:
    """Computed-mechanism follow-up commit (`git_native.commit_scoped`) for
    the closed origin-stub file(s) -- its OWN small commit, a sibling to
    `consumed_handoff_stamp`'s AC17 follow-up commit, never left as an
    unswept dirty working-tree edit. Mirrors
    `consumed_handoff_stamp._commit_and_push_follow_up` exactly (same
    commit_scoped/rev-parse/[push] shape, same `push_mode`
    gating -- DEC-1) -- not reused directly since that function's
    message/label are stamp-specific. The COMMIT is unconditional; the PUSH
    is gated by ``push_mode``: `"sync"` attempts a push here directly
    (via `commit_pipeline.push_with_retry`, so the same branch-policy gate
    the main ceremony commit's push obeys also governs this follow-up push);
    `"deferred"`/`"none"` skip it entirely (`pushed=None`,
    `push_status=PUSH_STATUS_NOT_ATTEMPTED`, no attempt) -- the caller spawns
    ONE detached push for the whole tail after it completes.

    Returns (follow_up_sha, pushed, push_status, error) -- an ``error`` here
    is a soft-fail (see `_run_origin_stub_close`'s caller): the origin-stub
    mutation already landed on disk via the composed op call; only the COMMIT
    of that mutation can fail here, and a failure leaves it as a working-tree
    edit for the next ceremony pass (or the lvv-09 cadence backstop) to pick
    up -- it does not unwind the already-landed main ceremony commit.

    ``follow_up_sha`` is captured via `rev_parse_head` -- Review: code-
    reviewer, Finding 1 (P1): captured a SECOND time, AFTER a landed push,
    because `push_with_retry` can fetch+rebase-onto this very commit on a
    rejected push before re-pushing, which rewrites its SHA. The pre-push
    capture is only ever the RETURNED value on a decline/no-remote/
    not-attempted/failed push -- none of which rewrite anything; on a
    landed push the post-push re-read is authoritative, with the pre-push
    value as fallback only if that re-read itself fails (never silently
    downgraded to None).

    ``push_status`` is the canonical `commit_pipeline.PUSH_STATUS_*` vocabulary
    (`derive_push_status`) -- it is the ONLY reliable way to tell a genuine
    push failure (`PUSH_STATUS_FAILED`, carried in ``error`` too) apart from a
    `branch_gate()` POLICY DECLINE (`PUSH_STATUS_DECLINED`) or a missing
    remote (`PUSH_STATUS_NO_REMOTE`): a decline is neither a landed push
    (``pushed`` stays falsy/None) nor a failure (``error`` stays `None`) --
    routing it through the error channel would surface a soft-fail for
    behaviour that is exactly correct, the same false-alarm class this plan
    removed from `integrity_breach`. Do not collapse `push_status ==
    PUSH_STATUS_NOT_ATTEMPTED` (this function's own `push_mode != "sync"`
    no-attempt case) with `PUSH_STATUS_DECLINED`/`PUSH_STATUS_NO_REMOTE`
    (a `"sync"` attempt that `push_with_retry` itself chose not to land) --
    two distinct reasons for "no push happened".

    A returned ``error`` is the ONLY success signal a caller may trust -- do not
    corroborate it against the branch tip. On a shared worktree an `index.lock`
    contention failure here is loud at the call but has been misread as success
    downstream, because the log tip a caller printed to "confirm" the commit was
    a PEER's commit, not this one (lesson 2026-07-21-git-index-lock-contention;
    row 10 of docs/research/2026-07-28-is-the-jettisoned-ceremony-lock-outer-ho.md).
    """
    message = _compose_origin_stub_close_message(closed_paths, committed_sha)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(message)
        msg_path = fh.name
    try:
        # Routed through the computed selector (not a raw `add_paths` +
        # `commit_with_message_file` pair) -- see `git_native.commit_scoped`'s
        # docstring. `closed_paths` is guaranteed non-empty here (the caller
        # returns early on an empty `closed_by_stub`); `commit_scoped` stages
        # internally on the AGREE branch and never re-derives staged content
        # from the worktree on the DIVERGED branch, closing the same
        # 506748a0 hazard the main ceremony commit is already routed around.
        # opro-01 C-01 (review finding, s1): this flow is the same
        # commit-then-own-sync-push shape `run_commit_pipeline` has, so it had
        # the same two-publisher race -- the post-commit hook detaches and
        # pushes while this call's own `push_with_retry` below races it. Tied
        # to `push_mode` for the same reason as the pipeline: on
        # `deferred`/`none` this call does NOT push (the guard below returns
        # early), so the hook's push is the only one and suppressing it would
        # strand the commit.
        commit_result = commit_scoped(
            closed_paths,
            msg_path,
            worktree_root,
            suppress_post_commit_auto_push=(push_mode == PUSH_MODE_SYNC),
        )
    finally:
        try:
            Path(msg_path).unlink()
        except OSError:
            print(
                f"skip: _commit_and_push_origin_stub_close: Path(msg_path).unlink() failed: {sys.exc_info()[1]}",
                file=sys.stderr,
            )
            pass

    if not commit_result.ok:
        return None, False, PUSH_STATUS_NOT_ATTEMPTED, f"git commit failed: {commit_result.stderr}"

    rev_result = rev_parse_head(worktree_root)
    follow_up_sha = rev_result.stdout.strip() if rev_result.ok else None

    # Post-commit claim release (C3d, docs/plans/2026-08-11-claim-release-
    # and-the-gate-that-cannot-clear.md): eligible -- same worktree,
    # `closed_paths` is already repo-relative (each `stub_path` in the
    # composed `handoff.close_origin_stub` op's `closed` list is `rel_id(
    # stub_path, worktree)` -- see `handoff_close_origin_stub.py` line
    # ~703), and `sid` is `run()`'s own required "WSC session id" caller
    # param, threaded down through `_run_origin_stub_close` unchanged --
    # the SAME session this whole post-commit tail is running on behalf
    # of, not a guess (`git_native.commit_scoped`'s own comment names
    # exactly this self/other ambiguity as why it does not wire release
    # in itself). Run synchronously here -- this function already executes
    # off the event loop via the caller's `_to_thread_commit_and_push`
    # (`asyncio.to_thread`), so a second `to_thread` hop would only add a
    # needless thread-pool round trip. Failure direction mirrors every
    # other C3 site: a release failure must never fail a commit that
    # already landed -- the commit above is the durable outcome; a
    # retained stale claim is the safe residue.
    # `sid` is Optional on this signature, and an unattributable release is
    # not a release: releasing under an unknown sid would be a guess at
    # authorship, which is the one thing this whole seam refuses to do.
    # Skipping is the same fail-safe RETAIN direction every other C3 site
    # takes -- and skipping EXPLICITLY, rather than letting a None fall into
    # the `except` below, keeps a genuine failure distinguishable from a
    # caller that simply had no sid to give.
    try:
        if sid:
            session_scope.release_committed_claims(
                sid, closed_paths, cwd=str(worktree_root)
            )
    except Exception:
        _LOG.debug(
            "_commit_and_push_origin_stub_close: release_committed_claims "
            "failed post-commit; claim(s) retained",
            exc_info=True,
        )

    if push_mode != PUSH_MODE_SYNC:
        return follow_up_sha, None, PUSH_STATUS_NOT_ATTEMPTED, None

    push_outcome = push_with_retry(
        worktree_root, budget_secs=CEREMONY_PUSH_BUDGET_SECS
    )
    push_status = derive_push_status(push_outcome)

    if push_status == PUSH_STATUS_PUSHED:
        # Review: code-reviewer — Finding 1 (P1): `push_with_retry` can
        # fetch+`git rebase --onto` this follow-up commit on a rejected
        # push before re-pushing, which REWRITES its SHA. The pre-push
        # `follow_up_sha` captured above is therefore stale in exactly the
        # retry case this ladder exists to handle. Re-resolve HEAD now,
        # after the push actually landed, so the reported SHA names the
        # commit that is really on the remote. Only the landed path pays
        # this second `rev-parse` — decline/no-remote/not-attempted/failure
        # paths below never rewrite anything, so they keep the pre-push
        # value untouched. If the re-read itself fails, fall back to the
        # pre-push SHA rather than downgrading a known-good value to None.
        # state/bug-backlog/2026-08-11-run-commit-pipeline-reports-a-
        # concurrent-0a91ea7dc77b.yaml (P1): that bare re-read fired on
        # every landed push, not just a rebase-retry, and could silently
        # adopt a peer's push landing in this window. `resolve_post_push_sha`
        # re-reads HEAD exactly as before but only adopts it once its tree
        # matches `follow_up_sha`'s (see that helper's own docstring); a
        # mismatch keeps `follow_up_sha`.
        landed_sha = resolve_post_push_sha(worktree_root, follow_up_sha)
        return landed_sha, True, push_status, None
    if push_status == PUSH_STATUS_FAILED:
        reason = push_outcome.message or "; ".join(push_outcome.failed) or "unknown push failure"
        return follow_up_sha, False, push_status, f"git push failed: {reason}"
    # PUSH_STATUS_DECLINED / PUSH_STATUS_NO_REMOTE / PUSH_STATUS_NOT_ATTEMPTED
    # / PUSH_STATUS_UNCONFIRMED / PUSH_STATUS_CADENCE_PENDING -- no push
    # landed, but this is NOT an error
    # (see docstring above): a policy decline, a missing remote, or a
    # subprocess timeout whose outcome was never observed (FIX-I,
    # 2026-08-19 -- distinct from PUSH_STATUS_FAILED above, which is a
    # genuine, git-reported reject) is `push_with_retry`'s own honest
    # "did not push, on purpose/by environment/unconfirmed" outcome.
    return follow_up_sha, None, push_status, None


#: Cap on the number of `blocking_children` paths rendered inline into a
#: skip-entry string — a wide fan-out (many live children) must not produce
#: an unreadable one-line ceremony entry.
_MAX_RENDERED_BLOCKING_CHILDREN = 3


def _render_skip_entry(s: dict) -> str:
    """Render one `handoff.close_origin_stub` skip entry as a
    `roadmap:stub:reason` ceremony line, extended with the guard's own
    `blocking_children`/`guard_error` fields when present, so the two
    guard-decline states (live children vs. indeterminate/fail-closed) —
    which demand opposite operator responses — survive past this string
    join instead of both reading as the bare `roadmap:stub:reason` prefix.

    Keeps the existing `roadmap:stub:reason` prefix shape intact (downstream
    readers/tests key on it); an entry with neither new field (e.g.
    `no-match`/`ambiguous`/`mutation-failed`) renders exactly as before.
    """
    prefix = f"{s.get('roadmap_id')}:{s.get('stub_id')}:{s.get('reason')}"
    children = s.get("blocking_children") or []
    guard_error = s.get("guard_error")
    if not children and not guard_error:
        return prefix
    bits = [prefix]
    if children:
        shown = ", ".join(children[:_MAX_RENDERED_BLOCKING_CHILDREN])
        remaining = len(children) - _MAX_RENDERED_BLOCKING_CHILDREN
        more = f" (+{remaining} more)" if remaining > 0 else ""
        bits.append(f"blocking: {shown}{more}")
    if guard_error:
        bits.append(f"guard_error: {guard_error}")
    return " ".join(bits)


async def _run_origin_stub_close(
    worktree_root: Path,
    common_dir: Path,
    committed_sha: str,
    governing_plan_slug: str,
    initial_consumed: list[tuple[str, dict]],
    close_origin_stub_handler: Callable[[dict, Path], Awaitable[dict]],
    *,
    push_mode: str = PUSH_MODE_SYNC,
    sid: Optional[str] = None,
    delivery_proof: Optional[dict] = None,
) -> dict:
    """Close the origin spinoff/spinoff-roadmap stub this session shipped
    (step 5d), composing the standalone `handoff.close_origin_stub` op via
    the CALLER-SUPPLIED ``close_origin_stub_handler`` (see module docstring
    "Origin-stub-close handler injection" for why this is injected rather
    than imported here). Runs UNLOCKED (DEC-3, and repo-wide since the
    2026-08-07 removal -- no ceremony-wide lock is held by this step or its
    caller), after `committed_sha` is known (mirrors
    `consumed_handoff_stamp.post_commit_stamp_and_ship`'s own precondition)
    -- never raises. ``push_mode`` gates the follow-up commit's push exactly
    as `consumed_handoff_stamp`'s own follow-up (DEC-1).

    Join inputs mirror the OLD bash Step 2.7b's own `_cosos_args` shape:
    `docs/plans/<governing_plan_slug>.md` (when `governing_plan_slug` is
    supplied -- the standalone op itself no-ops gracefully on a nonexistent
    path, so no pre-check is done here) paired with EACH consumed handoff
    from this pass's step-1 resolve (DEC-5,
    docs/plans/2026-07-24-multibaton-pickup-and-args-prose.md § C3 -- a
    session owning N consumed handoffs may derive from N distinct origin
    stubs, e.g. a DAG pickup of two independently-spun-off batons; truncating
    to `initial_consumed[0]` silently left every stub past the first open
    forever). A single-session pass with no consumed handoffs supplies one
    call with an empty `handoff_path`, matching the bash's
    `WSC_CONSUMED_HANDOFF` unset case byte-for-byte. Clean no-op (skipped,
    not failed) when neither `plan_path` nor any handoff path is available --
    the majority of workstreams are not stub-derived.

    Commit shape (DEC-5, explicit -- concurrent-EM shared index): the handler
    is called ONCE PER `initial_consumed` entry (plan_path repeated
    identically on every call); `closed` stub records are ACCUMULATED across
    all calls into a dict keyed by `stub_path`, so a stub reachable from more
    than one consumed handoff (the existing single-stub / shared-stub case)
    is recorded once, not double-counted, and the accumulation dict's
    insertion order keeps the N==1 path's `acted` ordering byte-identical to
    the pre-DEC-5 single-call code. Exactly ONE follow-up
    `_to_thread_commit_and_push` runs afterward, over the UNIONED
    `closed_paths` from every call -- never one follow-up commit per
    handoff, and never a window where some stubs are closed on disk and
    committed while others are still pending (a partial-mutation state on
    the shared git index every other concurrent-EM session also writes to).
    A per-call exception or non-zero `exit_code` is recorded into `failed`
    and that call's iteration continues to the next handoff rather than
    aborting the whole step -- one bad join must not block closing the
    stubs the other handoffs resolve cleanly.

    `sha=committed_sha` is always supplied (unlike the OLD bash, which never
    had a real post-commit sha available at its pre-commit Step 2.7b position)
    -- the standalone op's own stamp-before-ship ordering (see its module
    docstring) means this call is what lets the closed stub carry a
    `shipped_in` reference at all, closing a gap the bash could not.

    `delivery_proof` (optional; threaded verbatim from
    `close_out_and_stamp._reach_post_commit_tail_stub_close`, the ONLY caller
    with a delivery proof to give — see that function's own docstring) is
    forwarded unchanged into EVERY `close_origin_stub_handler` call this
    step makes (one per `initial_consumed` entry, same as `plan_path`/`sha`)
    -- `handoff.close_origin_stub`'s own `delivery_proof` param docs the
    completeness/stub-match conditions that decide whether it actually
    closes anything; this function never inspects or validates it itself,
    same "compose, don't reimplement" posture as the rest of this step.
    `None` (the `wsc_tail`-invoked call sites, which have no delivery proof
    of their own) preserves today's guard-only behaviour exactly.

    Returns a tail_ops-shaped `{acted, skipped, failed}` dict (never
    `failed_critical` -- see `wsc_tail.py`'s own step-6 exit-code rationale).
    """
    plan_path = f"docs/plans/{governing_plan_slug}.md" if governing_plan_slug else ""
    handoff_paths = [path for path, _fm in initial_consumed] if initial_consumed else [""]

    if not plan_path and not any(handoff_paths):
        return {
            "acted": [],
            "skipped": [f"{OP_CLOSE_ORIGIN_STUB}:no-governing-plan-or-consumed-handoff"],
            "failed": [],
        }

    closed_by_stub: dict[str, dict] = {}
    skipped: list[str] = []
    failed: list[str] = []

    for handoff_path in handoff_paths:
        if not plan_path and not handoff_path:
            continue

        try:
            call_params: dict = {
                "plan_path": plan_path,
                "handoff_path": handoff_path,
                "sha": committed_sha,
            }
            if delivery_proof is not None:
                call_params["delivery_proof"] = delivery_proof
            result = await close_origin_stub_handler(call_params, common_dir)
        except Exception as exc:  # noqa: BLE001 -- soft-fail, never raise past this tail step
            _LOG.warning("post_commit_tail: handoff.close_origin_stub raised %s: %s", type(exc).__name__, exc)
            failed.append(f"{OP_CLOSE_ORIGIN_STUB}: {exc}")
            continue

        if result.get("exit_code") != 0:
            # The op's own docstring documents TWO non-zero reply shapes: a
            # usage error carries `error`, the zero-join loud no-op (AC2/
            # AC14) carries `message` — reading only `error` silently
            # discarded the op's carefully-worded explanation and reported
            # a bare "unknown error" for every loud no-op.
            reason = result.get("error") or result.get("message") or "unknown error"
            failed.append(f"{OP_CLOSE_ORIGIN_STUB}: {reason}")
            continue

        for c in result.get("closed") or []:
            # dedup by stub_path (DEC-5): a stub reached from more than one
            # consumed handoff (shared-stub case) must not double-close, and
            # must not appear twice in the unioned follow-up commit.
            closed_by_stub[c["stub_path"]] = c
        skipped.extend(
            _render_skip_entry(s) for s in (result.get("skipped") or [])
        )

    if not closed_by_stub:
        return {
            "acted": [],
            "skipped": skipped or [f"{OP_CLOSE_ORIGIN_STUB}:no-op"],
            "failed": failed,
        }

    closed_paths = list(closed_by_stub.keys())
    follow_up_sha, _pushed, follow_up_push_status, follow_up_error = await _to_thread_commit_and_push(
        worktree_root, closed_paths, committed_sha, push_mode, sid
    )
    if follow_up_error:
        failed.append(f"follow-up: {follow_up_error}")
    elif follow_up_sha is None:
        # Should be unreachable (commit_result.ok implies a resolvable HEAD),
        # but never silently drop a genuine no-sha outcome as a clean success.
        failed.append("follow-up: commit landed but HEAD sha unresolved")
    elif follow_up_push_status in (
        PUSH_STATUS_CADENCE_PENDING,
        PUSH_STATUS_DECLINED,
        PUSH_STATUS_NO_REMOTE,
        PUSH_STATUS_UNCONFIRMED,
    ):
        # A policy decline, a missing remote, a ref-lock deferral to the next
        # cadence checkpoint, or a subprocess timeout whose
        # outcome was never observed (FIX-I, 2026-08-19) is NOT an error --
        # see `_commit_and_push_origin_stub_close`'s docstring. Named here
        # (skipped, never failed) purely for observability: the commit
        # itself landed, and the push status is withheld or unknown rather
        # than bad.
        #
        # These FOUR states are reported. The ladder is not exhaustive and
        # this comment does not claim it is: `PUSH_STATUS_PUSHED` falls
        # through deliberately (a silent success needs no entry), and
        # `PUSH_STATUS_NOT_ATTEMPTED` -- which is the DEFAULT
        # `push_mode="deferred"` case, not a rare one -- also falls through
        # and contributes nothing to `skipped`. That predates this change and
        # is not fixed here, because adding it changes observable ceremony
        # output on a path this roadmap does not own. Named rather than left
        # for the next reader to discover, and filed.
        skipped.append(f"follow-up:push:{follow_up_push_status}")

    return {"acted": closed_paths, "skipped": skipped, "failed": failed}


async def _run_deliverable_cascade(
    worktree_root: Path,
    repo_root: Path,
    stamped: list[str],
    cascade_handler: Optional[Callable[[dict, Path], Awaitable[dict]]],
) -> dict:
    """C6b's second trigger: for each handoff `stamp_and_ship` just flipped
    to `deployment_state: shipped` (a relpath in ``stamped``), read its own
    `deliverable_id` and fire `deliverable.cascade_terminal` — THE SAME
    shared entrypoint C6 registers — with `source_kind="handoff"` (see
    module docstring "Second trigger (C6b)").

    ``cascade_handler`` is resolved by the caller via `get_op_handler` (never
    re-implemented here); when the op is not registered (e.g. a test harness
    that never imports `deliverable_cascade`), this step is a clean no-op —
    the SAME "compose an optional standalone op if present" posture this
    module already applies to origin-stub-close resolution elsewhere.

    Returns a tail_ops-shaped `{acted, skipped, failed}` dict. A candidate
    handoff carrying no `deliverable_id` is skipped (named), not failed —
    plenty of handoffs legitimately carry none. A cascade call that itself
    reports `exit_code != 0` (e.g. "nothing downstream to advance") is
    skipped (named with the op's own message), not failed — that is the
    cascade's own honest "no candidates" outcome, not an error in firing it.
    Only an exception or a malformed reply is recorded as `failed` — one bad
    cascade call must not fail the rest of the tail (mirrors
    `_run_origin_stub_close`'s own soft-fail discipline).
    """
    acted: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []

    if not stamped:
        return {"acted": acted, "skipped": skipped, "failed": failed}

    if cascade_handler is None:
        return {
            "acted": acted,
            "skipped": [f"{OP_DELIVERABLE_CASCADE}:not-registered"],
            "failed": failed,
        }

    for relpath in stamped:
        handoff_abs = worktree_root / relpath
        fm = _read_meta(str(handoff_abs))
        deliverable_id = fm.get("deliverable_id") if fm else None
        if not isinstance(deliverable_id, str) or not deliverable_id.strip():
            skipped.append(f"{OP_DELIVERABLE_CASCADE}:{relpath}:no-deliverable-id")
            continue

        try:
            result = await cascade_handler(
                {
                    "deliverable_id": deliverable_id.strip(),
                    "source_kind": "handoff",
                    "source_path": str(handoff_abs),
                },
                repo_root,
            )
        except Exception as exc:  # noqa: BLE001 -- soft-fail, never raise past this tail step
            _LOG.warning("post_commit_tail: %s raised %s: %s", OP_DELIVERABLE_CASCADE, type(exc).__name__, exc)
            failed.append(f"{OP_DELIVERABLE_CASCADE}:{relpath}: {exc}")
            continue

        if not isinstance(result, dict):
            failed.append(f"{OP_DELIVERABLE_CASCADE}:{relpath}: malformed reply {result!r}")
            continue

        if result.get("exit_code") == 0:
            advanced = result.get("advanced") or []
            acted.extend(a.get("handoff_path", "") for a in advanced)
            # Review: coordinator:code-reviewer -- `commit_error` (AC8) is
            # present in the op's own result dict independent of exit_code
            # (a commit failure never flips exit_code, which stays keyed off
            # `advanced` alone), but was never read here, so it never reached
            # `has_failure` below. Fold it into `failed` so it does.
            commit_error = result.get("commit_error")
            if commit_error:
                failed.append(f"{OP_DELIVERABLE_CASCADE}:{relpath}: commit failed: {commit_error}")
        else:
            skipped.append(
                f"{OP_DELIVERABLE_CASCADE}:{relpath}: {result.get('error', 'no downstream artifact advanced')}"
            )

    return {"acted": acted, "skipped": skipped, "failed": failed}


#: The three named `_gate_cascade_clear` `MutateAbort` message shapes (see
#: `handoff_transition.py`) that are an expected, non-corrupting refusal —
#: classified as a named skip, never as `failed`. Matched by fixed prefix;
#: each message also interpolates caller-specific detail (a deployment_state
#: value, a missing-id list, a blocker id) that this match deliberately does
#: not pin, since only the FIXED lead text identifies which of the three
#: shapes fired.
_GCC_SKIP_PREFIXES = (
    "gate-cascade-clear requires deployment_state:awaiting_gate",
    "gate-cascade-clear: requested blocker id(s) not present in blocked_by",
)


def _is_gate_cascade_clear_named_skip(error_message: str) -> bool:
    """True for exactly the three `_gate_cascade_clear` `MutateAbort` shapes
    this leg treats as a skip (see `_GCC_SKIP_PREFIXES` and module docstring
    "Third leg (C3)"): dependent not `awaiting_gate`, requested blocker id no
    longer present in `blocked_by`, or a blocker whose live state does not
    clear the gate. Everything else — a lock timeout, an unparseable
    frontmatter, a post-mutation schema-validation failure, the
    blocker_ids/blocker_shas usage errors — is a real problem and must
    propagate into `failed` rather than be swallowed as a no-op.
    """
    if not error_message:
        return False
    if error_message.startswith(_GCC_SKIP_PREFIXES):
        return True
    return (
        error_message.startswith("gate-cascade-clear: blocker ")
        and "does not clear the gate" in error_message
    )


async def _run_gate_cascade_clear(
    worktree_root: Path,
    repo_root: Path,
    stamped: list[str],
    committed_sha: str,
    gate_cascade_clear_handler: Optional[Callable[[dict, Path], Awaitable[dict]]],
) -> dict:
    """C3's third leg: for each handoff `stamp_and_ship` just flipped to
    `deployment_state: shipped` (a relpath in ``stamped``), resolve its LIVE
    dependents via `ops.handoff_children.blocked_by_dependents_many`
    (one corpus walk for the whole stamped set) and fire
    `handoff.transition`'s `gate-cascade-clear` verb once per dependent whose
    `blocked_by` names it — see module docstring "Third leg (C3)" for the
    full design rationale (fail-closed indeterminate handling, the three
    named skip shapes, and the measured fan-out bound).

    ``gate_cascade_clear_handler`` mirrors ``cascade_handler``'s own
    optional-injection shape (not ``close_origin_stub_handler``'s required
    one): resolved by the caller via `get_op_handler` when not supplied, so
    an existing in-process caller that predates C3 needs no call-site change.
    A `None` handler (the op genuinely not registered) is a clean skip, same
    posture as `_run_deliverable_cascade`'s own "not-registered" branch.

    Returns a tail_ops-shaped `{acted, skipped, failed}` dict. `acted` names
    each dependent path whose `blocked_by` was actually narrowed/emptied;
    `skipped` carries the indeterminate-fan-out case, the fan-out-bound
    overflow, the three named `MutateAbort` refusals, and a genuine no-op
    (already at target state); `failed` carries everything else — a raised
    exception, a malformed reply, or any other `_gate_cascade_clear` error —
    mirroring `_run_deliverable_cascade`'s own soft-fail discipline: one bad
    cascade-clear call must not fail the rest of the tail.
    """
    import asyncio

    acted: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []

    if not stamped:
        return {"acted": acted, "skipped": skipped, "failed": failed}

    if gate_cascade_clear_handler is None:
        return {
            "acted": acted,
            "skipped": [f"{OP_GATE_CASCADE_CLEAR}:not-registered"],
            "failed": failed,
        }

    # ONE corpus walk for the whole stamped set (module docstring § COST) —
    # off the event loop, same hygiene rationale as this module's own
    # `_to_thread_commit_and_push`.
    candidate_keys = [str(worktree_root / relpath) for relpath in stamped]
    fan_out = await asyncio.to_thread(
        blocked_by_dependents_many, candidate_keys, worktree_root
    )

    for relpath, candidate_key in zip(stamped, candidate_keys):
        result = fan_out[candidate_key]
        state = result.get("state")

        if state == "indeterminate":
            # Tri-state: a scan hiccup or an unresolvable candidate id is
            # "we could not fully look", never "no dependents" — fail-closed
            # and logged, not silently declined (module docstring "Third leg
            # (C3)").
            _LOG.warning(
                "post_commit_tail: gate-cascade-clear fan-out for %s came back "
                "indeterminate (scan_errors=%s) — fail-closed, no dependent acted on",
                relpath, result.get("scan_errors"),
            )
            skipped.append(
                f"{OP_GATE_CASCADE_CLEAR}:{relpath}:indeterminate: {result.get('error')}"
            )
            continue

        if state != "dependents":
            continue  # "none" — no live dependent for this stamped baton

        dependents = result.get("dependents") or []
        candidate_identifiers = set(result.get("identifiers") or [])
        bounded = dependents[:_MAX_GATE_CASCADE_DEPENDENTS_PER_STAMPED]
        overflow = dependents[_MAX_GATE_CASCADE_DEPENDENTS_PER_STAMPED:]
        for dep_path in overflow:
            skipped.append(
                f"{OP_GATE_CASCADE_CLEAR}:{relpath}:{dep_path}:deferred-to-cadence-backstop "
                f"(fan-out bound {_MAX_GATE_CASCADE_DEPENDENTS_PER_STAMPED} exceeded)"
            )

        for dep_path in bounded:
            dep_meta = _read_meta(dep_path) or {}
            dep_blocked_by = dep_meta.get("blocked_by")
            if isinstance(dep_blocked_by, str):
                dep_blocked_by = [dep_blocked_by]
            if dep_blocked_by is not None and not isinstance(dep_blocked_by, (list, tuple)):
                failed.append(
                    f"{OP_GATE_CASCADE_CLEAR}:{dep_path}: blocked_by has unexpected type "
                    f"{type(dep_blocked_by).__name__!r} on live re-read"
                )
                continue
            matched_ids = [bid for bid in (dep_blocked_by or []) if bid in candidate_identifiers]
            if not matched_ids:
                # A concurrent writer (another session, or this tail's own
                # earlier iteration over a shared blocker) already cleared
                # this edge between the fan-out resolver's enumeration read
                # and this re-read. Not an error.
                skipped.append(f"{OP_GATE_CASCADE_CLEAR}:{dep_path}:no-longer-blocked-on-reread")
                continue

            try:
                gcc_result = await gate_cascade_clear_handler(
                    {
                        "verb": "gate-cascade-clear",
                        "handoff_path": dep_path,
                        "blocker_ids": matched_ids,
                        "blocker_shas": [committed_sha] * len(matched_ids),
                    },
                    repo_root,
                )
            except Exception as exc:  # noqa: BLE001 -- soft-fail, never raise past this tail step
                _LOG.warning(
                    "post_commit_tail: gate-cascade-clear raised %s: %s", type(exc).__name__, exc
                )
                failed.append(f"{OP_GATE_CASCADE_CLEAR}:{dep_path}: {exc}")
                continue

            if not isinstance(gcc_result, dict):
                failed.append(f"{OP_GATE_CASCADE_CLEAR}:{dep_path}: malformed reply {gcc_result!r}")
                continue

            if gcc_result.get("exit_code") == 0:
                if gcc_result.get("applied"):
                    acted.append(dep_path)
                else:
                    skipped.append(
                        f"{OP_GATE_CASCADE_CLEAR}:{dep_path}:no-op: {gcc_result.get('message')}"
                    )
                continue

            error_message = gcc_result.get("error") or ""
            if _is_gate_cascade_clear_named_skip(error_message):
                skipped.append(f"{OP_GATE_CASCADE_CLEAR}:{dep_path}: {error_message}")
            else:
                # (b): every OTHER _gate_cascade_clear error — a lock
                # timeout, unparseable frontmatter, schema-validation
                # failure, a usage error — is a real problem and must
                # propagate, never be swallowed as "treat every error as a
                # no-op" would (module docstring "Third leg (C3)").
                failed.append(f"{OP_GATE_CASCADE_CLEAR}:{dep_path}: {error_message}")

    return {"acted": acted, "skipped": skipped, "failed": failed}


# ---------------------------------------------------------------------------
# Completion-entry commit-ledger fold (2026-08-30 spike verdict:
# docs/research/spike-verdicts/2026-08-30-the-completion-entrys-commit-
# ledger-folds-at-the-event.md). Appends THIS pass's own `committed_sha`
# into an already-written completion entry's `commits:` YAML list, at
# post-commit time, inside this tail -- never a corpus re-walk, never a
# `git log --grep` lookup (the sha is already a required `run()` param).
# `completion.reconcile_commits` (killed 2026-08-23, K-054) swept every
# entry after the fact at up to 7.9s wall/26-of-26-breach; this fold
# touches exactly the ONE entry named by its caller, at the moment the
# commit that should be folded into it lands.
# ---------------------------------------------------------------------------

#: Label for this leg's skip/fail strings — mirrors `OP_CLOSE_ORIGIN_STUB`'s
#: use as a string prefix, not a registered op name (this leg composes no
#: standalone op; see module docstring "Design" in the spike verdict).
OP_COMPLETION_ENTRY_FOLD = "completion.commit_ledger_fold"

#: The two allowed roots a caller-supplied entry path may resolve under —
#: mirrors `completion_ops._flip_to_released_handler`'s own dual-root
#: allow-list (`docs/plans/` / `archive/completed/`). A path escaping both
#: is refused rather than written, even though this leg is soft-fail —
#: writing outside these roots is never the status-quo-ante residue a
#: soft-fail is supposed to preserve.
_FOLD_ALLOWED_ROOT_SUFFIXES: tuple[tuple[str, ...], ...] = (
    ("docs", "plans"),
    ("archive", "completed"),
)


def _resolve_fold_entry_path(worktree_root: Path, entry_path: str) -> Optional[Path]:
    """Resolve and contain a caller-supplied completion-entry path against
    this worktree's `docs/plans/` and `archive/completed/` roots -- the
    same two roots `completion.flip_to_released` already confines its own
    writes to. Returns `None` (never raises) on an empty path, an
    unresolvable path, or one that escapes both roots -- the caller treats
    that as a clean skip, not a failure to diagnose.
    """
    if not entry_path:
        return None
    candidate = Path(entry_path)
    if not candidate.is_absolute():
        candidate = worktree_root / candidate
    allowed_roots = [worktree_root.joinpath(*suffix) for suffix in _FOLD_ALLOWED_ROOT_SUFFIXES]
    return contained_path(candidate, allowed_roots)


def _apply_commit_fold(content: str, sha: str) -> tuple[str, bool]:
    """Content-additive append of ``sha`` into the frontmatter ``commits:``
    YAML list -- handles both shapes a completion entry ships with today
    (`coordinator_complete_entry.py`'s ``commits: []`` and
    `ceremony/completion_entry.py`'s ``commits: []  # fill via
    completion.reconcile_commits (Step 2.6.8)``), plus any already-
    populated flow- or block-style list left by an earlier fold pass.

    Reuses `completion_ops`'s own `commits:` shape regexes/parsing (never a
    second, drifting parser) — mirrors `_parse_existing_commits`'s exact
    flow/block/any-other-shape discrimination so this write agrees with
    what `completion.flip_to_released` will later read back.

    Returns ``(content, False)`` unchanged when ``sha`` is already present
    (idempotent no-op — the caller never fires a commit for a no-op).
    Raises ``ValueError`` on a frontmatter shape this module cannot safely
    rewrite (no ``commits:`` key at all, or one matching neither the flow
    nor the block regex) — the caller catches this and soft-fails, per the
    spike verdict's constraint 6.
    """
    from coordinator_core.ops.completion_ops import (
        _COMMITS_ANY_RE,
        _COMMITS_BLOCK_RE,
        _COMMITS_FLOW_RE,
        _split_flow_items,
    )

    if sha in _parse_existing_commits(content):
        return content, False

    lines = content.splitlines(keepends=False)
    fence_idxs = [i for i, line in enumerate(lines) if line == "---"]
    if len(fence_idxs) < 2:
        raise ValueError("malformed frontmatter: fewer than two '---' fence lines")
    fm_start, fm_end = fence_idxs[0], fence_idxs[1]

    for i in range(fm_start + 1, fm_end):
        line = lines[i]
        flow = _COMMITS_FLOW_RE.match(line)
        if flow:
            items = _split_flow_items(flow.group(1))
            items.append(sha)
            rendered = ", ".join(f'"{item}"' for item in items)
            lines[i] = f"commits: [{rendered}]"
            return "\n".join(lines) + "\n", True

        if _COMMITS_BLOCK_RE.match(line):
            j = i + 1
            insert_at = i + 1
            while j < fm_end:
                if re.match(r"^\s+-\s", lines[j]):
                    insert_at = j + 1
                    j += 1
                    continue
                if re.match(r"^[a-zA-Z]", lines[j]):
                    break
                j += 1
            lines = lines[:insert_at] + [f'  - "{sha}"'] + lines[insert_at:]
            return "\n".join(lines) + "\n", True

        if _COMMITS_ANY_RE.match(line):
            raise ValueError(
                f"unrecognized commits: shape in frontmatter — refusing to fold: {line!r}"
            )

    raise ValueError("no commits: key found in frontmatter")


def _fold_sha_into_entry_on_disk(entry_path: Path, sha: str) -> bool:
    """Atomic temp-write + `os.replace` of ``entry_path`` with ``sha``
    appended to its `commits:` list — same content-additive-in-place
    discipline as `completion_ops.append_plan_session`'s no-repo-root
    fallback branch (DR-216 D2(iii)/D3), and, per this leg's HARD
    CONSTRAINT (module docstring), NO file lock: `post_commit_tail.run()`
    is the sole in-process caller of this leg, invoked once per landed
    ceremony commit -- there is no concurrent-writer hazard on the SAME
    entry path within one process the way a JSON-RPC-exposed op has to
    guard against.

    Returns `True` when a real rewrite happened, `False` on an idempotent
    no-op (the sha was already present). Raises whatever
    `_apply_commit_fold`/file I/O raises -- the caller (`_run_completion_
    entry_fold`) is the soft-fail boundary, not this function.
    """
    text = entry_path.read_text(encoding="utf-8")
    new_text, changed = _apply_commit_fold(text, sha)
    if not changed:
        return False

    dir_path = entry_path.parent
    fd, tmp_path = tempfile.mkstemp(dir=str(dir_path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(new_text)
        os.replace(tmp_path, str(entry_path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return True


def _compose_completion_fold_message(entry_relpath: str, committed_sha: str) -> str:
    """Compose the fold follow-up commit's message body -- sibling to
    `_compose_origin_stub_close_message`, same one-line-plus-blank-plus-
    path shape."""
    return (
        f"ceremony: fold commit into completion entry commits list "
        f"(sha={committed_sha})\n\n- {entry_relpath}\n"
    )


def _run_completion_entry_fold(
    worktree_root: Path,
    entry_path: str,
    committed_sha: str,
    push_mode: str = PUSH_MODE_SYNC,
) -> dict:
    """Fold `committed_sha` into the completion entry named by
    ``entry_path``'s `commits:` YAML list, then land that one-file rewrite
    in its own scoped commit. Synchronous — the caller
    (`_to_thread_completion_entry_fold`) is the `asyncio.to_thread` hop;
    this function itself runs the blocking FS + git work, mirroring
    `_commit_and_push_origin_stub_close`'s own split.

    Soft-fail contract (spike verdict constraint 6, module docstring):
    EVERY exception this function could raise -- a missing/unreadable
    entry, a frontmatter shape `_apply_commit_fold` refuses, a commit
    failure -- is caught by the caller and folded into `failed`, never
    propagated past `run()`. A stale `commits:` list is the status quo
    ante; this leg can only improve on it, never regress a ceremony that
    otherwise succeeded.

    Idempotent by sha membership (constraint 4): a second fold pass over
    the SAME entry with the SAME sha already present is a clean no-op --
    no rewrite, no commit, recorded as `skipped`.

    Returns a tail_ops-shaped `{acted, skipped, failed}` dict.
    """
    resolved = _resolve_fold_entry_path(worktree_root, entry_path)
    if resolved is None:
        return {
            "acted": [],
            "skipped": [f"{OP_COMPLETION_ENTRY_FOLD}:unresolvable-or-uncontained-entry-path"],
            "failed": [],
        }

    try:
        changed = _fold_sha_into_entry_on_disk(resolved, committed_sha)
    except Exception as exc:  # noqa: BLE001 -- soft-fail, never raise past this tail step
        _LOG.warning(
            "post_commit_tail: completion-entry commit-ledger fold failed for %s: %s",
            resolved, exc,
        )
        return {
            "acted": [],
            "skipped": [],
            "failed": [f"{OP_COMPLETION_ENTRY_FOLD}: {exc}"],
        }

    if not changed:
        return {
            "acted": [],
            "skipped": [f"{OP_COMPLETION_ENTRY_FOLD}:already-present"],
            "failed": [],
        }

    entry_relpath = resolved.relative_to(worktree_root).as_posix()
    message = _compose_completion_fold_message(entry_relpath, committed_sha)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(message)
        msg_path = fh.name
    try:
        commit_result = commit_scoped(
            [entry_relpath],
            msg_path,
            worktree_root,
            # `push.py`'s own set, not a local `== PUSH_MODE_SYNC`: this leg
            # takes `PUSH_MODE_NEVER` from the close ceremony (which runs its
            # own `push.outstanding` afterwards), and that mode suppresses the
            # post-commit hook too -- letting it fire would spend a second
            # push on work the caller is about to publish anyway.
            suppress_post_commit_auto_push=(
                push_mode in _PUSH_MODES_SUPPRESSING_POST_COMMIT_HOOK
            ),
        )
    finally:
        try:
            Path(msg_path).unlink()
        except OSError:
            pass

    if not commit_result.ok:
        return {
            "acted": [],
            "skipped": [],
            "failed": [f"{OP_COMPLETION_ENTRY_FOLD}: git commit failed: {commit_result.stderr}"],
        }

    if push_mode == PUSH_MODE_SYNC:
        push_outcome = push_with_retry(worktree_root, budget_secs=CEREMONY_PUSH_BUDGET_SECS)
        push_status = derive_push_status(push_outcome)
        if push_status == PUSH_STATUS_FAILED:
            reason = push_outcome.message or "; ".join(push_outcome.failed) or "unknown push failure"
            return {
                "acted": [entry_relpath],
                "skipped": [],
                "failed": [f"{OP_COMPLETION_ENTRY_FOLD}: git push failed: {reason}"],
            }
        if push_status in (
            PUSH_STATUS_CADENCE_PENDING,
            PUSH_STATUS_DECLINED,
            PUSH_STATUS_NO_REMOTE,
            PUSH_STATUS_UNCONFIRMED,
        ):
            return {
                "acted": [entry_relpath],
                "skipped": [f"{OP_COMPLETION_ENTRY_FOLD}:push:{push_status}"],
                "failed": [],
            }

    return {"acted": [entry_relpath], "skipped": [], "failed": []}


def fold_completion_entry_commit(
    worktree_root: Path,
    entry_path: str,
    committed_sha: str,
    *,
    push_mode: str = PUSH_MODE_NEVER,
) -> dict:
    """The completion-entry commit-ledger fold, as a seam a SYNCHRONOUS
    caller outside this module can reach.

    Why this exists rather than `run()`: `run()` composes five post-commit
    legs and is `async`, and its only live caller is `/execute-plan`'s
    close-out. The close ceremony that actually WRITES completion entries
    (`workstream_complete.apply`) is synchronous, reaches none of the other
    four legs, and — until this seam — had no way to fold its own commit into
    the entry `d-complete-entry` had just written. The fold therefore shipped
    dead: `run()` folded a supplied path correctly and nothing supplied one,
    so every completion entry's `commits:` list stayed empty. This is the one
    remaining wire.

    Defaults to `PUSH_MODE_NEVER` because that is what the close ceremony
    needs: it runs its own `push.outstanding` tail immediately afterwards
    (`apply._run_push_outstanding_tail`), which publishes this commit along
    with everything else the pass landed. Passing `PUSH_MODE_SYNC` here would
    buy a second push of the same work.

    Soft-fail and idempotency are `_run_completion_entry_fold`'s own
    contract, unchanged and not re-implemented here — a `{acted, skipped,
    failed}` dict either way, never a raise.
    """
    return _run_completion_entry_fold(worktree_root, entry_path, committed_sha, push_mode)


async def _to_thread_completion_entry_fold(
    worktree_root: Path,
    entry_path: str,
    committed_sha: str,
    push_mode: str,
) -> dict:
    """`asyncio.to_thread` wrapper around `_run_completion_entry_fold` --
    split out for the same event-loop-hygiene reason as
    `_to_thread_commit_and_push`."""
    import asyncio

    return await asyncio.to_thread(
        _run_completion_entry_fold, worktree_root, entry_path, committed_sha, push_mode
    )


async def _to_thread_commit_and_push(
    worktree_root: Path,
    closed_paths: list[str],
    committed_sha: str,
    push_mode: str,
    sid: Optional[str] = None,
) -> tuple[Optional[str], Optional[bool], str, Optional[str]]:
    """`asyncio.to_thread` wrapper around `_commit_and_push_origin_stub_close`
    -- split out purely so the import stays local to this call (AC6 event-
    loop hygiene, same rationale as `wsc_tail.py`'s own `to_thread` calls)."""
    import asyncio

    return await asyncio.to_thread(
        _commit_and_push_origin_stub_close, worktree_root, closed_paths, committed_sha, push_mode, sid
    )


# ---------------------------------------------------------------------------
# Composed op -- steps 5c (stamp+ship) + 5d (origin-stub close), in one call.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PostCommitTailOutcome:
    """Composed outcome of both post-commit tail steps -- see module
    docstring. `stamp_outcome` is `consumed_handoff_stamp`'s own
    `StampOutcome`; `origin_stub_result` is a tail_ops-shaped
    `{acted, skipped, failed}` dict, same shape `wsc_tail.py`'s own
    `tail_results[OP_CLOSE_ORIGIN_STUB]` entry has always carried."""

    stamp_outcome: consumed_handoff_stamp.StampOutcome = field(
        default_factory=consumed_handoff_stamp.StampOutcome
    )
    origin_stub_result: dict = field(
        default_factory=lambda: {"acted": [], "skipped": [], "failed": []}
    )
    #: C6b's second trigger — tail_ops-shaped `{acted, skipped, failed}`,
    #: same shape `origin_stub_result` carries. See module docstring
    #: "Second trigger (C6b)".
    deliverable_cascade_result: dict = field(
        default_factory=lambda: {"acted": [], "skipped": [], "failed": []}
    )
    #: C3's third leg — tail_ops-shaped `{acted, skipped, failed}`, same
    #: shape the other two legs carry. See module docstring "Third leg (C3)".
    gate_cascade_clear_result: dict = field(
        default_factory=lambda: {"acted": [], "skipped": [], "failed": []}
    )
    #: 2026-08-30 completion-entry commit-ledger fold — tail_ops-shaped
    #: `{acted, skipped, failed}`, same shape the other three legs carry.
    #: See module section "Completion-entry commit-ledger fold".
    completion_entry_fold_result: dict = field(
        default_factory=lambda: {"acted": [], "skipped": [], "failed": []}
    )


async def run(
    worktree_root: Path,
    common_dir: Path,
    sid: str,
    committed_sha: str,
    *,
    chain_terminal: bool,
    governing_plan_slug: str,
    initial_consumed: list[tuple[str, dict]],
    close_origin_stub_handler: Callable[[dict, Path], Awaitable[dict]],
    push_mode: str = PUSH_MODE_SYNC,
    timing: Optional[Any] = None,
    cascade_handler: Optional[Callable[[dict, Path], Awaitable[dict]]] = None,
    delivery_proof: Optional[dict] = None,
    gate_cascade_clear_handler: Optional[Callable[[dict, Path], Awaitable[dict]]] = None,
    completion_entry_path: Optional[str] = None,
) -> PostCommitTailOutcome:
    """Compose steps 5c (post-commit consumed-handoff stamp+ship), 5d
    (origin-stub close), and C6b's second trigger (deliverable cascade) into
    ONE in-process call. Runs UNLOCKED (DEC-3, and repo-wide since the
    2026-08-07 removal) -- see module docstring HARD CONSTRAINT; the caller
    must not wrap this in a ceremony-wide lock.

    `close_origin_stub_handler` is caller-injected (see module docstring
    "Origin-stub-close handler injection"). `delivery_proof` is OPTIONAL --
    forwarded verbatim into `_run_origin_stub_close` (see its own docstring);
    `None` (every `wsc_tail`-invoked call site, which has no proof of its
    own) preserves today's guard-only behaviour exactly. `cascade_handler`
    is OPTIONAL --
    when omitted, it is bound directly to `deliverable_cascade._handler`
    (REPOINTED 2026-08-27: `deliverable.cascade_terminal` is killed, so this
    no longer resolves via `get_op_handler` -- see the call site's own
    comment), so existing callers (`wsc_tail.py`) that predate C6b need no
    call-site change. `timing`, when supplied, records the SAME two named
    spans as before C6b ("stamp_and_ship", "origin_stub_close") -- see
    module docstring "Timing-span preservation"; the deliverable-cascade and
    gate-cascade-clear steps deliberately run untimed so as not to widen
    `wsc_tail.py`'s pinned step-name contract. `gate_cascade_clear_handler`
    (C3) mirrors `cascade_handler`'s own OPTIONAL shape -- when omitted, it
    is resolved here via `get_op_handler(OP_HANDOFF_TRANSITION)`, folding a
    future `OpSuspendedError` into the same not-registered skip (see the
    call site's own comment), so existing callers that predate C3 need no
    call-site change either. `completion_entry_path` (2026-08-30, module
    section "Completion-entry commit-ledger fold") is OPTIONAL -- the sha
    THIS pass just committed is already a required param above (constraint
    1 of the spike verdict: no `git log --grep` lookup on this path); the
    entry path is the one piece this leg cannot derive itself (`apply.py`'s
    `{d-complete-entry.entry_path}` token substitution is the sanctioned
    resolver, and this function's own negative-spec below forbids
    re-deriving it). `None` (every existing call site) is a clean skip, not
    a failure.

    All five steps run unconditionally in sequence -- a stamp+ship exception
    propagates BEFORE origin-stub close, the deliverable cascade, the
    gate-cascade-clear fan-out, or the completion-entry fold ever run
    (matches the pre-extraction inline sequencing exactly: a crash mid-stamp
    on the fresh pass must leave the origin stub untouched, recovered only
    on the AC18-resumed re-invoke). An origin-stub-close failure, a
    deliverable-cascade failure, a gate-cascade-clear failure, or a
    completion-entry-fold failure, by contrast, is caught and soft-failed
    inside its own helper -- none of the four propagates past this
    function.
    """
    with _measure(timing, "stamp_and_ship"):
        stamp_outcome = await consumed_handoff_stamp.post_commit_stamp_and_ship(
            worktree_root,
            common_dir,
            sid,
            committed_sha,
            chain_terminal=chain_terminal,
            push_mode=push_mode,
        )

    with _measure(timing, "origin_stub_close"):
        origin_stub_result = await _run_origin_stub_close(
            worktree_root,
            common_dir,
            committed_sha,
            governing_plan_slug,
            initial_consumed,
            close_origin_stub_handler,
            push_mode=push_mode,
            sid=sid,
            delivery_proof=delivery_proof,
        )

    resolved_cascade_handler = cascade_handler
    if resolved_cascade_handler is None:
        # REPOINTED 2026-08-27: `deliverable.cascade_terminal` is killed (K-104)
        # and `get_op_handler` now raises OpSuspendedError for it, which would
        # crash this tail rather than skip a step. The op is dead; its compute is
        # retained undecorated in `ops/deliverable_cascade.py` precisely for
        # in-process callers like this one. Bind it directly.
        #
        # The `is None` fallback below is kept rather than collapsed: an injected
        # `cascade_handler` still wins (every test injects one), and a failure to
        # import degrades to the existing not-registered skip instead of raising.
        try:
            from coordinator_core.ops.deliverable_cascade import (
                _handler as resolved_cascade_handler,
            )
        except Exception:
            resolved_cascade_handler = None

    # NOT wrapped in `_measure()` (unlike the two C3a-composed steps above):
    # `wsc_tail.py`'s own `_TailTiming` step-name set is a PINNED contract
    # (`test_timing_map_covers_every_instrumented_step_with_nonnegative_ms`,
    # C1) this trigger must not widen — see module docstring "Timing-span
    # preservation". This step still runs unconditionally; it is simply not
    # separately named in the timing map.
    deliverable_cascade_result = await _run_deliverable_cascade(
        worktree_root,
        common_dir,
        stamp_outcome.stamped,
        resolved_cascade_handler,
    )

    resolved_gate_cascade_clear_handler = gate_cascade_clear_handler
    if resolved_gate_cascade_clear_handler is None:
        # `handoff.transition` is not on the suspension roster today, but
        # `get_op_handler` raises `OpSuspendedError` rather than returning
        # None for any op that IS suspended (ipc.py's own docstring) -- fold
        # that raise into the existing not-registered branch below so a
        # future kill of this op degrades to `_run_gate_cascade_clear`'s
        # clean "not-registered" skip instead of crashing this tail AFTER
        # the ceremony commit has already landed (see module docstring's
        # deliverable-cascade precedent immediately above for the same fold).
        try:
            resolved_gate_cascade_clear_handler = get_op_handler(OP_HANDOFF_TRANSITION)
        except OpSuspendedError:
            resolved_gate_cascade_clear_handler = None

    # NOT wrapped in `_measure()` -- same rationale as the deliverable-cascade
    # step immediately above (module docstring "Timing-span preservation").
    gate_cascade_clear_result = await _run_gate_cascade_clear(
        worktree_root,
        common_dir,
        stamp_outcome.stamped,
        committed_sha,
        resolved_gate_cascade_clear_handler,
    )

    # 2026-08-30 completion-entry commit-ledger fold (spike verdict, module
    # section "Completion-entry commit-ledger fold"): a clean skip, not a
    # failed leg, when the caller has no entry path to give — every existing
    # call site (`close_out_and_stamp._reach_post_commit_tail_stub_close`)
    # predates this leg and passes none.
    if completion_entry_path:
        completion_entry_fold_result = await _to_thread_completion_entry_fold(
            worktree_root, completion_entry_path, committed_sha, push_mode
        )
    else:
        completion_entry_fold_result = {
            "acted": [],
            "skipped": [f"{OP_COMPLETION_ENTRY_FOLD}:no-entry-path-supplied"],
            "failed": [],
        }

    return PostCommitTailOutcome(
        stamp_outcome=stamp_outcome,
        origin_stub_result=origin_stub_result,
        deliverable_cascade_result=deliverable_cascade_result,
        gate_cascade_clear_result=gate_cascade_clear_result,
        completion_entry_fold_result=completion_entry_fold_result,
    )


# ---------------------------------------------------------------------------
# JSON-RPC entry point -- standalone dispatch (never invoked by wsc_tail.py,
# which calls `run()` directly, in-process, injecting its own
# `_close_origin_stub_handler` module-global -- see module docstring).
# ---------------------------------------------------------------------------



# `ceremony.post_commit_tail` was DELETED as an op 2026-08-27 under the 200ms
# process-time bar (kill ledger K-116). Only the standalone dispatch surface is
# gone: `wsc_tail.py` calls `run()` directly and never went through the handler
# (see this module's own docstring), so the in-process path is unchanged.
#
# The op could only ever fail now anyway -- it resolved deliverable.cascade_terminal
# and session.sweep_consumed_handoffs, both killed in the same sweep, so every
# dispatch raised OpSuspendedError. Killing it is the honest disposition; leaving
# a registered op that cannot succeed is not.
