"""
coordinator_core.ops.ceremony.wsc_tail -- the `ceremony.wsc_tail` orchestrator op.

Purpose: the C9 integration chunk of the wsc_tail rebuild
(docs/plans/2026-07-16-wsc-pure-python-tail-rebuild.md). Sequences the whole
single-pass ceremony -- resolve, pre-commit tail ops, the
stage/commit/push section, the post-commit consumed-handoff
ship-stamp, and the final receipt -- entirely in-process (AC1/AC2). This
module contains NO business logic of its own beyond sequencing; every step is
delegated to its owning C1-C8 module (see imports below).

Sequence
--------
  1. Initial resolve (in-process, read-mostly): `resolver.find_all_consumed_
     handoffs()` determines `chain_terminal` for THIS pass. This is
     deliberately the LIGHTWEIGHT resolver contract promoted in `resolver.py`
     -- not `branch_resolution.py`'s full 17-branch J/F/B interactive resolution
     (the surviving engine of the retired ceremony.wsc_resolve op), which exists
     to drive an EM Q&A turn between two op calls (the very
     phase boundary this single-pass rebuild collapses away, see module
     `resolver.py` docstring "before this module existed, wsc_tail would have
     had to reach across a module boundary ..."). `chain_terminal` here gates
     whether the C5 post-commit stamp pass runs at all (the completion-entry
     `chain_terminal` fill this used to also feed was removed in C4 Phase 2,
     2026-07-23 -- see the `completion_title` bullet below) -- C5's own
     `redrive_consumed_set()`
     re-derives the actual consumed-handoff SET fresh, immediately before the
     stamp write (AC6's correctness-critical liveness
     re-check; this module's initial resolve is NOT that re-check).
  2. Pre-commit tail ops (best-effort, `tail_ops.py`): refresh-roadmap-callout
     (step_2.75, native `refresh_roadmap_callout.main` port -- wired 2026-07-22,
     closing a C9 enumeration gap; its former render-handoff-tracker sibling
     was retired 2026-08-14, see docs/plans/2026-08-14-retire-the-handoff-
     tracker-and-project-tracker-renders.md C2), coverage.gate,
     review_trail.write. Archive-sweeps are NOT here -- see step 5f below;
     they fire POST-commit, never pre-commit (C2, corrected 2026-07-23).
  3. Trailer derivation: caller-supplied `trailers` is used verbatim when
     given; otherwise best-effort `commit.anchors` (COMPUTE_ONLY) is called
     after pre-staging the NON-DIVERGED subset of `stage_paths` (its `Plan:`
     trailer reads the STAGED diff -- see `commit_anchors.py`). A path whose
     staged content diverges from its worktree content (deliberate
     partial-hunk staging -- `coordinator_core.git.divergence.
     diverging_paths()`) is deliberately left un-staged here: `commit.
     anchors` still sees it via `git diff --cached --name-only` (it is
     already staged, just with further unstaged edits on top), and a bare
     `git add` on it would destroy the deliberate staging before any
     downstream commit-mechanism selector observes it (C10 fix, docs/plans/
     2026-07-27-computed-commit-mechanism-selection.md § C10 -- see
     `_derive_trailers`'s own docstring for the full incident). Only the
     non-diverged subset is pre-staged; `run_commit_pipeline()`'s own
     `explicit_stage()` re-stages that same subset -- `git
     add` is idempotent there, so this pre-stage is never a double-mutation.
  4. Commit-sentinel write (AC18, pre-commit): before the stage/commit/push
     section runs, an empty-content sentinel is written recording pass
     intent (mirrors OLD `wsc_commit.py:1884-1896`).
  5. Commit unit, then post-commit tail (wsc-tail-sub-2s-
     invoke-budget DEC-1/DEC-3, PM-ratified 2026-07-22 -- supersedes the
     original "one outer `ceremony_lock` spans 5a-5d" design; the mutex
     itself was later killed by PM ruling, excise-the-ceremony-lock,
     2026-08-07 -- neither this module nor `run_commit_pipeline` acquires
     any `ceremony_lock` on this op's live path today):
       a. `commit_pipeline.run_commit_pipeline()` (C4), run inside ONE
          `asyncio.to_thread` call so the ipc dispatch `wait_for` timer CAN
          fire during this section (AC6) -- gates (C3) + stage + commit +
          [push]. Formerly acquired/released its OWN internal
          `ceremony_lock` (name="wsc-commit"), which this module never
          wrapped in an outer hold (DEC-3 jettison -- see Negative-spec);
          that inner acquisition is itself now gone (excise-the-ceremony-
          lock, 2026-08-07). In `push_mode=
          "deferred"` (the default; DEC-1), stage -> gates -> commit run
          synchronously in this same call -- the network push is
          skipped here entirely.
       b. On a landed commit, the sentinel is UPDATED with the real
          `committed_sha` (AC18) via the `on_committed` callback passed into
          `run_commit_pipeline()` -- fired the instant the commit lands,
          still on the worker thread. Never
          re-derived from a racy late `git rev-parse HEAD`.
       c+d. `consumed_handoff_stamp.post_commit_stamp_and_ship()` (C5) --
          R1 (fresh liveness re-check) + R3 (future-date guard) + R4
          (live-children fail-closed) + stamp-then-ship + AC17 follow-up
          COMMIT (unconditional, always synchronous -- anti-scope) -- AND
          origin-stub close (2026-07-22 fold, cross-repo/inbox/2026-07-22-
          claude-central-em-wsc-tail-cutover-contract.md "Out of scope"
          § 2.7b), which closes the origin spinoff/spinoff-roadmap stub
          this session shipped, joining on `(roadmap_id, stub_id)` from
          `governing_plan_slug` (-> `docs/plans/<slug>.md`) and/or the
          first consumed handoff from step 1 -- are now composed into ONE
          standalone REGISTERED op, `ceremony.post_commit_tail`
          (`coordinator_core/ops/ceremony/post_commit_tail.py`, C3a,
          2026-07-23 wsc-tail-slim-down), invoked IN-PROCESS via that
          module's `run()` at this single call site (see the pointer
          comment in this module ahead of `_d_node`). This is a PURE
          refactor of C3a: both steps run UNLOCKED (DEC-3, no
          `ceremony_lock` here or in the new module either) exactly as
          before, in the same sequence, with the same two named
          `_TailTiming` spans ("stamp_and_ship"/"origin_stub_close"). Origin-
          stub close is placed post-commit (also UNLOCKED per DEC-3) rather
          than pre-commit like the OLD bash's Step 2.7b, because
          `committed_sha` -- needed to stamp `shipped_in` on the closed stub
          before it ships (schema `_cf_shipped_in_required`, same ordering
          constraint C5 above documents) -- does not exist until the main
          commit has landed. A closed stub's mutation lands in its OWN small
          follow-up commit, sibling to C5's AC17 follow-up commit, never
          left as an unswept dirty edit; its push is gated by `push_mode`
          too. Both steps run on BOTH the fresh pass and the AC18 resumed
          pass (see Crash-resumption below) -- post-commit work,
          unconditional of which path reached step 5. Origin-stub close is
          best-effort: a stub-close failure soft-fails into `tail_results`
          (contributes to `exit_code=2`, never `failed_critical` -- an
          unclosed stub after a landed commit is recoverable via the
          lvv-09 cadence backstop, not a breach) and never blocks the rest
          of the tail. C3b (a LATER, separate chunk -- NOT this one) moves
          the invocation of `ceremony.post_commit_tail` itself across the
          repo boundary; this landing keeps the call site exactly where it
          was.
       e. `push_mode="deferred"` ONLY (DEC-1): after 5a-5d complete, ONE
          detached background push (`auto_push.spawn_detached_push()`,
          reused from C2 -- never a second detach mechanism) is spawned for
          the whole tail, covering the ceremony commit AND any follow-up
          commits (git pushes the branch tip, not individual commits). A
          spawn failure is skip-loud -- logged, never raised, never halts
          an already-landed commit. See `_spawn_deferred_push_skip_loud()`.
       f. Archive-sweeps (C2, corrected 2026-07-23): `tail_ops.
          fire_archive_sweeps_detached()` fires the four per-class CLIs
          (`sweep-terminal-plans.py` / `sweep-shipped-handoffs.py` /
          `sweep-consumed-handoffs.py` / `sweep-actioned-memos.py`, never the
          composite `sweep-boot.py`) detached -- gated on `committed_sha is
          not None`, runs on BOTH a fresh landed commit AND a resumed pass
          (post-commit work either way), and does NOT run if the commit
          pipeline failed or there was nothing to commit. Placed AFTER 5a-5d,
          not inside pre-commit tail ops (step 2) -- the original landing
          fired this pre-commit, racing `run_commit_pipeline`'s own commit
          for `.git/index.lock` and presenting `dirty_tree_gate`'s
          whole-worktree scan with exactly the unattributable-dirty-file
          shape it hard-fails on. See `_handler`'s own inline comment at the
          fire site for the full incident writeup.
  6. F1 exit-verdict ordering for `cs_release_artifact` (EM call, documented
     here): mirrors the OLD `wsc_commit.py` design verified against
     `test_wsc_commit.py` (`test_no_governing_slug_skips_release` /
     `test_governing_slug_triggers_release` / C5 sidecar F5) --
     `cs_release_artifact(common_dir, "plan", governing_plan_slug)` runs
     ONLY when `governing_plan_slug` was supplied AND the commit did NOT
     genuinely fail (`commit_failed=False`) AND did NOT integrity-breach
     (landed locally but failed to push) -- releasing a plan claim on a
     failed/breached commit would orphan the workstream (claim gone,
     deliverable never shipped; OLD code's C5/F5 rationale, preserved here
     verbatim). Absent `governing_plan_slug`, release is skipped (not
     failed) -- a non-governing-plan session holds no such claim. Session-
     claim housekeeping, not worktree git state -- no `ceremony_lock`
     involvement, past or present -- and is best-effort (never raises,
     never flips the op's exit_code).
  7. Receipt emit (reuse `receipt_emit.emit_receipt`) -- a minimal
     `PipelineContext` (no J/F/B branch graph -- see step 1) carrying one
     D-node per tail step run, so `compute_op_tail` aggregates the SAME
     acted/skipped/failed/failed_critical partition the OLD two-phase op
     produced. Sentinel is cleared only on successful receipt emit (AC18).

Crash-resumption (AC18): on entry, if a sentinel already exists for `sid`
with a REAL committed_sha payload (not the empty pre-commit placeholder),
this is a resumed invocation -- pre-commit tail ops and `run_commit_pipeline`
are SKIPPED entirely (the commit already landed) and execution resumes
directly at steps 5c/5d (the post-commit stamp pass and origin-stub close,
both UNLOCKED per DEC-3) using the recovered `committed_sha`, keyed never on
a fresh `git rev-parse HEAD`. Stamp, origin-stub close, their respective
follow-up commits, and receipt emit are each individually idempotent, so a
doubly-resumed invocation (e.g. a second crash mid-stamp) is safe to
re-resume any number of times. Step 5e's detached-push spawn (deferred mode)
also still runs on a resumed pass -- it is unconditional of which path
reached the post-commit steps.

Push-mode / result-contract decision (wsc-tail-sub-2s-invoke-budget DEC-1/F4,
PM-ratified 2026-07-22): `push_mode` defaults to `"deferred"` -- the three
in-op synchronous pushes this op used to make (main pipeline push, C5 stamp
follow-up push, step-5d stub-close push) are ALL replaced by ONE detached
background push spawned after step 5d (see step 5e). Setting
`COORDINATOR_WSC_SYNC_PUSH=1` restores `push_mode="sync"` -- today's fully
synchronous, byte-for-byte behavior (all three pushes happen in-op, `pushed`/
`follow_up_pushed` are real booleans) -- for tests and any caller that
genuinely needs a synchronous confirmation. `push_status` result matrix
(6 values, mutually exclusive -- C6b, docs/plans/2026-08-08-the-push-leg-
that-never-asked-which-branch.md, reconciles this module's vocabulary
against `commit_pipeline.PUSH_STATUS_*`'s canonical five; see
`_CANONICAL_PUSH_STATUS_TO_LOCAL` for the literal mapping):
  - `"deferred"`   -- `push_mode="deferred"` (the default), NOT a resumed
                      pass. ALWAYS this value at result-return time, whether
                      or not a commit landed -- the detached child hasn't
                      run yet when the op returns, so `"failed"` can never
                      legitimately appear here (push health is out-of-band,
                      via `.git/push-failures.log`, never synchronous).
                      `pushed`/`follow_up_pushed` are `None`,
                      `integrity_breach` is `False`.
  - `"pushed"`     -- `push_mode="sync"`, NOT resumed, and either the
                      push(es) that were attempted genuinely landed, or the
                      pipeline never reached a push at all (canonical
                      `push_status="not-attempted"`, e.g. nothing to
                      commit) -- this second case is a DIFFERENT "no push
                      happened" reason than `"declined"` below and must
                      never collapse into it.
  - `"failed"`     -- `push_mode="sync"`, NOT resumed, and a push was
                      attempted and did NOT land (canonical
                      `push_status="push-failed"`).
  - `"declined"`   -- `push_mode="sync"`, NOT resumed, and a push was
                      refused by branch policy (canonical
                      `push_status="declined"`) -- a real policy refusal,
                      distinct from `"pushed"`'s not-attempted case above.
  - `"no-remote"`  -- `push_mode="sync"`, NOT resumed, and no remote is
                      configured (canonical `push_status="no-remote"`).
  - `"unknown_resumed"` -- ANY resumed pass (crash-recovered via the AC18
                      sentinel), regardless of `push_mode` -- the ORIGINAL
                      pre-crash push outcome was never persisted anywhere
                      this pass can recover, so `pushed`/`integrity_breach`
                      stay at their initialized `None`/`False`. Folds the
                      old standalone `push_status_unknown_resumed` boolean
                      into this one enum rather than shipping a second,
                      overlapping push-status signal (F4). Has NO
                      counterpart in `commit_pipeline`'s canonical set --
                      derived from resumption bookkeeping the pipeline has
                      no visibility into, and preserved as its own member
                      rather than dropped.
`cs_release_artifact` (step 6) reuses the release-under-unknown precedent
and releases under ALL SIX values -- see step 6 below.

Op-name decision (plan § "Notes for the reviewer"): registered as
`ceremony.wsc_tail` -- the `ceremony.wsc_commit` name (retired 2026-07-29,
kill-list op removal -- wsc_commit.py deleted outright) is DELIBERATELY NOT
reused/revived (kill-list anti-scope; see plan Anti-scope).

Spec backlink: pln-rebuild-the-wsc-commit-ceremon-f7c2a0 § C9;
docs/plans/2026-07-22-wsc-tail-sub-2s-invoke-budget.md § DEC-1/DEC-3/C3.

Negative-spec (hard-won):
  - Does NOT run the full `branch_resolution.py` 17-branch J/F/B resolution
    (the surviving engine of the retired ceremony.wsc_resolve op) -- that
    graph exists to drive an EM Q&A turn between two op calls; this
    single-pass op has no such turn (see step 1 above and `resolver.py`'s
    own docstring on the module-boundary reach this rebuild closes).
  - Does NOT re-derive `committed_sha` from a late `git rev-parse HEAD` at
    ANY point, including on crash-resumption -- always either the value
    `commit_pipeline.CommitOutcome.committed_sha` captured immediately
    post-commit, or the sentinel's recovered payload. Originally this
    freedom-from-races rested on a `ceremony_lock` hold spanning the probe
    and the capture; that lock is gone (excise-the-ceremony-lock,
    2026-08-07). The guarantee now rests structurally on C11
    (docs/plans/2026-08-07-excise-the-ceremony-lock.md § C11,
    `commit_pipeline.py`): on the private-index branch the sha is the
    CAS-verified `stdout` of the commit itself; on the agree branch it is a
    message-matched `git rev-list` result checked against the pre-commit
    sha, failing loud on ambiguity -- never a bare unqualified `rev-parse
    HEAD` that a racing peer commit could shift out from under this call.
    A THIRD leg exists past those two -- `run_commit_pipeline`'s post-push
    re-read, which fires once a push has landed (`push_status == PUSHED`)
    and used to be exactly the bare unqualified `rev-parse HEAD` this
    bullet warns against: a peer's push landing in the window between our
    own commit completing and that re-read running was silently adopted as
    ours (P1, 3 of 5 calls wrong in one traced session; fixed in
    `8d2e3cf3e4ee`). It is not bare today, but it is also not ABSENT the
    way the two legs above are -- it still re-reads, it just verifies the
    re-read before adopting it: `commit_pipeline.resolve_post_push_sha`
    checks ancestry first (our pre-push sha must remain an ancestor of the
    re-read tip -- a rebase REWRITES our commit off history entirely, while
    anything a peer or push machinery builds ON TOP of ours keeps it as an
    ancestor), then tree identity (separates a genuine rebase -- same diff,
    new parent, identical tree -- from an unrelated peer commit that
    happens to match ancestry some other way). Every failure mode (null
    input, a failed/blank re-read, an unresolvable tree, any mismatch)
    falls back to the caller's own pre-push value rather than inventing a
    sha. So the invariant this bullet states is true again, but for a
    verify-then-adopt reason, not because the post-push leg stopped
    re-reading.
  - Does NOT release a `governing_plan_slug` claim on a genuinely failed or
    integrity-breached commit -- see step 6 above.
  - Does NOT wire this op into `/workstream-complete` or any other
    auto-invoked surface -- additive/opt-in only (AC15); that cross-repo
    re-wiring is an explicitly later, out-of-this-plan memo to central.
  - Does NOT call `tail_ops.cs_archive` -- REMOVED 2026-07-31. The call
    used to run mid-`/workstream-complete`, moving
    `<git-common-dir>/coordinator-sessions/<sid>/` to `.archive/` out from
    under a still-live session and resetting eight fire-once sentinels
    homed under that dir (`.foreground-ok`, `.harness-bg-capable`,
    `unrouted-sizing-nudge.fired`, `harness-directive-nudge.fired`,
    `em-report-altitude-tally.json`, `em-report-altitude-nudged`,
    `multiwave-workflow-nudged`/`workflow-launched`,
    `exploration-tier-dispatch-offered`) -- observed live in DoE-claude.
    Archival is now SOLELY a SessionEnd-hook occasion: DoE
    `coordinator/hooks/scripts/sessionend-archive-session.py` ->
    `coordinator/bin/wsc-close.py archive-session` (this repo) ->
    `coordinator_core.session.scope.archive`, plus
    `ops/session/reap.py`'s 24h live-excluding backstop. If this ever
    returns, it MUST NOT precede `_clear_sentinel` (AC18's crash-window
    invariant this op still guarantees) -- see the originating memo,
    `cross-repo/inbox/2026-07-31-doe-claude-em-cs-archive-runs-mid-session.md`.
    `tail_ops.cs_archive` itself is untouched and remains callable by other
    sites.
  - Does NOT revive the `ceremony.wsc_commit` op name.
  - Does NOT reimplement `handoff.close_origin_stub`'s join/scan/guard
    logic -- step 5d composes that op's own `_handler` in-process (same
    "compose, don't reimplement" convention as `commit.anchors` /
    `handoff.stamp` / `handoff.transition` elsewhere in this module); the
    standalone `handoff.close_origin_stub` op remains independently
    registered and callable -- this is a fold-IN, not a move.
  - Does NOT let an origin-stub-close failure fail an already-landed
    ceremony commit -- it is a soft-fail (`tail_results` entry, contributes
    to `exit_code=2` at most), never `failed_critical`, never `exit_code=1`.
  - Does NOT hold an outer `ceremony_lock` around steps 5c/5d (DEC-3,
    PM-jettisoned 2026-07-22) -- this is a deliberate feature removal, not
    an omission; do NOT add one back. Originally this bullet's rationale was
    that `run_commit_pipeline`'s own internal lock (step 5a) was the only
    `ceremony_lock` acquired on this op's live path, never nested under an
    outer hold. That inner hold is ALSO gone now (excise-the-ceremony-lock,
    2026-08-07, PM-ratified) -- `run_commit_pipeline` no longer acquires any
    `ceremony_lock` at all, so there is nothing left on this op's live path
    to nest under. Do NOT reintroduce a lock at either layer.
  - Does NOT weaken AC17's commit leg -- the stamp follow-up COMMIT (and the
    stub-close follow-up COMMIT) stay synchronous and explicit-pathspec
    regardless of `push_mode`; ONLY the push half of each ever detaches.
  - A fired ipc-dispatch timeout during step 5a's `asyncio.to_thread` call
    MUST abandon-and-let-complete, never tear down ahead of the worker
    thread finishing its commit -- see the invariant comment at the
    `to_thread` call site (AC6).
  - Step 2's archive-sweeps do NOT run in-process any more (C2) -- no
    blocking call to `fleet.archive_completed_plans` / `fleet.
    archive_completed_handoffs` / `fleet.archive_actioned_memos` remains on
    this op's path; `tail_ops.fire_archive_sweeps_detached` fires four
    detached CLIs instead. Accepted latency, stated honestly: archival now
    completes shortly AFTER this op returns, not within it -- this trades
    strict promptness for the ~2.77s median blocking cost those three calls
    used to carry (plan's Baseline table), while still occasioning every
    archival class on every WSC pass (unlike a bare deletion, which would
    have left terminal plans and actioned memos with no in-session
    occasion at all -- plan § Execution Notes "Occasion map re-verified").
  - Never fires the composite `coordinator/bin/sweep-boot.py` from this op
    -- it also runs the unintegrated-findings reap (a tracked `git rm`),
    out of scope for a call fired on every ceremony pass.
  - `completion_title`/completion-entry scaffold -- REMOVED (C4 Phase 2,
    2026-07-23 wsc-tail-slim-down). DoE's `ba085887` (`coordinator/skills/
    workstream-complete/SKILL.md`) re-expanded Step 2.6 Action (a) to invoke
    `coordinator-complete-entry.py` directly and synchronously again,
    staging the written entry into the Step 3 ceremony commit via
    `WSC_PATHS`, and no longer passes `--completion-title` to `wsc-tail.py`.
    That landing made this op's own scaffold+residue-fill branch (formerly
    `_run_precommit_tail`'s `if completion_title:`) dead weight -- it is
    gone from this op entirely; `completion_entry.py` itself is untouched
    (DoE's CLI still fronts it directly). `completion_title` was ALREADY
    optional at this op layer before C4 Phase 1 (`params.get(
    "completion_title")` always tolerated absence) -- Phase 1 flipped only
    the *trampoline's* CLI flag (`coordinator/bin/wsc-tail.py`) to
    accept-and-ignore, which the trampoline still does (Phase 3, not yet
    landed, is the flag's own removal). This op no longer reads the param
    at all.
  - C6 (2026-07-23 wsc-tail-slim-down): coverage.gate's shed is ASYNCIO-level
    concurrency (`asyncio.create_task` fired early, joined late inside
    `_run_precommit_tail`), NOT an OS-process `detached_spawn.spawn_detached`
    fire-and-forget like the C2/C5 sheds -- a true detached child cannot hand
    its verdict back to this op's own receipt without either blocking on it
    (defeating "detached") or dropping the verdict for this pass (the exact
    fail-open shape `coverage-gate-perf.md`'s incident already burned once).
    The verdict is ALWAYS captured, in-process, before this op's receipt is
    built -- see `_run_precommit_tail`'s own docstring for the full
    reasoning. `review_trail.write` is explicitly NOT shed by C6 and must
    not be shed as a follow-on "cleanup" without a fresh PM ruling -- see
    `tail_ops.py`'s module docstring negative-spec for the cross-repo reason
    (DoE's `review-coverage-gate.py` is a sequential predecessor reader).
  - C7 (2026-07-23 wsc-tail-slim-down): the step-1 resolve's residue was
    UNCHANGED by this chunk at the time it landed -- an explicit consumer
    enumeration (see the comment above "Step 1: initial resolve" below)
    confirmed all five then-current readers of `initial_consumed`/
    `chain_terminal` (disposition, completion-entry fill, roadmap-callout,
    the stub-close join, receipt ctx) were still live, because C3a/C3b/
    C4-Phase-2/C5 had not moved their call sites out of this op yet. C4
    Phase 2 has SINCE landed (see the completion_title bullet above and the
    module docstring's C4 Phase 2 paragraph) and removed the completion-
    entry consumer -- the enumeration above "Step 1: initial resolve" is the
    CURRENT four-item residue; this bullet is retained as the historical
    record of what C7 itself found, not a claim about today's residue. Do
    NOT slim the resolve further on the assumption that C3b/C5 have also
    landed -- re-run the enumeration first when one of them actually does.
  - C8 (2026-07-23 wsc-tail-slim-down, AC7): `diagnostics` now also names
    any failure surfacing purely through `tail_results["commit_pipeline"]`
    or `tail_results["cs_release_artifact"]` (see
    `_append_named_tail_failures`) -- previously a `cs_release_artifact`-only
    failure left `diagnostics` empty, and `coordinator/bin/wsc-tail.py`'s
    CLI printed a bare `[]` instead of falling back to `tail_results` (the
    `diagnostics` KEY is always present on the wire payload, so the
    trampoline's `.get("diagnostics", ...)` fallback never fires). See the
    receipt-contract comment ahead of "Step 7: receipt emit" below for the
    full map of where every OTHER tail step's outcome is recorded today
    versus once C3b/C4-Phase-2/C5 actually land.
"""

from __future__ import annotations
import sys

# Generator-provenance declaration: this op is pure sequencing ("contains NO
# business logic of its own beyond sequencing; every step is delegated to
# its owning C1-C8 module" -- module docstring). Its own direct write
# (_write_sentinel) targets <common_dir>/coordinator-sessions/<sid>/
# wsc-tail-commit-landed under .git, never a tracked repo path.
GENERATES = []

import asyncio
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional, Union

from coordinator_core.git.divergence import diverging_paths
from coordinator_core.hooks import auto_push
from coordinator_core.ipc import get_op_handler, register_op
from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT
from coordinator_core.ops.ceremony import consumed_handoff_stamp, tail_ops
from coordinator_core.ops.ceremony import post_commit_tail
from coordinator_core.ops.ceremony.commit_message import SweptRenameError, parse_swept_rename
from coordinator_core.ops.ceremony.commit_pipeline import (
    PUSH_MODE_DEFERRED,
    PUSH_MODE_SYNC,
    PUSH_STATUS_DECLINED,
    PUSH_STATUS_FAILED,
    PUSH_STATUS_NO_REMOTE,
    PUSH_STATUS_NOT_ATTEMPTED,
    PUSH_STATUS_PUSHED,
    run_commit_pipeline,
)
from coordinator_core.ops.ceremony.git_native import add_paths
from coordinator_core.ops.ceremony.pipeline_context import PipelineContext
from coordinator_core.ops.ceremony.receipt_emit import emit_receipt
from coordinator_core.ops.ceremony.receipt_schema import make_d_node
from coordinator_core.ops.ceremony.wsc_disposition import SINGLE_SESSION, WRITE_TOKEN
from coordinator_core.ops.ceremony.resolver import (
    detect_git_provenance_consumed,
    find_all_consumed_handoffs,
)
from coordinator_core.ops.fleet._common import main_worktree_root, rel_id
from coordinator_core.ops.handoff_close_origin_stub import _handler as _close_origin_stub_handler

# Import side-effect only: register "commit.anchors" so get_op_handler() below
# resolves it via a direct registry hit rather than paying its lazy-import
# fallback (get_op_handler() self-resolves a MISS since 2026-07-25, so this
# pre-import is belt-and-braces, not strictly required for correctness) --
# mirrors the "# noqa: F401 -- trigger registration" idiom used throughout
# this rebuild -- see e.g. tail_ops.py.
import coordinator_core.ops.commit_anchors  # noqa: F401,E402

_LOG = logging.getLogger(__name__)

OP_NAME = "ceremony.wsc_tail"

#: Native-port cs_release_artifact claim class for a governing plan (mirrors
#: the OLD wsc_commit.py's `extra_args=["plan", governing_plan_slug]` --
#: verified against `test_wsc_commit.py::test_governing_slug_triggers_release`).
_GOVERNING_PLAN_ARTIFACT_CLASS = "plan"

#: Tail-result label for step 5d (origin-stub close fold, see module docstring).
OP_CLOSE_ORIGIN_STUB = "handoff.close_origin_stub"

_SENTINEL_NAME = "wsc-tail-commit-landed"

#: Env seam (DEC-1/F4, mirrors `COORDINATOR_AUTO_PUSH_SYNC`): "1" restores
#: `push_mode="sync"` -- today's fully synchronous push behavior, byte-for-
#: byte -- for tests and callers that genuinely need a synchronous push
#: confirmation. Unset/anything else keeps the default `"deferred"` mode.
_ENV_SYNC_PUSH = "COORDINATOR_WSC_SYNC_PUSH"

#: C6b (docs/plans/2026-08-08-the-push-leg-that-never-asked-which-branch.md):
#: canonical `commit_pipeline.PUSH_STATUS_*` -> this module's own
#: `push_status` spelling, for the `push_mode="sync"`, NOT-resumed case (see
#: module docstring result matrix). `PUSH_STATUS_NOT_ATTEMPTED` (no push ever
#: reached the pipeline -- e.g. nothing to commit, commit itself failed)
#: intentionally maps to this module's `"pushed"`, matching the pre-C6b
#: behavior for that case -- it is a DIFFERENT "no push happened" reason than
#: a policy `"declined"` refusal, and must stay distinct from it, never
#: collapsed together.
_CANONICAL_PUSH_STATUS_TO_LOCAL = {
    PUSH_STATUS_PUSHED: "pushed",
    PUSH_STATUS_FAILED: "failed",
    PUSH_STATUS_DECLINED: "declined",
    PUSH_STATUS_NO_REMOTE: "no-remote",
    PUSH_STATUS_NOT_ATTEMPTED: "pushed",
}

#: Non-tail-step D-node id/resolving_op carrying this pass's per-step wall-clock
#: timing map (see `_TailTiming` below). `tail_step=False` -- this node is a pure
#: measurement annotation and must NEVER feed `compute_op_tail`'s
#: acted/skipped/failed aggregation (see `receipt_schema.compute_op_tail`, which
#: filters on `tail_step=True`).
_TIMING_NODE_ID = "wsc-tail-timing"
_TIMING_RESOLVING_OP = "wsc_tail.timing"


class _TailTiming:
    """Per-step wall-clock instrumentation for `ceremony.wsc_tail` (docs/plans/
    2026-07-23-wsc-tail-slim-down.md C1).

    Every cost claim about this op was, before this class existed, unmeasured
    inference -- there was zero timing instrumentation anywhere on this path
    (wsc_tail / tail_ops / commit_pipeline / consumed_handoff_stamp). This is
    the measurement, not a business-logic change: `measure()` wraps a step
    with `time.perf_counter()` (monotonic, portable -- no POSIX-only call, no
    `time.time()`) and never alters what the wrapped code does or returns.

    Step names are STABLE, greppable identifiers -- a contract, not debug
    output (a later chunk asserts on this map's step membership/count). Order
    is insertion order, mirroring the sequence the steps actually ran in for
    THIS pass (membership varies between a fresh and an AC18-resumed pass --
    see module docstring "Crash-resumption").

    Timer failure must never fail the ceremony: `measure()` swallows any
    exception raised while computing/recording the elapsed time itself (not
    exceptions from the wrapped body, which propagate normally) and simply
    omits that one entry rather than raising past a real ceremony step.
    """

    def __init__(self) -> None:
        self._steps: list[dict[str, Any]] = []

    @contextmanager
    def measure(self, name: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            try:
                elapsed_ms = (time.perf_counter() - start) * 1000.0
                self._steps.append({"step": name, "ms": elapsed_ms})
            except Exception as exc:  # noqa: BLE001 -- a timer failure must never fail the ceremony
                _LOG.warning(
                    "wsc_tail: timing capture for step %r failed (skip-loud): %s: %s",
                    name, type(exc).__name__, exc,
                )

    def as_list(self) -> list[dict[str, Any]]:
        return list(self._steps)


def _resolve_push_mode() -> str:
    return PUSH_MODE_SYNC if os.environ.get(_ENV_SYNC_PUSH) == "1" else PUSH_MODE_DEFERRED


def _spawn_deferred_push_skip_loud(worktree_root: Path) -> None:
    """Spawn ONE detached background push for the whole tail (DEC-1, step
    5e) -- covers the main ceremony commit and any follow-up commits, since
    git pushes the branch tip, not individual commits. Reuses C2's engine-
    facing `auto_push.spawn_detached_push()` (respawn-Popen, both platforms)
    -- never a second detach mechanism (plan anti-scope).

    Skip-loud: a failure here (branch unresolvable, spawn itself raises)
    never halts an already-landed commit -- logged and swallowed. Push
    health is observable out-of-band via `.git/push-failures.log`, never
    synchronously from this op's result (see module docstring push_status
    matrix).
    """
    try:
        branch = auto_push.resolve_branch(str(worktree_root))
        if not branch:
            _LOG.warning(
                "wsc_tail: deferred push skipped -- no current branch resolved for %s",
                worktree_root,
            )
            return
        auto_push.spawn_detached_push(str(worktree_root), branch)
    except Exception as exc:  # noqa: BLE001 -- skip-loud, never halt an already-landed commit
        _LOG.warning(
            "wsc_tail: deferred push spawn failed (skip-loud) for %s: %s: %s",
            worktree_root, type(exc).__name__, exc,
        )


def _sentinel_path(common_dir: Path, sid: str) -> Path:
    """Sentinel path for the AC18 crash-resumption design.

    Mirrors the OLD `wsc_commit.py:1884-1896` sentinel-under-claim-dir
    convention (`<common_dir>/coordinator-sessions/<sid>/...`), renamed
    `wsc-tail-commit-landed` to avoid colliding with the dormant
    `ceremony.wsc_commit`'s own `wsc-commit-landed` sentinel should both
    ever coexist in the same session directory.
    """
    return common_dir / "coordinator-sessions" / sid / _SENTINEL_NAME


def _write_sentinel(path: Path, content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _read_sentinel_sha(path: Path) -> Optional[str]:
    """Return the recovered committed_sha from an existing sentinel, or None.

    None covers both "no sentinel" (fresh pass) and "sentinel exists but
    empty" (pre-commit placeholder -- crashed before the commit landed; a
    fresh pass must be re-run from the top, not resumed at the stamp step).
    """
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError:
        print(f"skip: _read_sentinel_sha: content = path.read_text(encoding=\"utf-8\").strip() failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    return content or None


def _clear_sentinel(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        print(f"skip: _clear_sentinel: path.unlink() failed: {sys.exc_info()[1]}", file=sys.stderr)
        pass


def _derive_trailers(
    common_dir: Path,
    sid: str,
    stage_paths: list[str],
    nature: Optional[str],
    explicit_trailers: str,
) -> str:
    """Resolve the trailer block for the composed commit message.

    Caller-supplied `explicit_trailers` wins verbatim. Otherwise best-effort
    `commit.anchors` (COMPUTE_ONLY) -- its `Plan:` trailer reads the STAGED
    diff, so the SAFE subset of `stage_paths` is pre-staged here (see
    `diverging_paths()` split below).

    `stage_paths` is ALSO passed to `commit.anchors` as its `paths` scope, and
    that is load-bearing on a shared worktree: without it the op reads the
    whole index and answers "what is staged in the repo", not "what is in
    THIS commit", so a peer's staged plan becomes this commit's `Plan:`/
    `Deliverable-Id:`. See `commit_anchors._staged_files` for the measured
    incident (ship commit 582c7b510).

    C10 fix (docs/plans/2026-07-27-computed-commit-mechanism-selection.md
    § C10) -- the pre-stage used to be an unconditional `git add -- stage_
    paths` against the SHARED index, justified as idempotent ("git add on an
    already-staged path is a no-op"). That holds ONLY when index and
    worktree already AGREE for a path. On a path with deliberate PARTIAL-
    HUNK staging (index differs from HEAD *and* worktree differs from index
    -- see `coordinator_core.git.divergence.diverging_paths`), a bare `git
    add` OVERWRITES the operator's deliberately-staged content with
    worktree content, destroying the divergence before any downstream
    commit-mechanism selector (`git_native.commit_scoped()`) ever gets a
    chance to observe it -- reproducing the claude-klabauter 506748a0
    incident THROUGH this op instead of through a hand-typed `git commit --
    <paths>`.

    Fix: split `stage_paths` into the diverged subset (left untouched -- its
    staged content is EXACTLY what a caller deliberately staged, and it is
    already visible to `git diff --cached --name-only` without any further
    `git add`) and the non-diverged subset (staging is genuinely idempotent
    for these -- either already agreeing, or not yet staged at all, in which
    case `git add` is the correct, non-destructive first stage). Only the
    non-diverged subset is ever passed to `add_paths()`.

    This closes ONLY this module's own contribution to the race -- it does
    NOT make `run_commit_pipeline()`'s own `explicit_stage()` (C4,
    `commit_pipeline.py`) divergence-aware; that is a separate chunk's
    file and is out of this function's remit. See this chunk's DONE report
    for the residual gap.
    """
    if explicit_trailers:
        return explicit_trailers

    if stage_paths:
        worktree_root = main_worktree_root(common_dir)
        diverged = set(diverging_paths(stage_paths, cwd=str(worktree_root)))
        safe_paths = [p for p in stage_paths if p not in diverged]
        if diverged:
            _LOG.warning(
                "wsc_tail: trailer derivation skipping pre-stage for %d "
                "diverged path(s) (deliberate partial-hunk staging preserved "
                "verbatim, never re-added): %s",
                len(diverged), sorted(diverged),
            )
        if safe_paths:
            add_paths(worktree_root, safe_paths)

    handler = get_op_handler("commit.anchors")
    if handler is None:
        return ""
    try:
        result = handler(
            {"session_id": sid, "nature": nature, "paths": list(stage_paths or [])},
            common_dir,
        )
        return str(result.get("trailers", ""))
    except Exception as exc:  # noqa: BLE001 -- best-effort trailer derivation, never raises
        _LOG.warning("wsc_tail: commit.anchors raised %s: %s", type(exc).__name__, exc)
        return ""


#: Literal residue markers `coordinator_complete_entry.py`'s `_write_entry`
#: leaves in a freshly-scaffolded `archive/completed/` entry -- see
#: `_scan_completion_entry_scaffold_residue` below.
_COMPLETION_SCAFFOLD_TITLE_RESIDUE = "PLACEHOLDER"
_COMPLETION_SCAFFOLD_PROSE_RESIDUE = "<!-- PROSE:"


def _scan_completion_entry_scaffold_residue(
    stage_paths: list[str], worktree_root: Path
) -> list[str]:
    """Fail-loud precommit gate (bug 2026-07-28-workstream-complete-d-complete-
    entry-emi-f6be5553dee4): refuse to let a `coordinator-complete-entry`
    scaffold ride into a ceremony commit with its model-residue placeholders
    still in place.

    `coordinator_core.ops.coordinator_complete_entry._write_entry` deliberately
    writes a content-free scaffold -- a literal `title: "PLACEHOLDER --
    replace with past-tense workstream title"`, `nature: null` /
    `nature_inferred: true` when `--nature` was omitted, and an always-present
    `<!-- PROSE: ... -->` stub -- the EM is expected to hand-fill all three
    before the ceremony commit lands. Nothing enforced that fill-in once this
    op's own `--completion-title` fill path was removed (C4 Phase 2,
    2026-07-23, wsc-tail-slim-down -- see this module's docstring
    "completion_title" bullet): the scaffold could ride straight into
    `archive/completed/` with its placeholders intact and this op still
    returned `exit_code=0` (observed 2026-07-28, commit `1c4de5f2b`).

    The gate sits at the COMMIT boundary (this call site, inside `_handler`,
    immediately before the commit unit), not at the scaffold
    (`coordinator_complete_entry.py` itself, untouched by this bugfix) -- the
    scaffold is legitimately content-free at write time; the commit is where
    the artifact stops being editable scratch and becomes durable,
    downstream-read content (release notes, roadmap rollup, and chain-LOE
    aggregation all read `archive/completed/`).

    Negative-spec: does NOT restore the removed `--completion-title` fill
    path (C4 Phase 2 deleted it deliberately) -- this is a refusal gate, not
    a revived fill-in mechanism.

    Returns a list of diagnostic strings (empty when clean) -- one entry per
    (path, residue-kind) pair found, in the same named-and-actionable
    register as `git_native.commit_scoped`'s own refusal diagnostics (see
    that function's directory-pathspec / empty-path-set guards).
    """
    diagnostics: list[str] = []
    for raw_path in stage_paths:
        norm = raw_path.replace(os.sep, "/")
        if "archive/completed/" not in norm:
            continue
        full = worktree_root / raw_path
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            print(
                f"skip: _scan_completion_entry_scaffold_residue: "
                f"full.read_text(...) failed for {raw_path!r}: {sys.exc_info()[1]}",
                file=sys.stderr,
            )
            continue

        title_line = next(
            (line for line in text.splitlines() if line.startswith("title:")), ""
        )
        if _COMPLETION_SCAFFOLD_TITLE_RESIDUE in title_line:
            diagnostics.append(
                f"wsc_tail: completion-entry scaffold residue in {raw_path!r} -- "
                f"title still carries the literal scaffold placeholder "
                f"({title_line.strip()!r}). Hand-write the past-tense workstream "
                "title into this file before this commit."
            )
        if _COMPLETION_SCAFFOLD_PROSE_RESIDUE in text:
            diagnostics.append(
                f"wsc_tail: completion-entry scaffold residue in {raw_path!r} -- "
                "the `<!-- PROSE: ... -->` stub is still present. Hand-write the "
                "completion prose into this file before this commit."
            )
        if "nature: null" in text and "nature_inferred: true" in text:
            diagnostics.append(
                f"wsc_tail: completion-entry scaffold residue in {raw_path!r} -- "
                "`nature: null` / `nature_inferred: true` still unset. Resolve "
                "the completion nature (roadmap|bugfix|tech-debt|infra) in this "
                "file before this commit."
            )
    return diagnostics


def _swept_srcs_dsts(swept_renames: list[str]) -> tuple[list[str], list[str]]:
    srcs: list[str] = []
    dsts: list[str] = []
    for raw in swept_renames:
        try:
            src, dst = parse_swept_rename(raw)
        except SweptRenameError as exc:
            _LOG.warning("wsc_tail: dropping malformed swept_rename %r: %s", raw, exc)
            continue
        srcs.append(src)
        dsts.append(dst)
    return srcs, dsts


# ---------------------------------------------------------------------------
# Step 5c+5d -- EXTRACTED (C3a, 2026-07-23 wsc-tail-slim-down § C3a) into the
# standalone REGISTERED `ceremony.post_commit_tail` op
# (`coordinator_core/ops/ceremony/post_commit_tail.py`). This module still
# invokes that op's `run()` IN-PROCESS at both call sites below, on both the
# fresh pass and the AC18-resumed pass, exactly as it invoked the two steps
# inline before this extraction -- pure refactor, behaviour-identical. See
# that module's docstring for the origin-stub-close handler-injection design
# (why `_close_origin_stub_handler` below stays a module-global name here)
# and the timing-span-preservation design (why "stamp_and_ship" /
# "origin_stub_close" remain two separately-named `_TailTiming` spans).
# ---------------------------------------------------------------------------


def _commit_gap_reason(commit_failed: bool, sha_unverified: bool) -> str:
    """Discriminator string for every `tail_results[...]["skipped"/"failed"]`
    entry that explains WHY a post-commit step did not run (no resolvable
    `committed_sha` to key off of).

    W3 (docs/plans/2026-08-08-a-landed-commit-reported-as-failed.md): this
    used to be a binary `'commit-failed' if commit_failed else
    'nothing-to-commit'` repeated at every call site in this module -- both
    branches wrong for the third real state `commit_pipeline.PipelineResult.
    sha_unverified` describes: the commit LANDED (history changed, it is
    NOT a no-op) but its sha could not be resolved, so this op still cannot
    key a post-commit step (stamp, origin-stub close, archive sweeps) off a
    real sha. `sha_unverified` is checked first -- it can be True only when
    `commit_failed` is False (see `commit_pipeline.run_commit_pipeline`'s own
    `landed` split), so there is no ambiguity between the two.
    """
    if sha_unverified:
        return "landed-sha-unverified"
    return "commit-failed" if commit_failed else "nothing-to-commit"


def _d_node(node_id: str, resolving_op: str, tail_result: dict) -> dict:
    """Wrap a tail_ops-shaped {acted, skipped, failed[, failed_critical]}
    result dict into a schema-valid tail D-node -- feeds `compute_op_tail`.
    """
    return make_d_node(node_id, resolving_op=resolving_op, evidence=tail_result, tail_step=True)


# C8 (2026-07-23 wsc-tail-slim-down, AC7): the two tail labels that stay
# genuinely in-op after C7's step-1 slim -- see the receipt-contract comment
# ahead of "Step 7: receipt emit" below for the full map of where every OTHER
# tail step's outcome lands. `commit_pipeline` and `cs_release_artifact` are
# named here, individually, because they are the ONLY labels whose failure
# can surface with nothing else in `diagnostics` to explain it (see
# `_append_named_tail_failures` docstring for the incident this fixes).
#
# Negative-spec: do NOT widen this to every `tail_results` label
# unconditionally -- a blanket "name everything that failed" pass would
# duplicate `pipeline_result.diagnostics` (already extended into `diagnostics`
# above the commit-pipeline call) and would re-litigate the C6/C17
# disposition of where OTHER steps' failures belong (detached-fire steps
# report via the shared housekeeping-failures log, not this op's synchronous
# `diagnostics`). C4-Phase-2 has landed and did not touch either label in this
# tuple. A future shed chunk (C3b/C5) that removes one of these two labels
# from `tail_results` entirely must also remove it from this tuple -- naming a
# label that no longer exists is silently a no-op here, not a loud failure, so
# don't let this tuple drift stale.
_DIAGNOSTIC_NAMED_LABELS = ("commit_pipeline", "cs_release_artifact")


def _append_named_tail_failures(
    diagnostics: list, tail_results: dict, timing: "_TailTiming"
) -> None:
    """Name the failing tail item in `diagnostics` for the labels in
    `_DIAGNOSTIC_NAMED_LABELS`, with that step's own wall-clock timing
    (post-C1) folded into the message.

    The bug this fixes (AC7, "the `[]` wart"): before this helper existed,
    `diagnostics` was populated ONLY from `pipeline_result.diagnostics` and
    Detector B's warnings (see the step-1 resolve above) -- a failure
    surfacing purely through `tail_results[label]["failed"]` (e.g.
    `cs_release_artifact`'s holder-changed/TOCTOU guard tripping, with the
    main commit itself having landed clean) left `diagnostics` genuinely
    empty. `coordinator/bin/wsc-tail.py`'s CLI trampoline reads
    `result.get("diagnostics", result.get("tail_results"))` -- since the
    `diagnostics` KEY is always present on the wire payload (even as `[]`),
    the `tail_results` fallback never fires, and the caller sees a bare
    printed `[]` while the real failure sits unexamined one level down in
    `tail_results`. Called unconditionally (not gated on `exit_code`) --
    a label's own `failed` list is the trigger, not the aggregate verdict.
    """
    timing_by_step = {entry["step"]: entry["ms"] for entry in timing.as_list()}
    for label in _DIAGNOSTIC_NAMED_LABELS:
        failed = (tail_results.get(label) or {}).get("failed") or []
        if not failed:
            continue
        ms = timing_by_step.get(label)
        timing_suffix = f" ({ms:.1f}ms)" if ms is not None else ""
        diagnostics.append(
            f"{label} failed{timing_suffix}: {'; '.join(str(f) for f in failed)}"
        )


async def _run_precommit_tail(
    common_dir: Path,
    worktree_root: Path,
    sid: str,
    *,
    review_trail: "Optional[Union[dict, list]]",
    b_adjudication_present: bool,
    partition_mandatory: bool = False,
    coverage_range: str,
    coverage_from_handoff: str,
    coverage_scope_paths: Optional[list[str]],
    consumed_handoff_paths: list[str],
    timing: Optional[_TailTiming] = None,
) -> tuple[dict[str, dict], list[str]]:
    """Run every pre-commit tail op (C6 + refresh-roadmap-callout (step_2.75,
    2026-07-22 C9 wiring-gap fix; its former render-handoff-tracker sibling was
    retired 2026-08-14) + coverage.gate + review_trail), best-effort. Returns
    (results_by_label, extra_stage_paths).

    `extra_stage_paths` folds the render-produced artifact paths
    (roadmap-callout stub index) into the caller's stage set so they
    land in the SAME ceremony commit (AC5 explicit pathspec) rather than as
    unswept dirty edits -- and, AC12/C7b, this session's chain-ancestry
    waiver subdirectory (if one exists on disk from an earlier, aborted
    HALT pass for this same sid -- see
    `_pending_chain_ancestry_waiver_stage_path`).

    `consumed_handoff_paths` feeds `tail_ops.refresh_roadmap_callout` -- the same
    `initial_consumed` set the step-1 resolve already derived (see `_handler`),
    threaded straight through rather than re-resolved here.

    `partition_mandatory` (D, cross-repo/inbox/2026-08-15-example-retrieval-repo-em-wsc-
    review-trail-skips-silently.md): the SAME fail-loud posture as
    `b_adjudication_present`, forwarded to `tail_ops.write_review_trail`/
    `write_review_trail_many` alongside it -- a resolved review-scale row 4/6
    is as strong a statement that a review was owed as an adjudication
    assertion is. Defaults `False` for the same back-compat reason
    `b_adjudication_present` itself has no default here: every existing
    direct call site keeps passing it explicitly and is unaffected; this
    default only matters to a hypothetical future caller that omits it.

    `timing` records named sub-step spans (`precommit.tracker_and_roadmap`,
    `precommit.coverage_gate_fire`, `precommit.coverage_gate_join`,
    `precommit.review_trail`) -- see `_TailTiming`. The completion-entry
    scaffold's own `precommit.completion_entry` span was removed with the
    scaffold itself (C4 Phase 2, 2026-07-23 wsc-tail-slim-down -- see module
    docstring). Archive-sweeps are NOT recorded here any more (C2,
    corrected 2026-07-23) -- they fire post-commit under the
    `postcommit.archive_sweeps_detached` span in `_handler` itself, not as a
    sub-step of this pre-commit function.
    Optional/back-compat: a caller that does not pass one (e.g. an existing
    direct-unit-test call site) gets a throwaway private recorder instead of
    a `TypeError` -- this function's own timing contract is unaffected either
    way, only whether anyone reads it back.

    coverage.gate concurrency (C6, 2026-07-23 wsc-tail-slim-down): unlike the
    archive-sweeps / tracker-roadmap sheds (C2/C5), `coverage.gate` is NOT
    fired as a detached OS-level child (`detached_spawn.spawn_detached`). A
    true fire-and-forget child cannot hand its verdict back to THIS call's
    receipt without either (a) blocking on it anyway (defeating "detached"),
    or (b) accepting a missing/stale verdict for this pass -- which is
    exactly the fail-open shape `coverage-gate-perf.md`'s incident already
    burned us on once. `coverage.gate` is also, unlike the archive/tracker
    sheds, already a plain in-process async call that never touches the
    tracked worktree or git index (it shells out to read-only `git log` and
    persists its own artifact to the gitignored `state/coverage/gate-
    result.json` -- see `coverage_gate.py`), so the C2 index.lock / dirty-
    tree-gate hazard that forces the archive sweeps and tracker/roadmap
    renders onto the POST-commit side of the pipeline does not apply to it;
    it is safe to run concurrently with anything else in this pipeline,
    commit included. The shed here is therefore ASYNCIO-level, not
    OS-process-level: the call is *fired* immediately (`asyncio.create_task`)
    before the other pre-commit steps run, so its own `git log` + gate-
    algorithm work overlaps with whatever this coroutine does next (most of
    which -- `write_review_trail`'s own await, and this whole function
    returning control back up to `_handler`'s `trailer_derivation` /
    `commit_pipeline` `asyncio.to_thread` calls -- genuinely yields control
    back to the event loop), then *joined* (awaited) only once every other
    pre-commit step has run, right before this function returns. The verdict
    is captured synchronously, in-process, in the SAME call that builds the
    receipt -- never dropped, never stale.
    """
    if timing is None:
        timing = _TailTiming()
    results: dict[str, dict] = {}

    # coverage.gate's ceremony-close fire/join was removed here (K-001,
    # state/kill-ledger.md) -- the DAG walk it drove cost ~150-180s per
    # close. `coverage_range`/`coverage_from_handoff`/`coverage_scope_paths`
    # stay accepted-and-ignored, same precedent as `completion_title` above
    # in this module's own history, so an existing caller passing them is
    # unaffected.

    extra_stage_paths: list[str] = []

    # STEP_2_75 (2026-07-22 C9 wiring-gap fix; handoff-tracker leg retired
    # 2026-08-14, see docs/plans/2026-08-14-retire-the-handoff-tracker-and-
    # project-tracker-renders.md C2): refresh-roadmap-callout -- a disposable
    # sibling render, positioned before coverage.gate exactly as in the OLD
    # wsc_commit.py's Op 4 (STEP_2_7 stamp+archive -> STEP_2_75 render pair ->
    # STEP_2_9C coverage.gate).
    #
    # The render writes/rewrites an in-worktree file that is NOT otherwise part
    # of the caller's explicit stage_paths -- `commit_pipeline.run_commit_pipeline`'s
    # `dirty_tree_gate` scans the WHOLE worktree (not just the explicit pathspec) and
    # would flag it as an "unattributable" dirty path, hard-failing the ceremony
    # commit. Folding its artifact paths into `extra_stage_paths` closes that gap;
    # `git add` on an already-clean/no-op file is a harmless idempotent no-op.
    with timing.measure("precommit.tracker_and_roadmap"):
        roadmap_callout_result = tail_ops.refresh_roadmap_callout(worktree_root, consumed_handoff_paths)
        results[tail_ops.OP_ROADMAP_CALLOUT] = roadmap_callout_result
        extra_stage_paths.extend(roadmap_callout_result.get("roadmap_stub_index_paths") or [])

        # AC12/C7b chain-ancestry-waiver stage-path fold-in is REMOVED
        # (state/kill-ledger.md K-005, 2026-08-16 -- "waiver system dies"):
        # the whole mint/waiver mechanism this closed the ordering gap for
        # is gone.

    with timing.measure("precommit.review_trail"):
        if isinstance(review_trail, list):
            results[tail_ops.OP_REVIEW_TRAIL] = await tail_ops.write_review_trail_many(
                common_dir, review_trail,
                b_adjudication_present=b_adjudication_present,
                partition_mandatory=partition_mandatory,
            )
        else:
            results[tail_ops.OP_REVIEW_TRAIL] = await tail_ops.write_review_trail(
                common_dir, review_trail,
                b_adjudication_present=b_adjudication_present,
                partition_mandatory=partition_mandatory,
            )

    # Join (C6): every other pre-commit step above has now run; await the
    return results, extra_stage_paths


@register_op(OP_NAME)
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC 'ceremony.wsc_tail' handler -- single-pass ceremony orchestrator.

    See module docstring for the full sequence. Additive/opt-in only (AC15)
    -- no existing surface (including `/workstream-complete`) invokes this op
    automatically.

    Parameters (params dict):
        sid                   (str, required)  -- the WSC session id.
        subject               (str, required)  -- commit subject line.
        prose                 (str, optional)  -- commit message prose body.
        deleted_paths         (list[str], optional) -- "Deleted (Step 2.67):" block.
        kept_entries           (list[str], optional) -- pre-formatted "<path> — <reason>"
                                Kept-block entries (see `commit_message.format_kept_entry`).
        trailers              (str, optional)  -- pre-derived trailer text; when
                                absent, best-effort-derived via `commit.anchors`.
        nature                (str, optional)  -- Nature override forwarded to
                                `commit.anchors`.
        stage_paths           (list[str], optional) -- explicit paths to stage.
        caller_paths          (list[str], optional) -- caller-authored subset of
                                stage_paths (drives the missing-caller-path anomaly
                                signal; see `commit_pipeline.explicit_stage`).
        swept_renames         (list[str], optional) -- "<src>|<dst>" pairs already
                                `git mv`'d by the caller before this call.
        completion_title      -- REMOVED (C4 Phase 2, 2026-07-23 wsc-tail-slim-
                                down); accepted-and-ignored if present. The
                                completion-entry scaffold this op used to run
                                is now owned by DoE's `workstream-complete`
                                skill (Step 2.6 Action (a), `ba085887`), which
                                invokes `coordinator-complete-entry.py` directly.
        review_trail          (dict | list[dict], optional) -- forwarded to
                                `review_trail.write` (sha_range/reviewer/scope/
                                verdict/diff_loc[/scope_kind/workstream]). A
                                `dict` is the pre-existing single-record shape
                                (`tail_ops.write_review_trail`, unchanged); a
                                `list[dict]` carries N partitioned-review
                                records through this same call
                                (`tail_ops.write_review_trail_many` -- see that
                                function's own docstring for the per-slice
                                isolation and `b_adjudication_present`
                                semantics), via `wsc-tail.py`'s repeatable
                                `--review-slice` flag.
        b_adjudication_present (bool, optional) -- see `tail_ops.write_review_trail`.
        partition_mandatory    (bool, optional) -- sibling of `b_adjudication_present`
                                above, see `tail_ops.write_review_trail`'s own
                                `partition_mandatory` paragraph.
        coverage_range         (str, optional)  -- forwarded to `coverage.gate`.
        coverage_from_handoff  (str, optional)  -- forwarded to `coverage.gate`.
        coverage_scope_paths   (list[str], optional) -- forwarded to `coverage.gate`.
        governing_plan_slug   (str, optional)  -- when supplied AND the commit
                                did not genuinely fail/breach, releases the
                                "plan" claim for this slug (F1, see module docstring).
        scope_mode            (str, optional)  -- receipt scope_mode field.

    Returns:
        {
          "exit_code":               0 | 1 | 2,
          "disposition":             "single-session" | "chain-terminal",
          "committed_sha":           str | None,
          "pushed":                  bool | None,   # None in deferred mode
                                          # (default) AND on any resumed
                                          # pass -- meaningful ONLY under
                                          # push_mode="sync" (see push_status).
          "push_status":             "deferred" | "pushed" | "failed" |
                                          "declined" | "no-remote" |
                                          "unknown_resumed",  # see module
                                          # docstring "Push-mode / result-
                                          # contract decision" for the full
                                          # 4-value matrix.
          "integrity_breach":        bool,           # always False in
                                          # deferred mode / on resume.
          "commit_failed":           bool,
          "empty_consumed_set":      bool,           # R2 loud-report
          "stamped":                 list[str],
          "follow_up_committed_sha": str | None,     # the LAST follow-up
                                          # commit (branch tip after the stamp
                                          # leg).
          "follow_up_committed_shas": list[str],     # every follow-up commit
                                          # -- one per deliverable_id in the
                                          # stamped set; length 1 on the
                                          # ordinary single-deliverable close.
          "follow_up_pushed":        bool | None,    # None when the follow-up
                                          # commit's push was deferred/skipped.
          "tail_results":            dict,           # {label: {acted,skipped,failed}}
          "receipt_path":            str,
          "diagnostics":             list[str],
          "resumed_from_sentinel":   bool,
        }

    On error (exit_code=1): {"exit_code": 1, "error": str}
    """
    sid = str(params.get("sid") or "")
    if not sid:
        return {"exit_code": 1, "error": f"{OP_NAME}: 'sid' param is required"}

    subject = str(params.get("subject") or "")
    if not subject:
        return {"exit_code": 1, "error": f"{OP_NAME}: 'subject' param is required"}

    if repo_root is None:
        return {
            "exit_code": 1,
            "error": (
                f"{OP_NAME}: repo_root arg is None -- common_dir not supplied "
                "by engine (check _OP_KEY_SCOPE = 'common_dir')"
            ),
        }

    common_dir = Path(repo_root)
    worktree_root = main_worktree_root(common_dir)

    timing = _TailTiming()

    with timing.measure("params_and_push_mode"):
        prose = str(params.get("prose") or "")
        deleted_paths = [str(p) for p in (params.get("deleted_paths") or [])]
        kept_entries = [str(p) for p in (params.get("kept_entries") or [])]
        stage_paths = [str(p) for p in (params.get("stage_paths") or [])]
        caller_paths = set(str(p) for p in (params.get("caller_paths") or []))
        swept_renames_raw = [str(p) for p in (params.get("swept_renames") or [])]
        nature = str(params.get("nature") or "")
        review_trail = params.get("review_trail")
        b_adjudication_present = bool(params.get("b_adjudication_present", False))
        partition_mandatory = bool(params.get("partition_mandatory", False))
        coverage_range = str(params.get("coverage_range") or "")
        coverage_from_handoff = str(params.get("coverage_from_handoff") or "")
        coverage_scope_paths = params.get("coverage_scope_paths")
        governing_plan_slug = str(params.get("governing_plan_slug") or "")
        scope_mode = str(params.get("scope_mode") or "")

        diagnostics: list[str] = []
        tail_results: dict[str, dict] = {}
        push_mode = _resolve_push_mode()

    # C7 (2026-07-23 wsc-tail-slim-down) -- consumer enumeration, done BEFORE
    # deciding the minimal residue, per the plan's finding 17 (the Staff Engineer): slimming
    # this resolve without enumerating who reads `initial_consumed`/
    # `chain_terminal` risks silently flipping `disposition` to
    # "single-session" for a chain-terminal close (`chain_terminal` is a
    # wire-contract field of this op's return value). As of THIS landing all
    # four named consumers below are still live on disk, so NOTHING further is
    # removed from this resolve -- do not read this comment as dead code the
    # resolve can be slimmed around; it enumerates why the residue below is
    # already minimal, not a to-do list.
    #
    # (C4 Phase 2, 2026-07-23 wsc-tail-slim-down: the completion-entry
    # `chain_terminal` fill that used to be a fifth consumer here is GONE --
    # the completion-entry scaffold itself was removed from `_run_precommit_
    # tail` once DoE's `ba085887` re-expanded the workstream-complete skill's
    # Step 2.6 to call `coordinator-complete-entry.py` directly. `chain_terminal`
    # is no longer threaded into `_run_precommit_tail` at all.)
    #   1. `disposition` computation (`chain_terminal = bool(initial_consumed)`
    #      just below) -- permanent, wire-contract field, never leaves.
    #   2. Roadmap-callout paths (`_run_precommit_tail`'s
    #      `tail_ops.refresh_roadmap_callout(worktree_root, consumed_handoff_
    #      paths)` call) -- SURVIVES: C5's call-site move has NOT landed:
    #      the render still fires in-process from this op.
    #   3. Stub-close join (`post_commit_tail.run(..., initial_consumed=
    #      initial_consumed, ...)`, both the fresh-commit and AC18-resumed
    #      call sites below) -- SURVIVES: C3a extracted the LOGIC into
    #      `post_commit_tail.py` but this op still invokes it in-process,
    #      passing the same `initial_consumed` set this resolve derives; C3b
    #      (the call-site removal) has NOT landed (see this plan's Landing
    #      Order table -- claude-klabauter's re-drive occasion must land first).
    #   4. Receipt ctx (`ctx.consumed_handoffs` / `ctx.consumed_handoff` /
    #      `ctx.predecessors` / `ctx.predecessor`, set from `initial_consumed`
    #      near "Step 7: receipt emit" below, gated on `chain_terminal`) --
    #      PERMANENT: this is the actual receipt/metadata contract (PM axiom
    #      1) the resolve exists to feed; it never leaves.
    # Net result: the step-1 resolve's residue is unchanged by this chunk --
    # every current consumer is still live, so there is nothing genuinely
    # dead to slim yet. What DOES change here (C7's actual contribution) is
    # documented, not code deletion: Detector B (below) stays on this
    # blocking path deliberately -- `chain_terminal`/`disposition` are
    # consumed synchronously by ALL FOUR items above and are returned in this
    # op's own wire response, so deferring Detector B off this path would
    # leave `disposition` computed before the fallback runs, i.e. wrong for
    # exactly the ship-then-archive shape Detector B exists to catch.
    #
    # --- Step 1: initial resolve (in-process, lightweight -- see module docstring) ---
    with timing.measure("step1_resolve"):
        # AC5 discriminator (docs/plans/2026-08-10-commit-event-5s-cap-and-
        # the-silent-tail.md § C3): `scan_errors` is wired through so an
        # enumeration gap under state/handoffs/ or archive/handoffs/ is
        # captured, never silently indistinguishable from "sid genuinely
        # consumed nothing" (see this function's own docstring, "Scale
        # note" / `scan_errors` paragraph). `a_scan_errors` non-empty means
        # THIS resolve is degraded, not clean-empty.
        a_scan_errors: list[str] = []
        initial_consumed = find_all_consumed_handoffs(
            worktree_root, sid, scan_errors=a_scan_errors
        )
        b_warnings: list[str] = []
        if not initial_consumed:
            # Detector B (git-provenance, cross-repo/inbox/2026-07-22-claude-central-em-
            # wsc-tail-cutover-contract.md Ask 2): the lightweight scan above found
            # nothing for sid -- consult git's own provenance for a ship-then-archive
            # shape the live-stamp-dependent scan can't see, before concluding
            # single-session. Runs ONLY on this empty-result branch, keeping the
            # lightweight step-1 path lightweight (see resolver.detect_git_provenance_
            # consumed's docstring for the full algorithm and its two hit-guards).
            # to_thread'd (AC6, event-loop hygiene) -- this shells out to `git`
            # synchronously and would otherwise stall the event loop for its
            # duration, blocking the ipc dispatch `wait_for` timer from firing.
            b_hits, b_warnings = await asyncio.to_thread(
                detect_git_provenance_consumed, worktree_root, sid
            )
            if b_hits:
                initial_consumed = b_hits
            diagnostics.extend(b_warnings)
        diagnostics.extend(a_scan_errors)
        chain_terminal = bool(initial_consumed)
        # AC5 discriminator, continued: a resolve that came back empty AND
        # DEGRADED (Detector A hit an enumeration gap, or Detector B could
        # not fully compute its hit-list -- both docstrings are explicit
        # that this is never the same fact as "genuinely nothing consumed")
        # must not be treated as a legitimate single-session close.
        # `resolve_degraded` distinguishes that from the ordinary, silent,
        # correctly-nothing-to-flip case below.
        resolve_degraded = bool(a_scan_errors or b_warnings)
        disposition = WRITE_TOKEN if chain_terminal else SINGLE_SESSION

    with timing.measure("sentinel_read"):
        sentinel_path = _sentinel_path(common_dir, sid)
        recovered_sha = _read_sentinel_sha(sentinel_path)
        resumed = recovered_sha is not None

    committed_sha: Optional[str] = recovered_sha
    pushed: Optional[bool] = None
    integrity_breach = False
    commit_failed = False
    # W3 (docs/plans/2026-08-08-a-landed-commit-reported-as-failed.md): the
    # third real commit-pipeline state -- "landed, sha unresolvable" -- is
    # neither `commit_failed` nor "nothing happened". A resumed pass never
    # sets this (it skips `run_commit_pipeline` entirely and recovers a real
    # sha from the sentinel -- see `resumed` above), so it stays `False` on
    # that path by construction, exactly like `commit_failed` does.
    sha_unverified = False

    if not resumed:
        # --- Adjudication/review-trail supply gate (fail-loud, PRE-mutation) ---
        # `tail_ops.write_review_trail` turns this exact input into a
        # `failed_critical[]` entry, but that entry only becomes an exit code
        # at the bottom of this handler -- by which point the ceremony commit
        # has already landed. The observed shape (2026-08-14): a close-out
        # asserting adjudication, supplying no review metadata, exiting
        # non-zero AFTER its own commit, leaving a green-looking ceremony
        # with no trail record and nothing left to re-run. The breach is
        # decidable from params alone, so it is decided here, before the
        # roadmap render, the coverage gate, and the commit.
        if (b_adjudication_present or partition_mandatory) and not tail_ops.review_trail_metadata_complete(
            review_trail
        ):
            fields_str = ", ".join(tail_ops.REVIEW_TRAIL_REQUIRED_FIELDS)
            # D (cross-repo/inbox/2026-08-15-example-retrieval-repo-em-wsc-review-trail-
            # skips-silently.md): a resolved review-scale row 4/6
            # (`partition_mandatory`) is folded into the SAME pre-commit
            # refusal `b_adjudication_present` already trips -- a mandatory-
            # partition close with NO review metadata at all must not exit 0
            # any more than an adjudicated one may. The remediation names a
            # runnable producer, not a slash command: `workstream-complete
            # brief` already emits the per-commit slice list this needs at
            # `gates.review_scale.commit_slices`.
            if partition_mandatory and not b_adjudication_present:
                breach = (
                    f"{OP_NAME}: partition_mandatory with no complete review_trail record "
                    f"-- required fields: {fields_str}. gates.review_scale.chain_slices is the "
                    "chain-scoped review obligation (uncapped); gates.review_scale.commit_slices "
                    "is the narrower record-write population -- different sets by design. "
                    "The engine's own workstream-complete brief already computed a "
                    "per-commit slice list for this close at gates.review_scale.commit_slices "
                    "-- fill in reviewer/scope/verdict per entry and supply the list as "
                    "decisions['review'], or wsc-tail.py's --review-slice (repeatable) / "
                    "discrete --review-* flags."
                )
            else:
                breach = (
                    f"{OP_NAME}: b_adjudication_present with no complete review_trail record "
                    f"-- required fields: {fields_str}. Supply them (decisions['review'], or "
                    "wsc-tail.py's --review-* / --review-slice flags), or drop the "
                    "adjudication assertion."
                )
            _LOG.warning("wsc_tail: refusing commit -- %s", breach)
            return {
                "exit_code": 1,
                "error": breach,
                "diagnostics": [breach],
                "disposition": disposition,
                "resumed_from_sentinel": False,
            }

        # --- Step 2: pre-commit tail ops (best-effort) --- `precommit_tail_total`
        # is the whole-call span; `_run_precommit_tail` records its own four
        # named sub-step spans into the SAME `timing` recorder (see that
        # function's docstring) -- both levels are visible in the map.
        with timing.measure("precommit_tail_total"):
            precommit_results, extra_stage_paths = await _run_precommit_tail(
                common_dir,
                worktree_root,
                sid,
                review_trail=review_trail,
                b_adjudication_present=b_adjudication_present,
                partition_mandatory=partition_mandatory,
                coverage_range=coverage_range,
                coverage_from_handoff=coverage_from_handoff,
                coverage_scope_paths=coverage_scope_paths,
                consumed_handoff_paths=[p for p, _fm in initial_consumed],
                timing=timing,
            )
        tail_results.update(precommit_results)

        full_stage_paths = [*stage_paths, *extra_stage_paths]

        # --- Completion-entry scaffold-residue gate (fail-loud, commit
        # boundary) --- bug 2026-07-28-workstream-complete-d-complete-entry-
        # emi-f6be5553dee4: refuse the commit outright if any staged
        # `archive/completed/` path still carries unfilled
        # `coordinator-complete-entry` scaffold residue. See
        # `_scan_completion_entry_scaffold_residue`'s own docstring for why
        # this sits here (commit boundary) and not at the scaffold.
        scaffold_residue = _scan_completion_entry_scaffold_residue(
            full_stage_paths, worktree_root
        )
        if scaffold_residue:
            _LOG.warning(
                "wsc_tail: refusing commit -- %d completion-entry scaffold "
                "residue diagnostic(s): %s",
                len(scaffold_residue), scaffold_residue,
            )
            return {
                "exit_code": 1,
                "error": (
                    f"{OP_NAME}: completion-entry scaffold residue blocks this "
                    "commit -- see diagnostics"
                ),
                "diagnostics": scaffold_residue,
                "disposition": disposition,
                "resumed_from_sentinel": False,
            }

        # --- Step 3: trailer derivation --- to_thread'd (AC6): pre-stages
        # stage_paths and may shell out to `git`/call `commit.anchors`
        # synchronously. Kept INDIVIDUALLY visible in the timing map (not
        # folded into `commit_pipeline` below) -- the plan calls this step
        # out by name as KEEP-BUT-UNMEASURED: it reads the STAGED diff via
        # `commit.anchors` and its cost is exactly what this instrumentation
        # exists to surface.
        with timing.measure("trailer_derivation"):
            trailers = await asyncio.to_thread(
                _derive_trailers,
                common_dir, sid, full_stage_paths, nature or None, str(params.get("trailers") or ""),
            )

        swept_srcs, swept_dsts = _swept_srcs_dsts(swept_renames_raw)

        # --- Step 4: pre-commit sentinel (AC18, pass-intent placeholder) ---
        with timing.measure("sentinel_write"):
            _write_sentinel(sentinel_path, "")

        # --- Step 5a/5b: commit unit (DEC-3: no outer ceremony_lock wrap
        # here; excise-the-ceremony-lock 2026-08-07 removed run_commit_
        # pipeline's own inner acquisition too -- no ceremony_lock is taken
        # anywhere on this call's path). Run as ONE synchronous function
        # inside ONE asyncio.to_thread call (AC6) so the ipc dispatch
        # `wait_for` timer CAN fire during this section. INVARIANT (verbatim,
        # must hold): a fired timeout must abandon-and-let-complete, never
        # tear down ahead of the work -- cancelling this await abandons the
        # worker thread; the thread completes its commit on its own thread,
        # entirely outside asyncio's cancellation reach.
        #
        # `commit_pipeline` times `run_commit_pipeline()` AS A WHOLE, including
        # its internal `dirty_tree_gate` whole-worktree scan -- that scan is
        # NOT separately timed here. `dirty_tree_gate`'s own gate_paths are
        # derived INSIDE `run_commit_pipeline` (see commit_pipeline.py) and
        # are not reproducible from this call site without either duplicating
        # that derivation (drift-prone, and a second live scan would not
        # measure the SAME invocation the pipeline actually gated on) or
        # editing commit_pipeline.py, which this chunk's remit hard-forbids.
        # KEEP-BUT-UNMEASURED at the sub-span level, by design -- see the
        # C1 chunk's DONE report.
        with timing.measure("commit_pipeline"):
            pipeline_result = await asyncio.to_thread(
                run_commit_pipeline,
                worktree_root,
                session_id=sid,
                subject=subject,
                prose=prose,
                deleted_paths=deleted_paths,
                kept_entries=kept_entries,
                trailers=trailers,
                stage_paths=[*full_stage_paths, *swept_srcs],
                caller_paths=caller_paths,
                push_mode=push_mode,
                # Step 5b (AC18, review Finding 2 fix): write the REAL
                # committed_sha the instant the commit lands INSIDE
                # run_commit_pipeline() -- before push_mode="sync"'s own
                # push-with-retry network round-trip runs -- so a crash
                # during push is also covered by "resume from stamp step"
                # instead of re-running the whole pipeline (which would
                # double-commit). Never re-derived from a later `git
                # rev-parse HEAD`. See commit_pipeline.run_commit_pipeline's
                # `on_committed` docstring for the full rationale.
                on_committed=lambda sha: _write_sentinel(sentinel_path, sha),
            )

        diagnostics.extend(pipeline_result.diagnostics)
        commit_failed = pipeline_result.commit_failed
        committed_sha = pipeline_result.committed_sha
        sha_unverified = pipeline_result.sha_unverified
        pushed = pipeline_result.pushed
        integrity_breach = pipeline_result.integrity_breach

        stamp_outcome = consumed_handoff_stamp.StampOutcome()
        origin_stub_result: dict = {"acted": [], "skipped": [], "failed": []}
        deliverable_cascade_result: dict = {"acted": [], "skipped": [], "failed": []}
        if committed_sha is not None:
            # Steps 5c+5d (C5 stamp+ship, origin-stub close) -- UNLOCKED
            # (DEC-3). Sentinel already carries the real committed_sha as of
            # the on_committed callback above -- no re-write needed here.
            # Composed via `post_commit_tail.run()` (C3a extraction, see the
            # pointer comment above this function and that module's
            # docstring) -- same in-process invocation shape as before this
            # extraction, just one call instead of two inline blocks.
            tail_outcome = await post_commit_tail.run(
                worktree_root,
                common_dir,
                sid,
                committed_sha,
                chain_terminal=chain_terminal,
                governing_plan_slug=governing_plan_slug,
                initial_consumed=initial_consumed,
                close_origin_stub_handler=_close_origin_stub_handler,
                push_mode=push_mode,
                timing=timing,
            )
            stamp_outcome = tail_outcome.stamp_outcome
            origin_stub_result = tail_outcome.origin_stub_result
            deliverable_cascade_result = tail_outcome.deliverable_cascade_result
    else:
        # --- Resumed invocation (AC18): skip straight to the post-commit stamp
        # + origin-stub close -- UNLOCKED (DEC-3), keyed on the RECOVERED sha
        # (never a fresh HEAD read) -- both are post-commit work, so both
        # must still run on a resumed pass (see module docstring step 5d).
        # Same `post_commit_tail.run()` composition as the fresh pass above.
        tail_outcome = await post_commit_tail.run(
            worktree_root,
            common_dir,
            sid,
            committed_sha,  # type: ignore[arg-type]  -- resumed => not None (see resumed check above)
            chain_terminal=chain_terminal,
            governing_plan_slug=governing_plan_slug,
            initial_consumed=initial_consumed,
            close_origin_stub_handler=_close_origin_stub_handler,
            push_mode=push_mode,
            timing=timing,
        )
        stamp_outcome = tail_outcome.stamp_outcome
        origin_stub_result = tail_outcome.origin_stub_result
        deliverable_cascade_result = tail_outcome.deliverable_cascade_result

    # C2 (2026-07-23 wsc-tail-slim-down, POST-COMMIT correction 2026-07-23): archive
    # sweeps fire DETACHED here -- AFTER the commit has landed (fresh OR resumed pass
    # alike), never before. The original C2 landing fired this from _run_precommit_tail
    # (pre-commit), which put four detached `git mv`+`git commit` children racing
    # run_commit_pipeline's OWN commit on the same shared worktree -- .git/index.lock
    # contention plus exactly the unattributable-dirty-file shape dirty_tree_gate's
    # whole-worktree scan hard-fails on. Gated on `committed_sha is not None` -- if the
    # commit pipeline failed or there was nothing to commit, there is nothing newly-
    # terminal to archive, and firing into a failed/skipped ceremony risks a sweep
    # archiving state the ceremony just decided NOT to commit. Also the semantically
    # correct point regardless of the race: WSC Step 2.4 stamps the governing plan
    # `implemented` pre-commit, so a sweep firing post-commit observes that stamp
    # already landed rather than racing it.
    if committed_sha is not None:
        with timing.measure("postcommit.archive_sweeps_detached"):
            tail_results[tail_ops.OP_ARCHIVE_SWEEPS_DETACHED] = tail_ops.fire_archive_sweeps_detached(
                worktree_root
            )
    else:
        # Labelled-skip fix (cross-repo/inbox/2026-08-04-example-retrieval-repo-em-wsc-
        # tail-abort-loses-baton-terminal-flip.md): a commit_pipeline abort
        # used to leave this key unassigned entirely -- the node vanished
        # from the receipt instead of reading as a skip (9 nodes vs a
        # healthy 10). Assign unconditionally, discriminating the reason the
        # same way `tail_results["commit_pipeline"]`'s own skip label does
        # just below (`commit_failed` vs nothing-to-commit).
        tail_results[tail_ops.OP_ARCHIVE_SWEEPS_DETACHED] = {
            "acted": [],
            "skipped": [
                f"{tail_ops.OP_ARCHIVE_SWEEPS_DETACHED}:"
                f"{_commit_gap_reason(commit_failed, sha_unverified)}"
            ],
            "failed": [],
        }

    # --- Step 5e (DEC-1): deferred mode ONLY -- ONE detached background
    # push for the whole tail, spawned after 5a-5d complete (fresh OR
    # resumed pass alike). Skip-loud, never halts an already-landed commit.
    if push_mode == PUSH_MODE_DEFERRED and committed_sha is not None:
        with timing.measure("deferred_push_spawn"):
            _spawn_deferred_push_skip_loud(worktree_root)

    tail_results["commit_pipeline"] = {
        # W3: a `sha_unverified` landing is neither an `acted` sha (there is
        # none to name) nor a `nothing-to-commit` skip (history DID change) --
        # see `_commit_gap_reason`'s own docstring for why the prior
        # `committed_sha or commit_failed` binary rendered it as the latter.
        "acted": [committed_sha] if committed_sha else (
            ["commit_pipeline:landed-sha-unverified"] if sha_unverified else []
        ),
        "skipped": (
            []
            if committed_sha or commit_failed or sha_unverified
            else ["commit_pipeline:nothing-to-commit"]
        ),
        "failed": diagnostics if commit_failed else [],
    }
    tail_results["consumed_handoff_stamp"] = {
        "acted": stamp_outcome.stamped,
        "skipped": [
            # Labelled-skip fix (see the abort-path comment ahead of the
            # archive-sweeps assignment above, same memo): the stamp step
            # never ran on a commit abort, so `stamp_outcome` is still the
            # empty default and every skip-reason list below is empty too --
            # without this entry, an aborted pass emitted a bare `[]` here,
            # indistinguishable from "nothing to do".
            *([f"consumed_handoff_stamp:{_commit_gap_reason(commit_failed, sha_unverified)}"]
              if committed_sha is None else []),
            # Negative-spec: neither zero-candidate path may emit a bare `[]`.
            # A receipt that cannot distinguish "nothing to flip" from "could
            # not see what to flip" is what let a missed terminal flip sit
            # three days undetected upstream -- the sweep reading it was handed
            # a healthy-looking receipt. Outer-empty: resolve found no consumed
            # handoff for this sid. Inner-empty: a candidate existed, the
            # post-commit re-derive found none. Distinct labels, because they
            # are distinct conclusions.
            # -> cross-repo/inbox/2026-08-14-example-retrieval-repo-em-wsc-tail-consumed
            #    -handoff-resolver-blind-to-unclaimed-baton.md
            *(["consumed_handoff_stamp:no-consumed-handoff-resolved"]
              if committed_sha is not None and not chain_terminal else []),
            *(["consumed_handoff_stamp:empty-consumed-set-on-recheck"]
              if committed_sha is not None and chain_terminal and stamp_outcome.empty_consumed_set
              else []),
            *[f"future-dated:{p}" for p in stamp_outcome.skipped_future_dated],
            *[f"live-children:{p}" for p in stamp_outcome.skipped_live_children],
            *[f"indeterminate:{p}" for p in stamp_outcome.skipped_indeterminate],
            *[f"already-terminal:{p}" for p in stamp_outcome.skipped_already_terminal],
        ],
        "failed": [f"{e.get('path', '')}: {e.get('error', '')}" for e in stamp_outcome.errors],
    }
    if stamp_outcome.follow_up_error:
        tail_results["consumed_handoff_stamp"]["failed"].append(
            f"follow-up: {stamp_outcome.follow_up_error}"
        )
    # Silent-skip observability fix (cross-repo/inbox/2026-07-23-claude-
    # central-em-wsc-tail-stamp-ship-silent-skip.md, extended by cross-repo/
    # inbox/2026-08-04-example-retrieval-repo-em-wsc-tail-abort-loses-baton-terminal-
    # flip.md): a chain-terminal pass whose stamp step skipped EVERY
    # candidate (future-dated / live-children / indeterminate) and stamped
    # NOTHING left the closed baton silently `deployment_state: in_flight`
    # while this op still exited 0 -- exit 0 with a landed commit is exactly
    # the signal callers are told to trust (see DoE's `/workstream-complete`
    # skill body), so a stamp-nothing outcome MUST surface through the SAME
    # `tail_results["..."]["failed"]` channel `soft_failed` already reads
    # below (never a new channel -- exit_code=2 here means "tail item
    # surfaced for the operator", NOT "the commit failed"; the commit
    # already landed and `commit_failed`/`exit_code=1` are untouched by this
    # branch). Named per skip reason so the operator can tell "genuinely
    # future-dated" apart from "still has live children" apart from
    # "indeterminate" at a glance, with the exact remediation command for
    # the common (future-dated false-positive / confirmed-safe-to-ship)
    # case. The 2026-08-04 memo's own repro (`committed_sha is None`, so the
    # stamp step never RAN at all -- not "ran and skipped every candidate")
    # is the fourth arm below: mutually exclusive with the three loops
    # above it (gated on `committed_sha is not None` vs `is None`, so a
    # single candidate can never produce both shapes in the same pass).
    if chain_terminal and not stamp_outcome.stamped:
        if committed_sha is not None:
            for p in stamp_outcome.skipped_future_dated:
                tail_results["consumed_handoff_stamp"]["failed"].append(
                    f"future-dated:{p}: stamp skipped -- verify the handoff is "
                    f"actually closable, then remediate via `archive-stamp-cli "
                    f"ship-handoff {p} {committed_sha}`"
                )
            for p in stamp_outcome.skipped_live_children:
                tail_results["consumed_handoff_stamp"]["failed"].append(
                    f"live-children:{p}: stamp skipped -- a live successor still "
                    f"names this handoff as predecessor; once it closes, "
                    f"remediate via `archive-stamp-cli ship-handoff {p} "
                    f"{committed_sha}`"
                )
            for p in stamp_outcome.skipped_indeterminate:
                tail_results["consumed_handoff_stamp"]["failed"].append(
                    f"indeterminate:{p}: stamp skipped -- live-children check "
                    f"was indeterminate (fail-closed); verify manually, then "
                    f"remediate via `archive-stamp-cli ship-handoff {p} "
                    f"{committed_sha}`"
                )
        else:
            # Fourth arm (2026-08-04 memo): the stamp step never evaluated a
            # single candidate -- there is no `committed_sha` to interpolate
            # into a remediation command on this path, so name the path and
            # tell the operator to supply the sha of the commit that
            # actually landed the work, rather than emit a broken
            # `archive-stamp-cli ship-handoff <path> None`.
            reason = _commit_gap_reason(commit_failed, sha_unverified)
            for p, fm in initial_consumed:
                # No key at all is semantically `in_flight`, same reading
                # `handoff_stamp.py`'s terminal-state doc comment uses
                # ("carry `in_flight` or no deployment_state key at all --
                # both non-terminal").
                state = fm.get("deployment_state") or "in_flight"
                if state in HANDOFF_TERMINAL_DEPLOYMENT:
                    tail_results["consumed_handoff_stamp"]["failed"].append(
                        f"never-evaluated:{p}: stamp step did not run ({reason}) -- "
                        f"step-1 resolve observed `deployment_state: {state}` "
                        "(already terminal); no remediation needed."
                    )
                else:
                    tail_results["consumed_handoff_stamp"]["failed"].append(
                        f"never-evaluated:{p}: stamp step did not run ({reason}) -- "
                        f"step-1 resolve observed `deployment_state: {state}`. "
                        f"Remediate via `archive-stamp-cli ship-handoff {p} <sha-of-the-commit-"
                        "that-actually-landed-the-work>` once you have confirmed "
                        "what that sha is."
                    )
    elif not chain_terminal:
        # Silent-exit-0 fix (cross-repo/inbox/2026-08-10-doe-claude-em-wsc-
        # tail-silent-noop-and-gate-rewalk.md finding 1): the block above
        # only names a stamp outcome when `chain_terminal` is True --
        # `post_commit_stamp_and_ship()` short-circuits to an untouched
        # `StampOutcome()` (`empty_consumed_set=False`) whenever
        # `chain_terminal` is False (see that function's own docstring), so
        # this branch previously left NO trace anywhere in `diagnostics` or
        # `tail_results` that no terminal flip was attempted this pass --
        # exit_code=0 with an empty diagnostics list, indistinguishable from
        # "there was nothing to flip AND we checked" vs "we never looked".
        # Always name the disposition explicitly here (never gated on
        # `exit_code`, unlike the `if exit_code == 2` timing dump below) so
        # a caller reading `diagnostics` -- on ANY exit code -- can tell
        # "chain_terminal=False, no flip due" from silence. Does NOT change
        # `exit_code`: a step-1 resolve that genuinely finds no consumed
        # handoffs for this sid is a legitimate single-session close, not a
        # failure -- see `disposition`/`SINGLE_SESSION` above.
        #
        # AC5 (docs/plans/2026-08-10-commit-event-5s-cap-and-the-silent-
        # tail.md § C3): the OUTER empty case ("step 1 resolved no consumed
        # handoffs at all", this branch) is a DIFFERENT fact from the INNER
        # empty case (`chain_terminal` was True but the post-commit re-
        # derive came back empty, `stamp_outcome.empty_consumed_set` above --
        # already reaches exit_code=2 unconditionally). This branch must
        # only escalate when `resolve_degraded` is True (Detector A hit an
        # enumeration gap, or Detector B could not fully compute its hit-
        # list) -- that is the only in-op-observable signal that separates
        # "should have flipped and didn't" from "correctly nothing to flip"
        # (an ordinary single-session close, the overwhelmingly common
        # case, must stay silent-exit-0 -- see plan Anti-scope). Appending
        # to `tail_results["consumed_handoff_stamp"]["failed"]` (rather than
        # only `diagnostics`) is what actually reaches `soft_failed` in the
        # exit-code ladder below -- a bare `diagnostics.append` alone never
        # changes `exit_code`, which was this branch's whole prior gap.
        if resolve_degraded:
            tail_results["consumed_handoff_stamp"]["failed"].append(
                f"outer-empty-degraded:sid={sid}: step-1 resolve for a "
                "chain-terminal flip did not complete cleanly (Detector A "
                f"scan_errors={a_scan_errors!r} / Detector B "
                f"warnings={b_warnings!r}), so an empty consumed-handoff set "
                "here is NOT trustworthy as 'genuinely nothing to flip' -- "
                "the commit landed but a predecessor handoff may still need "
                "its deployment_state flipped by hand. Investigate the named "
                "scan/warning above, then remediate via `archive-stamp-cli "
                f"ship-handoff <predecessor-handoff-path> {committed_sha}` "
                "once you have confirmed the real predecessor."
            )
        # Claim-store contradiction (cross-repo/inbox/2026-08-11-doe-claude-
        # em-pickup-claim-never-reaches-frontmatter.md): a fully-clean resolve
        # (neither Detector A's `consumed_by:` frontmatter scan nor Detector
        # B's git-provenance scan hit anything, and `resolve_degraded` above
        # is False -- both scans genuinely ran and found nothing) can still be
        # WRONG when the closing session's OWN claim-record store disagrees.
        # `session.claims.list_claims_by_session_checked` reads the
        # branch-independent claim LEDGER directly (`session_id` file inside
        # `<class>-claims/<basename>/`), never the frontmatter mirror both
        # detectors above depend on -- so a claim that landed in the ledger
        # but whose frontmatter mutation never fired (the memo's repro: a
        # `pickup-assemble apply` `already_satisfied` skip that never wrote
        # `claimed_by`/`in_flight` to the handoff) still shows up here even
        # though Detector A/B see nothing. A held `handoff-claims` entry with
        # a genuinely empty resolve is exactly that contradiction -- named
        # loud, never silently folded into the ordinary single-session-close
        # case. `errors` (unresolvable sessions_dir) is treated as "no
        # evidence either way", not itself a contradiction -- this check adds
        # a NEW fact only when it has one to add.
        try:
            # Local import (mirrors `session/claims.py`'s own local import of
            # `_CLAIM_SUBDIRS` inside this same function, and the docstring's
            # "Import discipline" note): `coordinator_core.ops` eager-imports
            # every op module at package-init time, including `ops/session/
            # reap.py`, which imports back from `session/claims.py` -- a
            # module-level import here would risk the same cycle this
            # module's own ops.* imports above already coexist with only
            # because none of them touch `session.claims` at import time.
            from coordinator_core.session.claims import list_claims_by_session_checked

            claim_matches, claim_errors = list_claims_by_session_checked(
                sid, cwd=str(worktree_root)
            )
        except Exception as exc:  # pragma: no cover -- defensive, see above
            claim_matches, claim_errors = [], [
                f"list_claims_by_session_checked raised {type(exc).__name__}: {exc}"
            ]
        held_handoff_claims = sorted(
            basename for class_, basename in claim_matches if class_ == "handoff-claims"
        )
        if held_handoff_claims and not claim_errors:
            tail_results["consumed_handoff_stamp"]["failed"].append(
                f"outer-empty-claim-contradiction:sid={sid}: this session "
                "still holds a live handoff claim in the claim-record ledger "
                f"({', '.join(held_handoff_claims)}), yet a clean step-1 "
                "resolve (Detector A consumed_by-frontmatter scan and "
                "Detector B git-provenance scan both ran without error and "
                "found nothing for this session) -- the predecessor "
                "handoff's frontmatter mutation likely never landed (it may "
                "still read `deployment_state: ready_to_fire` / "
                "`pickup_ready: true`). Investigate via `pickup-assemble "
                f"brief <handoff-path>` and, once confirmed, remediate via "
                f"`archive-stamp-cli ship-handoff <predecessor-handoff-path> "
                f"{committed_sha}`."
            )
        diagnostics.append(
            f"consumed_handoff_stamp: chain_terminal=False for sid={sid} -- "
            "step-1 resolve (find_all_consumed_handoffs + Detector B) found "
            "no consumed handoff(s) this pass, so no terminal flip was due "
            "and none was attempted (disposition="
            f"{disposition!r}, resolve_degraded={resolve_degraded!r}). If a "
            "chain-terminal flip was expected here, investigate why the "
            "resolve came back empty -- this pass will not retry it."
        )
    if committed_sha is None:
        # Labelled-skip fix (2026-08-04 memo): `origin_stub_result` never
        # left its empty default on this path -- same "vanished/empty node"
        # complaint as the archive-sweeps and consumed_handoff_stamp fixes
        # above, applied to this node's own `skipped` channel.
        origin_stub_result["skipped"] = [
            f"{OP_CLOSE_ORIGIN_STUB}:"
            f"{_commit_gap_reason(commit_failed, sha_unverified)}"
        ]
        # Review: coordinator:code-reviewer — C6b's cascade result was computed
        # by post_commit_tail.run() but never read here, so AC6h refusals never
        # reached this ceremony's own response. Same labelled-skip treatment as
        # origin_stub_result immediately above.
        deliverable_cascade_result["skipped"] = [
            f"{post_commit_tail.OP_DELIVERABLE_CASCADE}:"
            f"{_commit_gap_reason(commit_failed, sha_unverified)}"
        ]
    tail_results[OP_CLOSE_ORIGIN_STUB] = origin_stub_result
    tail_results[post_commit_tail.OP_DELIVERABLE_CASCADE] = deliverable_cascade_result

    # --- Step 6: F1 exit-verdict ordering for cs_release_artifact
    # (see module docstring "F1 exit-verdict ordering" for the full rationale) ---
    # Review: coordinator:code-reviewer -- checked this gate against the
    # `sha_unverified` state (W3, 2026-08-08-a-landed-commit-reported-as-
    # failed.md) and it needs no change. It keys on `commit_failed`/
    # `integrity_breach` alone, never on `committed_sha`, so a
    # `sha_unverified=True` landing already takes the release branch below --
    # correct, since the work landed and the claim should release. This is a
    # claim-release decision, not a did-anything-happen one, so it is a
    # different discriminator than `_commit_gap_reason` even though it shares
    # the `commit_failed` variable; do not fold it into that sweep.
    with timing.measure("cs_release_artifact"):
        if governing_plan_slug and not commit_failed and not integrity_breach:
            cs_release_result = tail_ops.cs_release_artifact(
                common_dir, _GOVERNING_PLAN_ARTIFACT_CLASS, governing_plan_slug
            )
        else:
            reason = (
                "no-governing-plan-slug" if not governing_plan_slug
                else "commit-failed-or-breached"
            )
            cs_release_result = {
                "acted": [], "skipped": [f"{tail_ops.OP_CS_RELEASE_ARTIFACT}:{reason}"], "failed": [],
            }
    tail_results[tail_ops.OP_CS_RELEASE_ARTIFACT] = cs_release_result

    # C8 (2026-07-23 wsc-tail-slim-down, AC7): name any failure among the two
    # tail_results labels that still resolve fully in-op (see
    # `_DIAGNOSTIC_NAMED_LABELS`'s own comment) in `diagnostics`, before that
    # list is read anywhere (the exit_code==2 self-evidencing block below,
    # and the final wire response) -- fixes the printed-`[]` wart.
    _append_named_tail_failures(diagnostics, tail_results, timing)

    # --- Receipt contract for the slimmed op (C8, 2026-07-23 wsc-tail-slim-
    # down) -- what lands as a D-node in THIS op's OWN receipt today, and
    # where every other step's outcome goes, spelled out because a future
    # shed chunk needs the full map before removing another one:
    #
    #   - TODAY (this landing): every entry currently in `tail_results` at
    #     this point -- commit_pipeline, consumed_handoff_stamp,
    #     close_origin_stub, deliverable_cascade, archive_sweeps:detached_fire,
    #     cs_release_artifact, plus every precommit_tail label (roadmap_callout,
    #     coverage_gate annotation, review_trail) -- gets EXACTLY ONE D-node
    #     below (the `ctx.add_node` loop), success or failure alike. Nothing
    #     else has actually left this receipt yet: C3a extracted stamp+ship/
    #     stub-close's LOGIC into `post_commit_tail.py`, but this function
    #     still calls `post_commit_tail.run()` IN-PROCESS (no IPC hop, no
    #     receipt of its own) and folds its outcome -- including C6b's
    #     `deliverable_cascade_result` (AC6h refusals named here, review-
    #     flagged as previously computed-then-discarded) -- into `tail_results`
    #     exactly as before the extraction. C2/C5 have not moved archive
    #     sweeps / roadmap-callout out of this op either -- see C7's own
    #     consumer enumeration above the step-1 resolve for the current
    #     residue. (C4 Phase 2 DID land since C7/C8 were written: the
    #     completion-entry scaffold's call site left this function entirely --
    #     see the module docstring's C4 Phase 2 paragraph -- so it is no
    #     longer among these labels at all, not moved to a cross-referenced
    #     receipt or the C17 log below.)
    #
    #   - `coverage.gate` (K-001, state/kill-ledger.md) is the precedent for
    #     what changes when a step's evidence, not merely its call site,
    #     leaves: it no longer appears in `tail_results` at all -- no D-node,
    #     no receipt field -- because the close path stopped invoking the op
    #     that produced it, not because the invocation moved off-thread (C6's
    #     asyncio shed, which kept the verdict as a D-node, is retired by this
    #     landing).
    #
    #   - FORWARD CONTRACT -- not yet true for the remaining tracked chunks,
    #     tracked by C3b / C5 (see this plan's Landing Order / skew-tolerance
    #     section -- a claude-klabauter-side shed chunk must not assume a sibling chunk
    #     it depends on has already landed): once a step's CALL SITE actually
    #     leaves this function body (not merely its logic extracted into a
    #     helper module still invoked in-process), its outcome stops being a
    #     D-node HERE and is recorded instead as --
    #       * stamp+ship + stub-close (C3a/C3b): `post_commit_tail.py`'s own
    #         receipt (once it is invoked via its registered op path rather
    #         than this in-process `run()` call), cross-referenced from this
    #         receipt by op-call id.
    #       * archive sweeps / tracker regen (C2/C5): the shared
    #         housekeeping-failures log (C17), written ONLY on failure -- a
    #         clean success writes nothing there (life-support principle:
    #         visible only when something breaks).
    #     (C4 Phase 2 already landed -- the completion-entry scaffold left by
    #     a different route than this forward contract predicted: DoE's
    #     `workstream-complete` skill now runs `coordinator-complete-entry.py`
    #     directly instead of this op scaffolding it, so there is no D-node
    #     AND no C17 log entry for it here -- it simply never runs from this
    #     function any more.)
    #     Until each of the remaining landings actually removes its call site
    #     from this function, its D-node stays exactly where it is below --
    #     do NOT pre-emptively delete a D-node for a call that is still
    #     running in-process, and do not read this comment as license to do so.
    #
    # --- Step 7: receipt emit (reuse receipt_emit.emit_receipt) ---
    ctx = PipelineContext(ceremony="wsc", scope_mode=scope_mode, disposition=disposition, sid=sid)
    if chain_terminal:
        ctx.consumed_handoffs = [p for p, _fm in initial_consumed]
        ctx.consumed_handoff = ctx.consumed_handoffs[0] if ctx.consumed_handoffs else ""
        ctx.predecessors = [fm.get("predecessor", "") for _p, fm in initial_consumed]
        ctx.predecessor = ctx.predecessors[0] if ctx.predecessors else ""

    for node_id, (resolving_op, result) in enumerate(tail_results.items()):
        ctx.add_node(_d_node(f"tail-{node_id}", resolving_op, result))

    # Timing D-node (non-tail-step -- see `_TIMING_NODE_ID` docstring): folded
    # into the ledger BEFORE the first emit so a failed run (exit_code=1,
    # returned above, or exit_code=2 below) is still self-evidencing from
    # the persisted receipt alone. The map is
    # deliberately NOT complete at this point: `receipt_emit` and
    # `sentinel_clear` below are measured after this D-node is built and are
    # structurally unrepresentable here without a second receipt write --
    # not worth the extra I/O on a sub-2s path. (Review: staff-eng — prior
    # comment claimed completeness the map does not have.)
    ctx.add_node(make_d_node(_TIMING_NODE_ID, resolving_op=_TIMING_RESOLVING_OP, evidence={"steps": timing.as_list()}))

    with timing.measure("receipt_emit"):
        receipt_path, _op_tail = emit_receipt(
            ctx,
            repo_root=worktree_root,
            sid=sid,
            tail_phase="archival",
            receipt_phase="phase-2",
        )

    # --- Failure-class discriminator ---
    failed_critical = any(r.get("failed_critical") for r in tail_results.values())
    soft_failed = any(r.get("failed") for r in tail_results.values())

    if commit_failed:
        exit_code = 1
    elif failed_critical or soft_failed or integrity_breach or stamp_outcome.empty_consumed_set:
        exit_code = 2
    else:
        exit_code = 0

    # --- Step 4/5 (AC18): clear the sentinel only on successful receipt emit ---
    with timing.measure("sentinel_clear"):
        _clear_sentinel(sentinel_path)

    # --- push_status (AC8/DEC-1/F4): single enum, see module docstring
    # "Push-mode / result-contract decision" for the full 4-value matrix.
    # ANY resumed pass wins regardless of push_mode -- the original
    # pre-crash push outcome was never persisted anywhere this pass can
    # recover, so `pushed`/`integrity_breach` above stay at their
    # initialized None/False (semantically distinct from `pushed: None`
    # meaning "no remote configured").
    if resumed:
        push_status = "unknown_resumed"
    elif push_mode == PUSH_MODE_DEFERRED:
        # ALWAYS "deferred" at result-return time, whether or not a commit
        # landed -- the detached child (if any was spawned) hasn't run yet
        # when this returns, so "failed" can never legitimately appear here.
        push_status = "deferred"
    else:
        # push_mode="sync" (COORDINATOR_WSC_SYNC_PUSH=1): read the canonical
        # `push_status` directly off `pipeline_result` (C6b) -- NEVER
        # re-derived from the `pushed` tri-state, which double-books `None`
        # across three distinct meanings (no remote, nothing to breach, a
        # policy decline) and, before this fix, silently reported a policy
        # decline as `push_status="pushed"`. See
        # `_CANONICAL_PUSH_STATUS_TO_LOCAL` for the mapping.
        push_status = _CANONICAL_PUSH_STATUS_TO_LOCAL[pipeline_result.push_status]

    # Self-evidencing on a soft-failed pass (exit_code=2): fold the per-step
    # timing map into `diagnostics` too, not just the receipt, so a caller
    # that only reads the wire response (never opens the receipt file) still
    # sees where the time went on the run that just flagged a problem.
    if exit_code == 2:
        diagnostics.extend(
            f"timing:{entry['step']}={entry['ms']:.1f}ms" for entry in timing.as_list()
        )

    return {
        "exit_code": exit_code,
        "disposition": disposition,
        "committed_sha": committed_sha,
        "pushed": pushed,
        "push_status": push_status,
        "integrity_breach": integrity_breach,
        "commit_failed": commit_failed,
        "empty_consumed_set": stamp_outcome.empty_consumed_set,
        "stamped": stamp_outcome.stamped,
        "follow_up_committed_sha": stamp_outcome.follow_up_committed_sha,
        "follow_up_committed_shas": stamp_outcome.follow_up_committed_shas,
        "follow_up_pushed": stamp_outcome.follow_up_pushed,
        "tail_results": tail_results,
        "receipt_path": rel_id(receipt_path, worktree_root),
        "diagnostics": diagnostics,
        "resumed_from_sentinel": resumed,
        "timing": timing.as_list(),
    }
