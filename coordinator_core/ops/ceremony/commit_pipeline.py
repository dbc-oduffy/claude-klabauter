"""
coordinator_core.ops.ceremony.commit_pipeline -- native stage -> commit -> push
critical section, integrating C1 (git_native), C2 (commit_message), and C3
(commit_gates). This is the C4 chunk of the `wsc_tail` rebuild
(docs/plans/2026-07-16-wsc-pure-python-tail-rebuild.md).

`run_commit_pipeline()` is the single entry point: it classifies the
caller-supplied paths against staged git state (tolerant explicit-stage -- a
path already swept by a concurrent archival op is skipped, not treated as a
`git add` failure; `ceremony_lock` used to be acquired for the worktree for
the duration of this whole pass -- deleted 2026-08-07,
docs/plans/2026-08-07-excise-the-ceremony-lock.md -- see that plan's safety
argument for why the two residual windows it left, C10's divergence dedup
and C11's sha capture, are safe without it),
runs C3's deletion-block + dirty-tree gates, composes the commit message via
C2, writes it to a PID-scoped temp file, commits via `git_native.
commit_scoped()` (C3/C4 -- the computed commit-mechanism selector, an
EXPLICIT pathspec ALWAYS, AC5 -- never a bare `git commit`), captures the
post-commit HEAD SHA, and runs a push-with-retry loop (reject-detect ->
fetch -> rebase --onto -> re-push, bounded, never `--force`). Every `git`
subprocess this module issues directly, or transitively via
`commit_scoped()`, routes through `git_native._git` (AC2/AC3) -- no bare
`subprocess.run` written in this file. That does NOT make `_git()` this
module's sole spawn path end to end: `_drain_pending_push_after_sync`
(below) calls `auto_push.drain_pending_push`, which issues its own bare
`subprocess.run` calls in `coordinator_core/hooks/auto_push.py`, unrouted
through `git_native._git`.

Spec backlink: pln-rebuild-the-wsc-commit-ceremon-f7c2a0 § C4 (AC5).
Spec backlink: docs/plans/2026-07-27-computed-commit-mechanism-selection.md
  § C4 -- `commit()` routed through `commit_scoped()`; `explicit_stage()`
  made divergence-aware.
Provenance: ported from `_run_explicit_stage` / `_run_git_push` /
  `_rebase_onto_fetched_ref` / `_capture_head_sha` /
  `_derive_pushed_tristate` in the OLD `wsc_commit.py` (still present on disk
  at port time, not yet deleted by the kill list) -- simplified to the scope
  C4's chunk body actually calls for; the second-chance committed-rename
  lookup (`_scan_committed_renames`, an unrelated 2026-07-11 incident fix) is
  intentionally NOT ported here -- out of C4's stated scope.

Negative-spec (hard-won):
  - The final commit is ALWAYS scoped to an EXPLICIT `commit_paths` pathspec
    -- never a bare `git commit` / `git commit -m`. In the common (agree)
    case this is literally `git commit -F <msgfile> -- <commit_paths>`; when
    `commit_scoped()` observes a diverged path in the batch it instead
    builds the commit under a private index and lands it via `update-ref`
    (see `git_native.commit_scoped`'s own docstring) -- the pathspec-
    scoping guarantee holds either way. A concurrent sibling's own staged
    file or deletion outside `commit_paths` is never absorbed (parity
    assertions d + e).
  - `explicit_stage()` never re-stages (bare `git add`) a path whose staged
    content DIVERGES from its worktree content -- doing so would destroy
    deliberately-staged partial-hunk content before `commit_scoped()` (C3)
    ever gets a chance to observe and preserve it (claude-klabauter 506748a0
    incident shape, one layer up).
  - The temp message file path is built via `tempfile`, never a hardcoded
    path -- see the Windows port-hazard checklist at
    `state/improvement-queue/2026-07-15-naked-python-on-windows-port-checklist-t-7f55b7e682d3.yaml`.
  - `committed_sha` is never a blind post-commit `git rev-parse HEAD` (C11,
    docs/plans/2026-08-07-excise-the-ceremony-lock.md) -- that could pick up
    a concurrent sibling's own commit landing in the same window on a shared
    branch, now that there is no `ceremony_lock` bounding the window. When
    `commit_scoped()` takes the private-index branch, `committed_sha` is its
    own CAS-verified `stdout` (the exact sha `update-ref` landed). When it
    takes the agree branch, `committed_sha` is resolved by matching a
    per-commit `Commit-Token: <uuid4().hex>` trailer -- minted inside
    `commit()` and appended to the message before it is written to the temp
    file -- against a pathspec-scoped `git log --grep=<token> --fixed-strings
    --format=%H <pre-commit-HEAD>..HEAD -- <commit_paths>` (docs/plans/
    2026-08-08-a-landed-commit-reported-as-failed.md, W1: the prior subject-
    substring match could spuriously match a peer commit whose message merely
    *contained* this call's subject -- a revert, a rollup, a memo quoting it
    -- since no peer can ever author this exact token string, the match is
    collision-free by construction), failing loud (non-zero `exit_code`,
    `committed_sha=None`) rather than guessing when no unambiguous match is
    found -- see `commit()`'s own docstring. A verification failure on this
    path (HEAD unresolvable, empty subject, or zero/ambiguous token match)
    still sets `CommitOutcome.landed=True`: `git commit` already created the
    commit at that point, so the caller must not treat it as a no-op --
    see `CommitOutcome.landed`'s own docstring and `commit()`'s.
  - Push-with-retry never passes `--force` at any point.
  - Does NOT shell out to bash/node/`.sh`/`.js` -- git only, and only via
    `git_native._git`.
  - `push_mode` (wsc-tail-sub-2s-invoke-budget DEC-1/F1) does NOT change
    stage/gate/commit semantics at all -- it ONLY gates whether
    `push_with_retry()` runs inside this critical section.
    RETIRED (DR-329 § 4, 2026-08-25, `docs/decisions/DR-329-push-runs-on-a-
    cadence-not-on-every-commit.md`): this instruction used to bind
    `scoped_git_commit.py` specifically ("never passes `push_mode`, so it
    always gets `sync` by construction; do not add a call site that
    defaults it to anything else") -- that caller was deleted outright by
    PM ruling on 2026-08-23 (`docs/plans/2026-08-23-the-scoped-commit-
    rebuilt-from-first-principles.md`, ledger K-045), and no successor
    inherited its "never passes `push_mode`" contract. Every LIVE caller of
    `run_commit_pipeline` is now governed by DR-329's disposition instead:
    the six named cadence surfaces (`docs/decisions/DR-329-*.md` § 2) pass
    `push_mode=PUSH_MODE_NEVER` at their own commit leg and instead call
    `push_outstanding()` synchronously at their own checkpoint;
    `close_out_and_stamp.py` (not itself a cadence surface) also passes
    `push_mode=PUSH_MODE_NEVER` explicitly, deferring publication to
    whichever cadence checkpoint runs next rather than owning a synchronous
    push of its own. `push_mode`'s own default parameter value
    (`PUSH_MODE_SYNC`) is UNCHANGED by this DR -- it is the explicit
    call-site contract that changed, not the function signature -- so a
    hypothetical new caller that omits `push_mode` still gets `"sync"` by
    construction; DR-329 does not forbid that, it only requires every
    NAMED existing caller above to pass `PUSH_MODE_NEVER` explicitly rather
    than relying on the default.
  - A directory pathspec in `stage_paths` is refused BEFORE `explicit_stage()`
    ever runs (session fb5fa766, 2026-07-31 incident, closed 2026-07-31) --
    `commit_scoped()`'s own directory refusal, further down the pipeline,
    fires only after `git add -- <dir>/` has already staged it, leaving
    residue `reset_paths()` deliberately does not clean up (a directory
    pathspec matches whatever is CURRENTLY inside it at reset time, not just
    what this call staged). See `run_commit_pipeline()`'s pre-stage guard,
    which reuses `git_native.directory_pathspecs()` / `directory_pathspec_
    diagnostic()` -- the same predicate/wording `commit_scoped()` uses, not a
    forked second notion of "is a directory pathspec".
"""

from __future__ import annotations
import sys

# Generator-provenance declaration (generator_provenance.py's AST reader):
# this module's only file write is a PID-scoped commit-message temp file
# under the git common dir (_write_commit_message_tempfile, via tempfile) --
# never a tracked repo-relative artifact. Commit/push effects land through
# git subprocess calls (git_native), not a Python-level file write this
# sweep can see.
GENERATES = []

import logging
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Sequence, Set, Tuple, Union

from coordinator_core.git.divergence import DivergenceCheckFailed, diverging_paths

#: Timeout (seconds) `explicit_stage()` gives its own `diverging_paths()`
#: call. Same reasoning and value as `git_native._DIVERGENCE_CHECK_TIMEOUT_
#: SECS` -- this function's own `git add` decision (below) is exactly the
#: same class of decision `commit_scoped()` makes on the SAME predicate, so
#: it gets the same `fail_loud=True` treatment and the same widened timeout,
#: not Check 13's `2.0s` advisory default.
_DIVERGENCE_CHECK_TIMEOUT_SECS = 5.0
from coordinator_core.lifecycle import git_common_dir
from coordinator_core.ops.ceremony import git_native
from coordinator_core.wire_paths import rel_id as _archive_sweep_rel_id

#: Shared argv-safe path packer + its budget constant, both promoted
#: (2026-08-15) to `git_native.py` -- the shared home, since `git_native.py`
#: cannot import back from this module (this module already imports
#: `git_native`, above; a reverse import would be circular). Bound to these
#: module-level names so every existing bare `_chunk_paths(...)` /
#: `_DIVERGENCE_CHECK_ARGV_BUDGET_CHARS` reference below is unchanged --
#: see `git_native._chunk_paths`'s own docstring for the packer itself.
_chunk_paths = git_native._chunk_paths
_DIVERGENCE_CHECK_ARGV_BUDGET_CHARS = git_native._DIVERGENCE_CHECK_ARGV_BUDGET_CHARS

from coordinator_core.git.git_state import index_read_cache_scope
from coordinator_core.ops.ceremony.commit_gates import (
    DirtyTreeOutcome,
    GateOutcome,
    carry_gate,
    deletion_block_gate,
    dirty_tree_gate,
    op_scope_coverage_gate,
)
from coordinator_core.ops.ceremony.commit_message import (
    _ends_with_trailer_block,
    compose_message,
    compute_commit_paths,
    compute_gate_paths,
)
from coordinator_core.session.core import session_dir as _session_core_session_dir
from coordinator_core.session.core import sessions_dir as session_hub_dir
from coordinator_core.session.liveness import live_session_ids
from coordinator_core.telemetry.op_latency import record_composition_span

#: `run_commit_pipeline()` composition-span names (C2, docs/plans/2026-08-19-
#: the-engine-stops-paying-a-network-push-on-every-commit.md § C2). Two rows
#: per sync-mode invocation that reaches `commit()`, sharing one
#: `composition_id` -- "pre_push" covers stage -> gates -> commit (everything
#: BEFORE `push_with_retry()` is called), "push" covers exactly the
#: `push_with_retry()` call itself. This is the direct measurement AC2 asks
#: for: the prior "8.9s p50 is the push" claim was attributed by shape only
#: (`run_commit_pipeline` step 2 short-circuits on empty `commit_paths`
#: *before either gate runs*, so the 4ms floor sample bounds nothing about
#: the pre-push leg specifically -- staff-eng F8), and `ipc.py`'s
#: `_OP_TIMEOUT_OVERRIDES` comment block records 53 `git` spawns and
#: 25.7/36.6/40.9s wall for a 2,100-path commit with no network claim
#: attached -- direct in-tree evidence the non-push leg can itself be
#: seconds at fleet path counts. That spawn count is now named as the DEFECT
#: the old 150s override cap was hiding (`ipc.CEREMONY_BUDGET_SECS`, DR-348,
#: revoked 2026-08-21), not a cost this span exists to accommodate: these
#: spans measure the leg the ceremony budget now holds to 2s end-to-end, so a
#: 53-spawn commit is exactly what this instrumentation must catch, not
#: explain away. Reusing `record_composition_span` (the
#: existing span writer, no new sink, no new field) rather than inventing a
#: parallel mechanism -- see that function's own docstring for the record
#: shape and its fail-open contract (never raises, honours
#: `COORDINATOR_OP_LATENCY_DISABLE`).
_COMPOSITION_SPAN_PRE_PUSH = "commit_pipeline.pre_push"
_COMPOSITION_SPAN_PUSH = "commit_pipeline.push"

#: `push_mode` values for `run_commit_pipeline()` (wsc-tail-sub-2s-invoke-
#: budget DEC-1/F1). "sync" (default) is `scoped_git_commit.py`'s wire
#: contract, untouched -- that caller never passes `push_mode`, so it always
#: gets "sync" by construction. "deferred"/"none" both skip the in-pipeline
#: `push_with_retry()` call (the caller becomes responsible for the push, or
#: for never issuing one); "deferred" additionally signals the caller
#: (`wsc_tail.py`) to spawn ONE detached background push after its own
#: locked critical section completes.
#:
#: "never" is the fourth, and is about a DIFFERENT question than the other
#: three. Sync/deferred/none all answer "which publisher pushes this commit"
#: -- every one of them ends with the commit published by somebody. "never"
#: answers "may this commit be published at all", and the answer is no: the
#: in-pipeline push leg is skipped (as in "none") AND the `post-commit`
#: hook's own detached push is stood down, so NO publisher pushes it. Use it
#: where committing and publishing are separate deliberate acts and only the
#: first one is authorized here -- `publish.py::_commit_published_dests` is
#: the reason it exists (a percolation ends at a local commit; the push is
#: the operator's own next step, `percolate-push <target>`).
#:
#: Why a mode rather than leaving that caller on "none": under "none" the
#: post-commit hook is deliberately left armed, because there the hook's
#: push is the only one there is and standing it down would strand the
#: commit. A caller that must not publish therefore cannot express itself
#: with "none" -- it would be relying on whatever the hook's own branch
#: policy happens to say, which is not a decision it controls. See
#: `test_sole_publisher_suppression.py` for the pinned wiring.
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


# ---------------------------------------------------------------------------
# Tolerant explicit-stage
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StageOutcome:
    """Typed result of `explicit_stage()`.

    Fields:
        exit_code -- 0 (clean), 2 (a caller-supplied path is genuinely
            missing -- degraded, not a hard failure), or the raw `git add`
            returncode on a genuine stage failure.
        acted -- paths `git add`-ed THIS call. A DIVERGED existing path
            (see `explicit_stage` docstring) is never in `acted` -- it is
            already staged with deliberately-partial content, so this call
            does not touch it -- but it IS in `staged_paths` (it is staged,
            just not by this call). `acted` no longer always mirrors
            `staged_paths` once a diverged path is present in the batch.
            On a FAILED `git add` (non-atomic partial-batch failure -- see
            `explicit_stage`'s own docstring), `acted` is NOT unconditionally
            `[]`: it is reconciled against real index state scoped to this
            call's own `to_stage` batch, so any genuinely-partially-staged
            residue is still reported here and still covered by a caller's
            rollback (e.g. `run_commit_pipeline`'s `finally`).
        skipped -- human-readable per-path classification tags (see
            `explicit_stage` docstring).
        failed -- non-empty only on a genuine `git add` subprocess failure.
        staged_paths -- the reconciled set of paths actually staged; callers
            forward THIS (not the raw `paths` argument) into `commit_paths`
            derivation so a swept path is never re-forwarded as a bare
            pathspec.
        swept_renames -- (old, new) pairs for paths skipped because they are
            a staged rename source; forwarded into `commit_paths` via C2's
            `compute_commit_paths` so the rename's content is not dropped.
        missing_caller_paths -- caller-supplied paths that were genuinely
            absent (not staged, not swept) -- drives `exit_code == 2`.
        ignored_caller_paths -- caller-supplied paths that exist on disk,
            are untracked, and are excluded by `.gitignore` -- also drives
            `exit_code == 2`, but kept in a SEPARATE field from
            `missing_caller_paths` (2026-08-03 fix): "this path is absent"
            and "this path exists but is `.gitignore`-blocked" are different
            operator-facing facts (a session legitimately writes an ignored
            cache file, e.g. `state/orientation_cache.md` -- that is not the
            same anomaly as a path that was never written at all) and must
            not collapse into one bucket just because both are benign,
            non-fatal, degraded-batch signals. See `explicit_stage`'s own
            docstring for the incident (live 2026-08-03 `safe-commit-offer`
            run) this closes: previously an ignored path was invisible to
            this pre-filter and reached `git add`, where it failed the WHOLE
            batch (non-atomically -- see the "Non-atomicity" section of
            `explicit_stage`'s docstring) rather than being skipped like a
            missing path already was.
        checked_paths -- the exact path set `diverging_paths()` was scoped
            to this call (the existing-on-disk subset of `paths`). Exposed
            so a downstream `commit_scoped()` call in the SAME locked
            critical section can trust `diverged_paths` for every path in
            `checked_paths` instead of re-deriving it (docs/plans/
            2026-07-27-computed-commit-mechanism-selection.md § dedup --
            see `commit()`'s threading of these two fields below). A path
            outside `checked_paths` (e.g. a swept-rename destination, never
            passed into this call's `paths`) was never vetted here and
            must still be checked fresh by the caller.
        diverged_paths -- the subset of `checked_paths` found to diverge
            (STAGED != WORKTREE). Same dedup rationale as `checked_paths`.
        deletion_paths -- the subset of `staged_paths` that are DELETIONS
            (2026-08-04 fix, defect A/B -- see `explicit_stage`'s own
            docstring "Deletion staging" section): a path missing from the
            worktree that IS attributable to a real deletion (staged via a
            prior `git rm`, or a tracked file simply `rm`'d and never
            re-added) rather than to "never existed"/"never tracked". Never
            derived from `acted` alone -- an already-staged deletion this
            call never re-`git add`s is still a member (mirrors
            `diverged_paths`' own "in `staged_paths`, not necessarily in
            `acted`" shape). `run_commit_pipeline` unions this into the
            caller-supplied `deleted_paths` before composing the commit
            message's "Deleted (Step 2.67):" block -- without that, a
            deletion this function newly makes committable would trip
            `commit_gates.deletion_block_gate`'s Assertion-3 (a staged
            deletion in scope with no Step 2.67 block is a hard gate
            failure), trading a silent drop for an opaque refusal instead
            of an actual commit.
    """

    exit_code: int
    acted: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    checked_paths: Set[str] = field(default_factory=set)
    diverged_paths: Set[str] = field(default_factory=set)
    staged_paths: List[str] = field(default_factory=list)
    swept_renames: List[Tuple[str, str]] = field(default_factory=list)
    deletion_paths: List[str] = field(default_factory=list)
    missing_caller_paths: List[str] = field(default_factory=list)
    ignored_caller_paths: List[str] = field(default_factory=list)


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


def _reason_from_git_result(result: "git_native.GitResult", *, attempted: Sequence[str]) -> str:
    """Compose a real failure diagnosis, never a bare `exit_code=N`.

    2026-08-03 fix (live `safe-commit-offer` incident): a caller-facing
    report saying only `"exit_code=1"` gives an operator nothing to act on
    without re-running the op -- and a re-run against a shared worktree is
    not always safe or possible (the paths may have moved on by then). Both
    `git add` (ignored-path refusal) and `git commit -F ... --` (the
    "nothing to commit" no-op) are confirmed to sometimes leave `stderr`
    genuinely empty while the real diagnosis lands on `stdout` instead --
    prefer stderr, fall back to stdout, and only fall back to the raw
    `attempted` path list (never a bare code alone) when BOTH streams are
    silent, so the report always names what this call was trying to do.
    """
    detail = condense_git_diagnostic(result.stderr) or condense_git_diagnostic(result.stdout)
    if detail:
        return detail
    paths_preview = ", ".join(attempted[:5])
    if len(attempted) > 5:
        paths_preview += f", ... ({len(attempted)} total)"
    return (
        f"exit_code={result.returncode} (no diagnostic output on stdout/stderr "
        f"-- attempted paths: {paths_preview})"
    )[:_MAX_DIAGNOSTIC_CHARS]


def _parse_rename_line(line: str) -> Optional[Tuple[str, str]]:
    """Parse one `R<score>\\t<old>\\t<new>` name-status line, or None if not a rename."""
    parts = line.split("\t")
    if len(parts) != 3 or not parts[0].startswith("R"):
        return None
    return parts[1], parts[2]


def _worktree_key(root: Path, p: str) -> str:
    """Return `p` in the CWD-relative, forward-slashed form git reports it in.

    Every git probe in `explicit_stage` runs with `cwd=worktree_root`, and git
    prints its matches relative to that cwd no matter which form the pathspec
    was written in -- `git ls-files --deleted -- /abs/path/f` scopes correctly
    and prints `f`. A caller-supplied ABSOLUTE path therefore never matches
    `worktree_deleted` / `swept_delete` / `swept_rename` by raw string equality,
    and a real deletion falls through to "genuinely absent"
    (`bug-2026-08-14-explicit-stage-absolute-deletion-path`: a partial commit
    that reports success, not an error).

    Only the membership KEY is normalized -- classification results keep the
    caller's own path form, which `git add` accepts either way, so a caller
    reconciling `staged_paths`/`deletion_paths` against its own pathspec still
    sees the strings it passed in.

    A path outside `root` (or one that resolves outside it) is returned
    unchanged: it cannot correspond to a git-reported name anyway, and the
    fallthrough to "missing" is the correct classification for it.
    """
    candidate = Path(p)
    if not candidate.is_absolute():
        return p
    for base in (root, root.resolve()):
        try:
            return candidate.relative_to(base).as_posix()
        except ValueError:
            continue
    try:
        return candidate.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return p


def _path_is_scopeable(root: Path, p: str) -> bool:
    """True iff `p` can be handed to a `git` pathspec under `cwd=root`
    without git itself hard-failing on the PATH (independent of whether it
    exists or is tracked).

    Empirically confirmed (2026-08-26): `git status`/`git ls-files` with a
    pathspec outside the worktree, or one whose containing directory does
    not exist on disk at all, exits `rc=128` with `fatal: Invalid path
    '...': No such file or directory` -- a property of the path string
    itself, not an indeterminate answer about deletion state. Mirrors
    `_worktree_key`'s own relativization attempts (a relative `p`, or an
    absolute `p` that resolves under `root`, is scopeable; anything else is
    not) so the two stay in lockstep -- a path this returns False for is
    exactly the one `_worktree_key` would return unchanged for, which can
    never match a git-reported name anyway.
    """
    candidate = Path(p)
    if not candidate.is_absolute():
        # A RELATIVE path can escape the worktree too (`../outside/x.md`),
        # and git rejects it with the same rc=128 path-shape fatal as the
        # absolute case -- confirmed 2026-08-26: "fatal: ../outside/x.md:
        # '../outside/x.md' is outside repository". Normalise lexically
        # rather than via `resolve()`: a path being probed for DELETION is
        # routinely absent from disk, so containment must not depend on the
        # target existing.
        normalized = os.path.normpath(os.path.join(str(root), p))
        try:
            return os.path.commonpath([normalized, str(root)]) == os.path.normpath(str(root))
        except ValueError:
            # Different drive on Windows -- not under `root` by definition.
            return False
    for base in (root, root.resolve()):
        try:
            candidate.relative_to(base)
            return True
        except ValueError:
            continue
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _diverging_paths_chunked(
    paths: Sequence[str],
    cwd: str,
    *,
    timeout: float,
) -> Set[str]:
    """Chunked `diverging_paths(..., fail_loud=True)` for `explicit_stage()`'s
    divergence check, closing the Windows argv-length defect a single
    unchunked call hits at percolate-publish scale (~2000-2700 paths on one
    `git diff --cached --name-only` argv exceeds the 32767-char Windows
    command-line cap, `subprocess` reports `rc=127`, and the whole batch
    reads as indeterminate even though nothing actually diverged -- see
    `state/lessons/2026-08-14-partial-stage-protection-did-not-survive-a-
    moving-head.md` for why that check exists and must not be weakened to
    fix this). This is deliberately NOT one unfiltered `git diff --cached
    --name-only` (no pathspec) with an in-process intersection against
    `paths` -- that changes the cost model to full-index-size on a large
    repo instead of pathspec-bounded, trading the argv problem for an
    unrelated one; chunking keeps the same per-call cost shape `diverging_
    paths()` already documents, just spread across more calls.

    Paths are packed into chunks via `_chunk_paths()` (bounded by
    `_DIVERGENCE_CHECK_ARGV_BUDGET_CHARS` -- see that constant's own
    docstring), and each chunk gets its own independent `diverging_paths()`
    call -- a path's divergence answer comes from exactly the one chunk it
    was placed in, never from a whole-batch verdict ORed/ANDed across
    chunks, so the answer for any given path is identical to what an
    unchunked call would have produced for it. A `DivergenceCheckFailed`
    from ANY chunk (a genuine `git diff` error, not an argv-length
    artifact -- each chunk is already sized to avoid that) propagates
    immediately and uncaught, exactly like the unchunked call used to:
    `explicit_stage()`'s own `try`/`except DivergenceCheckFailed` still
    refuses the whole call rather than guess at paths whose chunk never
    got an answer.
    """
    diverged: Set[str] = set()
    for chunk in _chunk_paths(paths):
        diverged.update(diverging_paths(chunk, cwd=cwd, timeout=timeout, fail_loud=True))
    return diverged


class WorktreeDeletionProbeFailed(Exception):
    """Raised by `_worktree_deleted_paths_chunked` when it cannot answer,
    rather than degrading to the pre-fix "found nothing" guess.

    2026-08-26 fix (docs/research/spike-verdicts/2026-08-26-one-porcelain-
    v2-read-replaces-the-probe-suite.md): the probe this replaces
    (`git ls-files --deleted`, chunked) silently degraded a failing chunk
    to "no deletions found" and marked every caller path in it
    `unverifiable_missing_caller_paths` -- a permissive guess a caller
    could not tell apart from a genuine "confirmed absent" answer without
    reading a second field. Fail-loud closes that: a chunk-level failure
    (non-zero rc, timeout, a malformed/unrecognised v2 record, or a `u`
    unmerged record) now raises instead of degrading, so `explicit_stage()`
    refuses the call the same way its divergence check already does on
    `DivergenceCheckFailed` -- see that `try`/`except` for the mirrored
    shape.
    """


def _worktree_deleted_paths_chunked(root: Path, paths: Sequence[str]) -> Set[str]:
    """Chunked, path-scoped, fail-loud `git status --porcelain=v2 -z
    --ignored -uall -- <paths>` read for `explicit_stage()`'s unstaged-
    deletion classification probe -- the fail-loud replacement for the old
    `git ls-files --deleted` probe (`WorktreeDeletionProbeFailed`'s own
    docstring covers the incident this closes).

    Returns the CWD-relative name set of every path in `paths` whose
    worktree content is missing while its index still matches HEAD (a
    plain `rm` never followed by `git rm`/`git add` -- the v2 record's `Y`
    (worktree-vs-index) status is `D`). Chunked exactly like the divergence
    check: each chunk is an independent, pathspec-bounded call, and a
    path's membership comes from the ONE chunk it was placed in -- never a
    whole-batch verdict merged across chunks.

    Every record type this read can emit is explicitly dispatched by its
    leading token (spike verdict § U3, confirmed against git
    2.55.0.windows.4): `1` ordinary (space-delimited, path is the 9th
    field), `2` rename/copy (path is the 10th field; its `<origPath>` is
    the NEXT NUL-separated field, consumed and discarded here -- this
    probe only answers deletion membership, and a renamed path is handled
    by the separate, deliberately-unscoped `diff_cached_name_status`
    read), `?` untracked and `!` ignored (path is everything after the
    2-character prefix), `u` unmerged (raises -- a conflicted path has no
    safe worktree-deletion answer), `#` header (skipped). Any OTHER
    leading token raises rather than being silently skipped -- an
    unrecognised record is exactly the "this probe cannot vouch for this
    answer" case fail-loud exists to catch.

    The absent-record door (spike verdict § U1): `git status` emits
    NOTHING for a path that is either CLEAN-TRACKED or MATCHED-NOTHING (an
    unmatched pathspec) -- both exit 0 with no record, and neither is a
    deletion, so no record for a requested path in this probe's OWN chunk
    is not itself an error. Every path with no record in its chunk is
    still explicitly resolved via a batched `git ls-files -z` membership
    check (`git_native.ls_files_scoped`) so this function never defaults
    silence to a classification it never verified -- see that helper's own
    docstring for why the check is needed at all despite neither outcome
    changing this probe's answer (both are simply "not deleted").

    A path OUTSIDE `root` (or whose containing directory does not exist on
    disk at all) is excluded from every git call this function issues --
    empirically confirmed (2026-08-26) that `git status`/`git ls-files`
    both hard-fail (`fatal: Invalid path '...': No such file or directory`,
    rc=128) on such a pathspec, which is a property of the PATH, not an
    indeterminate answer about deletion state, and must not escalate the
    whole chunk to `WorktreeDeletionProbeFailed` -- `_worktree_key` already
    returns such a path unchanged (unresolvable under `root`), so it can
    never legitimately match a git-reported name anyway; the caller's own
    fallthrough to "missing" (see `explicit_stage`'s classification loop)
    is the correct, unweakened answer for it, exactly as it was before this
    fix (`test_explicit_stage_absolute_path_outside_worktree_is_missing`).
    """
    if not paths:
        return set()

    deleted: Set[str] = set()
    for chunk in _chunk_paths(list(paths)):
        scoped_chunk = [p for p in chunk if _path_is_scopeable(root, p)]
        if not scoped_chunk:
            continue
        result = git_native.status_porcelain_v2_scoped(root, scoped_chunk)
        if not result.ok:
            raise WorktreeDeletionProbeFailed(
                "_worktree_deleted_paths_chunked: `git status --porcelain=v2` "
                f"failed or timed out (rc={result.returncode}) for {len(scoped_chunk)} "
                "path(s) -- worktree-deletion classification indeterminate "
                f"({condense_git_diagnostic(result.stderr) or condense_git_diagnostic(result.stdout) or 'no diagnostic output'})"
            )

        fields = result.stdout.split("\0")
        if fields and fields[-1] == "":
            fields = fields[:-1]

        seen: Set[str] = set()
        i = 0
        while i < len(fields):
            record = fields[i]
            if not record:
                i += 1
                continue
            token = record[0]
            if token == "1":
                parts = record.split(" ", 8)
                if len(parts) < 9 or len(parts[1]) < 2:
                    raise WorktreeDeletionProbeFailed(
                        f"_worktree_deleted_paths_chunked: malformed ordinary "
                        f"v2 record: {record!r}"
                    )
                path = parts[8]
                seen.add(path)
                if parts[1][1] == "D":
                    deleted.add(path)
                i += 1
            elif token == "2":
                parts = record.split(" ", 9)
                if len(parts) < 10 or i + 1 >= len(fields):
                    raise WorktreeDeletionProbeFailed(
                        f"_worktree_deleted_paths_chunked: malformed rename "
                        f"v2 record: {record!r}"
                    )
                seen.add(parts[9])
                # `<origPath>` is the NEXT NUL field -- consumed, not used by
                # this deletion-only probe (see this function's own
                # docstring).
                i += 2
            elif token in ("?", "!"):
                seen.add(record[2:])
                i += 1
            elif token == "u":
                raise WorktreeDeletionProbeFailed(
                    f"_worktree_deleted_paths_chunked: unmerged (conflict) "
                    f"v2 record, no safe deletion answer: {record!r}"
                )
            elif token == "#":
                i += 1
            else:
                raise WorktreeDeletionProbeFailed(
                    f"_worktree_deleted_paths_chunked: unrecognised v2 record "
                    f"leading token: {record!r}"
                )

        # NO absent-record settling spawn here, deliberately -- this probe's
        # answer domain is "which of `paths` are worktree-deleted", and for
        # THAT question a silent path is resolved, not defaulted. Once the
        # `git status` call above has SUCCEEDED, silence means git scanned
        # the path and had nothing to report, which is definitionally "not
        # deleted" whether the path is clean-tracked or matched nothing.
        # Neither outcome is a deletion, so no second read can change the
        # answer. The permissive-guess failure this function exists to close
        # was silence after a FAILED probe -- that arm now raises above.
        #
        # The present/absent split callers need is settled independently by
        # `explicit_stage`'s own in-process `(root / p).exists()` check (see
        # its docstring), which costs no process at all. An earlier revision
        # spawned `git ls-files` per chunk here and discarded its stdout:
        # measured 2x the spawns of the probe it replaced (8 vs 4 on a
        # 600-path batch, +122ms) for an identical result set, which at
        # percolate scale alone would breach the DR-344 brightline.
    return deleted


def _residue_paths_chunked(
    root: Path, to_stage: Sequence[str]
) -> Tuple[Set[str], bool, Optional["git_native.GitResult"]]:
    """Chunked `git diff --cached --name-only -z -- <to_stage>` for
    `explicit_stage()`'s post-`git add`-failure residue reconciliation --
    same argv-length hazard `_diverging_paths_chunked` and
    `_worktree_deleted_paths_chunked` close, one call site over (see
    `explicit_stage()`'s own "Residue reconciliation" comment for what this
    check protects and why `-z` is required).

    Returns `(residue, indeterminate, first_failure)`. `residue` is the
    CWD-relative name set of every path found ACTUALLY staged, unioned
    across every chunk that answered -- a path's membership comes from
    exactly the one chunk it was placed in, never a whole-batch verdict.
    `indeterminate` is True iff at least one chunk's own `git diff` call
    failed; `first_failure` is that chunk's `GitResult` (for diagnostic
    text), or `None` when every chunk answered. A failing chunk's own
    paths are simply absent from `residue` (fail-closed for exactly those
    paths -- the caller's `reconciled_acted` filter already treats absence
    as "not confirmed staged"), so a genuine `git diff` failure on one
    chunk never gets read as "nothing in that chunk was staged" -- the
    caller surfaces `indeterminate` precisely so that distinction is not
    lost.
    """
    residue: Set[str] = set()
    indeterminate = False
    first_failure: Optional["git_native.GitResult"] = None
    for chunk in _chunk_paths(to_stage):
        result = git_native.diff_cached_name_only(root, paths=chunk, nul_separated=True)
        if result.ok:
            residue.update(entry for entry in result.stdout.split("\0") if entry)
        else:
            indeterminate = True
            if first_failure is None:
                first_failure = result
    return residue, indeterminate, first_failure


class WorktreeRootMissing(ValueError):
    """`worktree_root` does not resolve to a directory on this host.

    A caller error, deliberately raised rather than returned as a degraded
    outcome. Filed 2026-08-25 by claude-klabauter-em (`state/bug-backlog/
    2026-08-25-a-bad-worktree-root-reports-as-n-missing-768a39de52b3.yaml`):
    handed a root that does not exist, `explicit_stage` classified EVERY
    requested path `missing:<path>` and returned `exit_code=0`, so a
    dispatched `git-commit-agent` read a root problem as a pathspec problem,
    concluded the sanctioned route was unreachable, and fell back to a bare
    `git commit` -- which skips `deletion_block_gate`, `dirty_tree_gate`,
    `carry_gate` and `op_scope_coverage_gate`. Three commits landed that way
    in one session and all three were CORRECT, which is why nothing announced
    itself.

    The observed cause is a path-dialect mismatch, not a typo: `/X/project-
    claude-klabauter` (the MSYS/bash form) is accepted by the Bash tool, by `git`, and
    by PowerShell's `Test-Path`, while `Path('/X/claude-klabauter').exists()`
    is False and `Path('X:/claude-klabauter').exists()` is True. Agents that
    reach this API from a bash context hand out the form their shell gave
    them.

    NEGATIVE SPEC -- do NOT resolve this by normalising the MSYS form into a
    drive-letter path. That adds a path dialect to this function's contract,
    puts the engine in the business of guessing which host spelling was
    meant, and leaves the next unaccepted dialect failing the same silent
    way. Rejecting at entry costs one `is_dir()` and cannot be misread.
    `exit_code == 2` is NOT the right channel either: that means "a
    caller-supplied path is genuinely missing -- degraded, not a hard
    failure", and a missing root is neither about a path nor degraded.
    """


def _require_worktree_root(worktree_root: Union[str, Path]) -> Path:
    """Resolve `worktree_root` to a Path, raising if it is not a directory.

    Purpose: the single entry check both `explicit_stage` and
    `run_commit_pipeline` run before any path under the root is classified,
    so a bad root can never be reported as N missing pathspecs. See
    `WorktreeRootMissing` for why this raises instead of returning.
    """
    root = Path(worktree_root)
    if not root.is_dir():
        raise WorktreeRootMissing(
            "worktree_root does not exist: %r -- this is the ROOT, not a "
            "pathspec, and no path under it was inspected. On Windows hosts "
            "pass the drive-letter form (`X:/claude-klabauter`), never the "
            "MSYS form (`/X/claude-klabauter`) that bash and git accept but "
            "Python's pathlib does not." % (str(worktree_root),)
        )
    return root


def explicit_stage(
    worktree_root: Union[str, Path],
    paths: Sequence[str],
    caller_paths: Optional[Set[str]] = None,
) -> StageOutcome:
    """Tolerant/reconciling explicit-path stage: `git add -- <to_stage>`.

    Detects paths already swept (git mv'd or staged-deleted) by a concurrent
    peer/tail op and skips them rather than letting a batch `git add` fail on
    a missing source path.

    Divergence-aware (C4 fix, docs/plans/2026-07-27-computed-commit-
    mechanism-selection.md § C4 "second job"): among the paths that exist on
    disk, any whose STAGED content differs from its WORKTREE content right
    now (`coordinator_core.git.divergence.diverging_paths` -- deliberate
    partial-hunk staging) is left OUT of the `git add` batch. A bare
    `git add` on a diverged path overwrites its deliberately-staged content
    with worktree content -- the claude-klabauter 506748a0 incident shape --
    destroying the divergence one layer above where `git_native.
    commit_scoped()` (C3) would otherwise have observed and preserved it.
    Such a path is still reported in `staged_paths` (it IS staged, just not
    by this call) and tagged `"diverged:<p>"` in `skipped`.

    Cost: one extra batched `diverging_paths()` call (two `git diff`
    subprocesses) per `explicit_stage()` invocation, scoped via pathspec to
    the existing-path subset of the caller's own batch -- never a full-tree
    status walk, never per-path. In the overwhelming-majority agree case
    (nothing diverged) this adds two small, pathspec-bounded git calls; it
    does not change the `git add` call itself, so the common-case cost stays
    essentially the fixed two-call overhead, not a new per-path or
    full-status cost. (`wsc_tail._derive_trailers` already pays the
    identical two-call cost against the same pathspec shape, per C10.)

    Classification per path `p` in `paths` (see `StageOutcome` for the
    resulting fields):
      `(worktree_root / p).exists()` AND NOT diverged -> staged normally
                                          (`to_stage`, `acted`).
      `(worktree_root / p).exists()` AND diverged      -> skipped
                                          `"diverged:<p>"`; included in
                                          `staged_paths`, NOT in `acted`,
                                          NOT re-added via `git add`.
      `p` is a staged rename source   -> skipped `"swept:<p>-><new>"`;
                                          `(p, new)` appended to
                                          `swept_renames`. A literal `|` in
                                          either side falls back to
                                          `"missing:<p>"` (ambiguous to a
                                          pipe-delimited forwarding value).
      `p` is a staged deletion source (2026-08-04 fix, defect B -- see
      "Deletion staging" below) -> checked BEFORE the plain `exists()` test
      above (2026-08-11 fix, doe-claude-em memo `cross-repo/inbox/2026-08-11-
      doe-claude-em-two-gaps-that-let-machine-local-files-stay-tracked.md`
      § 2 -- see "Untrack vs. add" below for why the ordering itself is the
      fix) ->
        `p` not in `caller_paths`     -> skipped `"swept-deleted:<p>"`
                                          (benign -- a GENERATED artifact the
                                          tail's own archival step git-rm'd;
                                          NOT this call's own deletion, so it
                                          is never absorbed into
                                          `staged_paths`/`deletion_paths`).
        `p` in `caller_paths`         -> skipped `"already-staged-
                                          deleted:<p>"`; included in
                                          `staged_paths` AND
                                          `deletion_paths` (already staged --
                                          no `git add` needed -- this IS the
                                          caller's own deletion and belongs in
                                          the commit set, exactly like a
                                          diverged path belongs without being
                                          re-`git add`-ed).
      `p` is UNSTAGED-deleted (tracked, `rm`'d from the worktree but never
      `git rm`/`git add`-ed -- 2026-08-04 fix, defect A -- see "Deletion
      staging" below) ->
        `p` not in `caller_paths`     -> skipped `"worktree-deleted:<p>"`
                                          (benign -- mirrors the staged-
                                          deletion "not this call's business"
                                          case above; never staged by this
                                          call).
        `p` in `caller_paths`         -> skipped `"deleted:<p>"`; `p`
                                          appended to `to_stage` (a `git add`
                                          on an explicit deleted pathspec
                                          stages the removal -- see
                                          `git_native.add_paths`' own
                                          docstring) and to `deletion_paths`.
      genuinely absent (never tracked, never on disk, not a rename/deletion
      source of any kind) ->
        `p` not in `caller_paths`     -> skipped `"missing:<p>"` (benign).
        `p` in `caller_paths`         -> skipped `"missing-caller:<p>"`; `p`
                                          appended to `missing_caller_paths`.
      `p` exists on disk, is UNTRACKED, and matches `.gitignore` (2026-08-03
      fix -- see the "Ignored-path pre-filter" section below) ->
        `p` not in `caller_paths`     -> skipped `"ignored:<p>"` (benign --
                                          e.g. a peer's own gitignored scratch
                                          file that happens to sit in the
                                          same batch).
        `p` in `caller_paths`         -> skipped `"ignored-caller:<p>"`; `p`
                                          appended to `ignored_caller_paths`
                                          (drives `exit_code == 2`, same as
                                          `missing_caller_paths`, but kept in
                                          its own field -- see
                                          `StageOutcome.ignored_caller_paths`
                                          for why these two facts must not
                                          collapse into one bucket). Never
                                          added to `to_stage`, never
                                          included in `staged_paths` -- an
                                          ignored, untracked path was never
                                          staged before this call and is not
                                          staged now.

    Untrack vs. add (2026-08-11 fix, doe-claude-em memo `cross-repo/inbox/
    2026-08-11-doe-claude-em-two-gaps-that-let-machine-local-files-stay-
    tracked.md` § 2 "`scoped-git-commit` cannot perform an untrack commit, by
    construction"): a `git rm --cached` untrack leaves the file's CONTENT on
    disk (only the index entry is removed), so `(worktree_root / p).exists()`
    is True for it -- same as any ordinary tracked-and-modified path. Before
    this fix the loop tested `exists()` FIRST, so an untracked-but-still-on-
    disk path fell into `existing` and reached the ignored-path pre-filter
    below, where -- now that nothing tracks it -- it matches whatever
    `.gitignore` rule motivated the untrack in the first place, and gets
    refused with `"excluded by .gitignore"`. That reading is correct for an
    ADD (a caller trying to `git add` a path `.gitignore` blocks) and
    INVERTED for an UNTRACK (being gitignored is the *precondition* of a
    legitimate untrack, not a violation of one) -- so the one ceremony this
    repo's doctrine names for scoped commits was structurally unable to land
    an untrack of a gitignored path, pushing callers toward a bare
    `git commit` instead. The fix is ordering, not a new predicate: `p in
    swept_delete` (derived from `git diff --cached --name-status`, an INDEX
    fact independent of worktree existence) is now checked before `exists()`,
    so a staged deletion is classified as `already-staged-deleted`/`swept-
    deleted` regardless of whether its content still sits on disk, and never
    reaches `existing` or the ignore pre-filter at all. A staged ADD/MODIFY
    against a gitignored path is untouched by this reordering -- it was never
    in `swept_delete` (that set holds `D`-status entries only) and still
    resolves via `exists()` into `existing`, still hits the ignore pre-filter,
    still declines with the unchanged `"excluded by .gitignore"` reason.

    Path form (2026-08-14 fix, `bug-2026-08-14-explicit-stage-absolute-
    deletion-path`): `paths` may be absolute or repo-relative -- the two are
    equivalent inputs. Every git probe here runs with `cwd=worktree_root` and
    reports CWD-relative names regardless of the pathspec form it was queried
    with, so each membership test against a git-derived set (`swept_delete`,
    `swept_rename`, `worktree_deleted`, `diverged`, the post-failure residue
    set) goes through `_worktree_key`. Before this, an absolute path matched
    none of them: a real tracked deletion was classified "genuinely absent",
    dropped from the commit set, and the commit landed WITHOUT it while
    reporting success -- a partial commit that reads as clean (observed as 83
    declined deletion-intents per `percolate-round` publish round). The
    `check_ignore` pre-filter is deliberately NOT normalized: `git check-ignore
    -v -z --stdin` echoes back the pathname exactly as supplied, so its output
    is already in the caller's own form.

    Classification RESULTS keep the caller's path form (`git add` accepts
    either), so a caller reconciling `staged_paths`/`deletion_paths`/`acted`
    against its own pathspec gets back the strings it passed in.

    `caller_paths` defaults to the empty set -- treat none as caller-supplied
    (every path classified as a benign GENERATED-path skip on miss).

    Ignored-path pre-filter (2026-08-03 fix, live `safe-commit-offer`
    incident): a caller-supplied touch-list legitimately contains a session's
    own gitignored working file (e.g. `state/orientation_cache.md`,
    `.gitignore:78`) alongside real dirty paths -- a session writing such a
    file is not an anomaly. Before this fix, an untracked ignored path was
    invisible to the `(worktree_root / p).exists()` classification above (it
    exists on disk, it just isn't tracked), so it flowed into `to_stage` and
    reached `git add`, where -- per the "Non-atomicity" note below -- it
    failed the WHOLE batch rather than being skipped the way a genuinely
    missing path already was. This pre-filter runs a single batched
    `git_native.check_ignore()` call (never per-path, mirroring the existing
    divergence-check cost shape) over the `existing` subset BEFORE the
    divergence check and BEFORE `to_stage` is built, so a `.gitignore`-
    blocked path never reaches `git add` at all -- the same "refuse before
    it can leave residue" posture `run_commit_pipeline`'s own pre-stage
    directory-pathspec guard already uses, one layer up. `check_ignore()`'s
    plain (non-`--no-index`) `git check-ignore` is index-aware by default,
    so it answers exactly the "would a fresh `git add` refuse this path"
    question this classification needs on its own: a path that is BOTH
    tracked and `.gitignore`-matching (a pattern added after the path was
    already committed) is never reported as ignored -- `git add` on an
    already-tracked path succeeds regardless of a later-added ignore
    pattern, so pre-filtering it out would silently stop committing real
    changes to a file the caller still owns. No separate tracked/untracked
    gate is needed here; see `git_native.check_ignore()`'s own negative-spec
    for why `--no-index` is deliberately not used.

    Negative-spec: never raises -- every git/subprocess error is captured
    into `StageOutcome.failed` (via `git_native.GitResult.ok`), including an
    indeterminate `diverging_paths(..., fail_loud=True)` divergence check
    (`DivergenceCheckFailed`, caught internally and converted to
    `exit_code=-1` + a `failed` entry -- never propagated past this
    function) and an indeterminate worktree-deletion probe
    (`WorktreeDeletionProbeFailed`, caught the same way -- see
    `_worktree_deleted_paths_chunked`'s own docstring for the 2026-08-26 fix
    that made this probe fail loud instead of degrading).

    Non-atomicity of `git add` on failure (empirically confirmed
    2026-07-31): a mixed `git add -- to_stage` batch is NOT atomic --
    a `.gitignore`-blocked path (invisible to the `(worktree_root / p).
    exists()` pre-filter above, since an ignored file still exists on disk)
    can error AFTER an earlier path in the same batch is already staged.
    On that failure branch, `acted` is NOT unconditionally `[]` -- it is
    reconciled against real index state scoped to `to_stage` (`git diff
    --cached --name-only -- <to_stage>`, never a bare/unscoped read that
    could sweep in a concurrent peer session's own staged work), so any
    genuinely-partially-staged residue is still visible to a caller's
    rollback bookkeeping. See `StageOutcome.acted`'s own docstring.

    Deletion staging (2026-08-04 fix -- live incident: a named-in-pathspec
    deletion was silently dropped from the commit set twice in a row,
    `state/handoffs/2026-08-04-stamp-the-scope-guard-plan-and-close-the.md`
    at `e41c208b1`/`74a30c846`; a THIRD invocation over the same path after
    `git rm`-staging it first reported `empty-commit-set` instead, defect B).
    Root cause: the `(root / p).exists()` classification above is False for
    BOTH "this path was deleted" and "this path never existed" -- the two
    were conflated into one "missing" bucket, and a missing path (whatever
    its cause) was never added to `to_stage`/`staged_paths`, so a deletion
    could never reach `commit_paths` through this function at all, no matter
    how the caller staged it. Fixed by attributing a missing path to a real
    deletion BEFORE falling through to "genuinely absent" -- see the two new
    classification arms above (staged deletion source, unstaged/worktree
    deletion) -- and by recording every deletion this call includes in
    `StageOutcome.deletion_paths` so `run_commit_pipeline` can compose a
    correct "Deleted (Step 2.67):" message block for it (see that field's
    own docstring for why this is required, not cosmetic: without it,
    `commit_gates.deletion_block_gate`'s Assertion-3 hard-fails a staged
    deletion whose message never declared it).
    """
    root = _require_worktree_root(worktree_root)
    caller_paths = caller_paths or set()

    if not paths:
        return StageOutcome(exit_code=0, skipped=["stage:no-paths-provided"])

    swept_rename: Dict[str, str] = {}
    swept_delete: Set[str] = set()
    diff_result = git_native.diff_cached_name_status(root, find_renames=True)
    if diff_result.ok:
        for line in diff_result.stdout.splitlines():
            parsed = _parse_rename_line(line)
            if parsed is not None:
                swept_rename[parsed[0]] = parsed[1]
                continue
            parts = line.split("\t")
            if len(parts) >= 2 and parts[0] == "D":
                swept_delete.add(parts[1])
    # A failing `git diff --cached` (e.g. no commits yet) leaves the swept-set
    # empty -- best-effort, mirrors the OLD seam's try/except-Exception shape.

    # Unstaged-deletion set (defect A): tracked paths, scoped to this call's
    # own `paths`, whose worktree content is missing but whose index still
    # matches HEAD (a plain `rm`, never followed by `git rm`/`git add`).
    # Distinct from `swept_delete` above -- see
    # `_worktree_deleted_paths_chunked`'s own docstring for why the staged
    # and unstaged cases need two separate git reads. 2026-08-26 fix: this
    # probe is now fail-loud (`WorktreeDeletionProbeFailed`), never a
    # silent "found nothing" degrade on failure -- caught immediately below
    # and converted into a `StageOutcome` failure, mirroring the divergence
    # check's own `try`/`except` shape a few lines down.
    try:
        worktree_deleted = _worktree_deleted_paths_chunked(root, paths)
    except WorktreeDeletionProbeFailed as exc:
        return StageOutcome(
            exit_code=-1,
            failed=[
                f"explicit_stage: worktree-deletion probe indeterminate for "
                f"{len(paths)} path(s) -- refusing to guess which are "
                f"genuinely absent ({exc})"
            ],
        )

    existing: List[str] = []
    skipped: List[str] = []
    swept_renames: List[Tuple[str, str]] = []
    missing_caller_paths: List[str] = []
    already_staged_deletions: List[str] = []
    to_delete: List[str] = []

    for p in paths:
        # Membership against the three git-derived sets goes through the
        # CWD-relative key, never `p` itself -- see `_worktree_key`'s own
        # docstring for why an absolute caller path otherwise misses every one
        # of them and gets reported as genuinely absent.
        key = _worktree_key(root, p)
        # `swept_delete` membership is checked BEFORE `exists()` (2026-08-11
        # fix, see this function's own docstring "Untrack vs. add"): a
        # `git rm --cached` untrack leaves the file's content on disk, so
        # `exists()` alone cannot distinguish it from an ordinary tracked
        # path, and would route it into `existing` -- reaching the
        # ignored-path pre-filter below, where a gitignored untrack is
        # wrongly refused as if it were a blocked `git add`. `swept_delete`
        # is an INDEX fact (`git diff --cached --name-status`), independent
        # of worktree existence, so testing it first classifies a staged
        # deletion correctly regardless of whether its content still sits on
        # disk -- and an ADD/MODIFY path (never a `swept_delete` member) is
        # completely unaffected by this reordering.
        if key in swept_delete:
            if p in caller_paths:
                # This IS the caller's own deletion, already staged (e.g. a
                # prior `git rm`) -- no `git add` needed, but it belongs in
                # the commit set exactly like a diverged path does (staged,
                # not re-touched by this call).
                skipped.append(f"already-staged-deleted:{p}")
                already_staged_deletions.append(p)
            else:
                skipped.append(f"swept-deleted:{p}")
        elif (root / p).exists():
            existing.append(p)
        elif key in swept_rename:
            new = swept_rename[key]
            if "|" in p or "|" in new:
                # Ambiguous to the pipe-delimited forwarding value -- cannot
                # safely report this as a rename. Falls through to "missing",
                # but a caller-named path must still be flagged as such
                # (2026-08-04 latent-bug fix: this branch previously always
                # tagged bare "missing:<p>" regardless of `caller_paths`,
                # silently dropping a caller's own path out of
                # `missing_caller_paths` -- and therefore out of any
                # caller-visible decline report -- purely because it also
                # happened to collide with a `|`-containing rename).
                if p in caller_paths:
                    skipped.append(f"missing-caller:{p}")
                    missing_caller_paths.append(p)
                else:
                    skipped.append(f"missing:{p}")
            else:
                skipped.append(f"swept:{p}->{new}")
                swept_renames.append((p, new))
        elif key in worktree_deleted:
            if p in caller_paths:
                skipped.append(f"deleted:{p}")
                to_delete.append(p)
            else:
                skipped.append(f"worktree-deleted:{p}")
        elif p in caller_paths:
            skipped.append(f"missing-caller:{p}")
            missing_caller_paths.append(p)
        else:
            skipped.append(f"missing:{p}")

    # Ignored-path pre-filter (2026-08-03 fix, index-aware single-call form
    # 2026-08-03): among `existing` (exists on disk), pull out the subset
    # `git check-ignore` would refuse a fresh `git add` on -- see
    # `explicit_stage`'s own docstring for why `check_ignore()`'s default
    # index-aware behavior already excludes tracked paths on its own, and why
    # this runs before the divergence check / `to_stage` is built. One
    # batched call, never per-path.
    ignored_caller_paths: List[str] = []
    if existing:
        ignore_result = git_native.check_ignore(root, existing)
        if ignore_result.returncode in (0, 1):
            ignored_set = {
                match[3]
                for match in git_native.parse_check_ignore_stdin_z(ignore_result.stdout)
            }
        else:
            # Indeterminate check-ignore answer -- fail closed by treating
            # nothing as ignored (existing pre-fix behavior for this batch);
            # a genuinely ignored path still gets caught below by `git add`
            # itself, just without this pre-filter's benefit for this one
            # call. Never silently swallowed: surfaced narrowly, not as a
            # hard `failed` entry, since the ordinary `git add` path below
            # remains a correct (if less tolerant) fallback.
            ignored_set = set()
    else:
        ignored_set = set()

    if ignored_set:
        existing = [p for p in existing if p not in ignored_set]
        for p in ignored_set:
            if p in caller_paths:
                skipped.append(f"ignored-caller:{p}")
                ignored_caller_paths.append(p)
            else:
                skipped.append(f"ignored:{p}")

    missing_caller_exit_code = 2 if (missing_caller_paths or ignored_caller_paths) else 0

    # Divergence check (C4): scoped to the existing-path subset only, via
    # pathspec -- one batched call, never a full-tree walk, never per-path.
    # See `explicit_stage`'s own docstring for the incident this closes and
    # the cost note.
    #
    # `fail_loud=True`: this check's answer directly drives the `git add`
    # decision two lines below -- the identical commit-mechanism-selection
    # hazard `git_native.commit_scoped()` guards against (see that
    # function's own `fail_loud` comment), one layer up. An indeterminate
    # `git diff` result must not be read as "nothing diverged", or a
    # genuinely diverged path gets silently re-added here, overwriting its
    # deliberately-staged content before `commit_scoped()` downstream ever
    # gets a chance to observe and preserve it -- reproducing 506748a0
    # through THIS call site instead. `explicit_stage()`'s own negative-spec
    # says "never raises", so the failure is caught immediately below and
    # converted into a `StageOutcome` failure, matching every other error
    # path in this function.
    try:
        diverged: Set[str] = (
            _diverging_paths_chunked(
                existing,
                cwd=str(root),
                timeout=_DIVERGENCE_CHECK_TIMEOUT_SECS,
            )
            if existing
            else set()
        )
    except DivergenceCheckFailed as exc:
        # AC3/AC11 (docs/plans/2026-08-13-claim-release-deadlock-and-the-
        # doctrine-that-rejects-it.md, C3b): what this protects THAT GIT
        # DOES NOT -- `git add` has no concept of "was this path already
        # deliberately diverged/staged by someone else"; it happily
        # overwrites deliberately-staged content, reproducing incident
        # 506748a0 (see the comment above this `try`). Outlet, no human: a
        # bounded `_DIVERGENCE_CHECK_TIMEOUT_SECS` (5.0s) subprocess call,
        # never a hold -- on timeout/indeterminacy the caller gets an
        # immediate single-request reject naming the cause and re-issues
        # the same call; there is nothing to wait out.
        return StageOutcome(
            exit_code=-1,
            skipped=skipped,
            failed=[
                f"explicit_stage: divergence check indeterminate for {len(existing)} "
                f"path(s) -- refusing to guess which are safe to `git add` ({exc})"
            ],
            swept_renames=swept_renames,
            missing_caller_paths=missing_caller_paths,
            ignored_caller_paths=ignored_caller_paths,
        )

    # `diverging_paths()` reports CWD-relative names too (it is `git diff
    # --name-only` under `cwd=root`), so this membership test goes through
    # `_worktree_key` for the same reason the deletion/rename sets above do --
    # missing here would silently re-`git add` a deliberately-diverged path,
    # the 506748a0 shape this check exists to prevent.
    to_stage: List[str] = []
    for p in existing:
        if _worktree_key(root, p) in diverged:
            skipped.append(f"diverged:{p}")
        else:
            to_stage.append(p)

    # `to_delete` (worktree-unstaged deletions, defect A) bypasses the
    # divergence check entirely -- there is no worktree CONTENT for a
    # missing path to diverge from; `diverging_paths()` is scoped to
    # `existing` only and was never asked about these. Appended to
    # `to_stage` here so `git_native.add_paths()` stages the removal (see
    # that function's own docstring) in the SAME batched `git add` call as
    # ordinary content changes -- one subprocess, not two.
    to_stage.extend(to_delete)

    # `staged_paths` covers every existing path (diverged ones are already
    # staged -- just not by this call -- non-diverged ones become staged
    # below), every already-staged deletion (staged before this call, not
    # re-`git add`-ed), and every worktree deletion this call is about to
    # stage; order follows the original `paths` ordering restricted to each
    # subset.
    staged_paths = list(existing) + list(already_staged_deletions) + list(to_delete)

    # `deletion_paths` (see `StageOutcome.deletion_paths`'s own docstring):
    # every deletion actually included in `staged_paths` this call --
    # already-staged ones unconditionally (this call never touches them, so
    # nothing below can un-stage them), and `to_delete` ones PROVISIONALLY
    # here, reconciled against the real `git add` outcome below (never
    # reported as staged if the batch add genuinely failed to stage them).
    deletion_paths = list(already_staged_deletions)

    if not to_stage:
        return StageOutcome(
            exit_code=missing_caller_exit_code,
            skipped=skipped,
            staged_paths=staged_paths,
            swept_renames=swept_renames,
            missing_caller_paths=missing_caller_paths,
            ignored_caller_paths=ignored_caller_paths,
            checked_paths=set(existing),
            diverged_paths=diverged,
            deletion_paths=deletion_paths,
        )

    # Chunked `git add -- <to_stage>` (2026-08-15 sweep): `to_stage` is the
    # caller's own batch, unbounded by construction, and `add_paths()` puts
    # it whole on argv -- the same Windows `CreateProcess` cap hazard every
    # other batched call in this function closes. Chunking `git add` itself
    # is semantics-preserving in a way chunking a QUERY is not: staging is
    # additive and commutative across chunks (mirrors running the ORIGINAL
    # single non-atomic `git add -- to_stage` call, which itself may stage
    # some paths before failing on a later one -- see the "Residue
    # reconciliation" comment below), so stopping at the first failing
    # chunk reproduces the exact "some staged, then one fails, the rest
    # never attempted" shape a single unchunked call could already produce
    # -- never a NEW failure mode. The post-failure residue reconciliation
    # immediately below is unaffected: it reconciles against real index
    # state over the FULL `to_stage`, not per-chunk, so it still correctly
    # reports every path this call's chunks actually staged before the
    # first failure, regardless of chunk boundaries.
    add_result = git_native.GitResult(returncode=0, stdout="", stderr="")
    for _add_chunk in _chunk_paths(to_stage):
        add_result = git_native.add_paths(root, _add_chunk)
        if not add_result.ok:
            break
    if add_result.ok:
        deletion_paths.extend(to_delete)
        return StageOutcome(
            exit_code=missing_caller_exit_code,
            acted=list(to_stage),
            skipped=skipped,
            staged_paths=staged_paths,
            swept_renames=swept_renames,
            missing_caller_paths=missing_caller_paths,
            ignored_caller_paths=ignored_caller_paths,
            checked_paths=set(existing),
            diverged_paths=diverged,
            deletion_paths=deletion_paths,
        )

    # Never a bare `exit_code=N` (2026-08-03 fix, live `safe-commit-offer`
    # incident): `git add`'s failure message is not guaranteed to land on
    # stderr -- see `commit()`'s own `_reason_from_git_result` use for the
    # sibling case (`git commit`'s "nothing to commit" no-op prints to
    # STDOUT with an EMPTY stderr, confirmed empirically). `add_paths()`'s
    # ignored-path refusal DOES normally print to stderr (this pre-filter
    # exists precisely to keep that path out of `to_stage` in the first
    # place), but a caller must not assume every future `git add` failure
    # mode shares that property -- falling back to stdout, then to the
    # attempted-path list itself, keeps this diagnosable without a re-run
    # even when both streams are unexpectedly silent.
    reason = _reason_from_git_result(add_result, attempted=to_stage)

    # Residue reconciliation (empirically confirmed 2026-07-31, code-reviewer
    # Finding 1, fa1aeeeb9187 review): a mixed `git add -- to_stage` batch is
    # NOT atomic on failure -- a `.gitignore`-blocked path errors AFTER some
    # earlier paths in the same batch are already staged. Assuming `acted ==
    # []` on any add failure (the old behavior) leaves that partial residue
    # invisible to every caller's rollback bookkeeping, including the
    # widened `run_commit_pipeline` try/finally -- reproducing the exact
    # staged-and-abandoned defect that `finally` exists to close. Reconcile
    # against real index state scoped to `to_stage` (the paths THIS call
    # attempted) via a pathspec-bounded `git diff --cached --name-only --
    # to_stage`, never a bare/unscoped read -- an unscoped read could sweep
    # in a concurrent peer session's own staged work on this shared
    # worktree and misreport it as this call's residue. Diverged paths are
    # never in `to_stage` (they were filtered out above), so they can never
    # be misreported as reconciled residue here.
    #
    # If the reconciliation check itself fails (rare -- `git diff --cached`
    # against an empty/unborn HEAD, etc.), fail closed: report no residue
    # rather than guess, and surface the indeterminate check as its own
    # `failed` entry so it stays visible rather than silently swallowed.
    # Review: code-reviewer -- Finding 4: `-z` (NUL-separated, never
    # C-quoted) is required here, not the newline-separated default --
    # without it, `git diff --cached --name-only` C-quotes any path
    # containing non-ASCII bytes/quotes/backslashes/control characters,
    # which silently fails the plain-string `p in residue` membership test
    # below for exactly those paths -- under-reporting `acted` and leaving
    # genuinely-partially-staged residue invisible to this call's rollback,
    # the same "staged-and-abandoned" shape this whole reconciliation exists
    # to close, just gated on filename bytes.
    # Chunked (2026-08-15 sweep): `to_stage` is unbounded, and a single
    # `git diff --cached --name-only -- to_stage` put the WHOLE batch on
    # argv -- the exact Windows argv-length defect that read as
    # "explicit_stage: post-failure residue check indeterminate" for every
    # path in a percolate-publish-scale batch, even when nothing was
    # actually unconfirmable. `_residue_paths_chunked` below answers each
    # chunk independently and reports which (if any) chunk could not
    # answer -- a chunk-level failure degrades ONLY that chunk's own paths
    # to unconfirmed (fail-closed for exactly the paths this call cannot
    # vouch for), rather than the pre-chunking behaviour of treating the
    # ENTIRE batch as indeterminate merely because one argv-bounded slice
    # of it failed. This is a strict improvement on the same fail-closed
    # posture, never a relaxation: a chunk that never answers still leaves
    # its paths out of `reconciled_acted`, exactly as the old whole-batch
    # failure did for every path.
    residue, residue_indeterminate, residue_failure = _residue_paths_chunked(root, to_stage)
    # `_worktree_key` for the same reason the classification sets use it:
    # `git diff --cached --name-only` reports CWD-relative names, so a raw
    # membership test under-reports residue for an absolute caller path --
    # the invisible-residue shape this reconciliation exists to close.
    reconciled_acted = [p for p in to_stage if _worktree_key(root, p) in residue]
    failed_entries = [f"git add: {reason}"]
    if residue_indeterminate:
        assert residue_failure is not None
        failed_entries.append(
            "explicit_stage: post-failure residue check indeterminate for one or "
            "more path chunk(s) -- cannot confirm whether every one of this "
            "call's paths were partially staged "
            f"({condense_git_diagnostic(residue_failure.stderr) or f'exit_code={residue_failure.returncode}'})"
        )

    return StageOutcome(
        exit_code=add_result.returncode,
        acted=reconciled_acted,
        skipped=skipped,
        failed=failed_entries,
        # Latent-bug fix (2026-08-03, in-scope carve-out): this branch never
        # passed `staged_paths` before, defaulting it to `[]` even though
        # `existing` (diverged-already-staged paths included) is exactly as
        # valid here as on every other return in this function -- see
        # `StageOutcome.staged_paths`'s own docstring. `run_commit_pipeline`'s
        # rollback uses `acted`, not `staged_paths`, so this did not cause
        # the live incident, but a caller inspecting `staged_paths` after a
        # `stage.failed` return got a silently wrong (empty) answer.
        staged_paths=staged_paths,
        swept_renames=swept_renames,
        missing_caller_paths=missing_caller_paths,
        ignored_caller_paths=ignored_caller_paths,
        checked_paths=set(existing),
        diverged_paths=diverged,
        # Same non-reconciled convention as `staged_paths` above on this
        # failure branch (see the Latent-bug-fix comment on that field,
        # immediately above): `deletion_paths` mirrors it rather than
        # reconciling independently against `reconciled_acted` -- an
        # already-staged deletion is unaffected by this call's failed `git
        # add` regardless, and a `to_delete` entry that genuinely failed to
        # stage surfaces via `failed`/`acted` already, same as any other
        # partially-staged residue this branch reports.
        deletion_paths=deletion_paths + to_delete,
    )


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------


#: Matches a bare full git object sha -- how `commit()` (C11) distinguishes
#: `commit_scoped()`'s private-index branch (whose `GitResult.stdout` IS the
#: new commit sha, verbatim from `commit-tree`/`update-ref`) from its agree
#: branch (whose `stdout` is `git commit`'s human-readable summary text, e.g.
#: `"[main abc1234] subject\\n 1 file changed..."`) -- never a bare hex-digest
#: string on its own. Widened to 40-64 hex (S1 Finding 6, review
#: state/review-trail/2026-08-08-excise-lock-close-review/s1-c10-c11.md):
#: `commit_scoped()` has no separate signal for which branch ran, so a
#: SHA-256 repository's 64-hex private-index stdout must still match here --
#: a 40-only pattern would silently fall through to the slower, now-possibly-
#: failing agree-branch message-match path on a commit that CAS-verified
#: perfectly. See `commit()`'s own docstring for the full mechanism.
_FULL_SHA_RE = re.compile(r"[0-9a-f]{40,64}")

#: `_ends_with_trailer_block` and its `_TRAILER_LINE_RE` now live in
#: `commit_message.py` (imported above) -- shared with `compose_message()`'s
#: own trailer-append logic, which hit the identical hazard this module's
#: `commit()` token mint was built to avoid (staff-eng R1 F1,
#: state/review-trail/2026-08-08-landed-commit-close-review/r1-w1.md). Do
#: not redefine a second copy here; see that module's docstring for the
#: full five-shape predicate contract.


@dataclass(frozen=True)
class CommitOutcome:
    """Typed result of the `git commit -F <msgfile> -- <paths>` step.

    Fields:
        exit_code -- 0 on success.
        committed_sha -- the VERIFIED landed commit sha (C11,
            docs/plans/2026-08-07-excise-the-ceremony-lock.md): the
            private-index branch's own CAS-verified `stdout`, or -- for the
            agree branch -- a `git log --grep=<token> --fixed-strings
            <pre-sha>..HEAD` match against this call's own minted
            `Commit-Token:` trailer (docs/plans/2026-08-08-a-landed-commit-
            reported-as-failed.md, W1). None on any commit failure OR on a
            failed/ambiguous verification -- never a blind
            `git rev-parse HEAD`, which could return a concurrent sibling's
            own commit landing in the same window.
        landed -- "did history change": True iff the `git commit`
            invocation actually created a commit, set INDEPENDENTLY of
            whether `committed_sha` could be resolved (docs/plans/
            2026-08-08-a-landed-commit-reported-as-failed.md, W1). True on
            every path where `commit_scoped()` succeeded -- the two
            sha-resolved success returns AND all three post-success
            verification-failure returns (HEAD unresolvable on an
            unborn-branch first commit, empty message subject, zero-or-
            ambiguous token match) -- because in every one of those cases
            `git commit` already created the commit; only `committed_sha`
            is unknown. False on a genuine `git commit` failure, including
            the ordinary "nothing to commit" empty-commit-set no-op --
            history did not change on that path, and this flag must not
            invert that into a phantom commit.
        stderr -- git's stderr on a non-zero exit, "" on success. Exception:
            the private-index branch's success return (see
            `worktree_excluded` below) also carries a loud stderr message
            here even though `exit_code == 0` -- a staged-only commit is a
            legitimate SUCCESS, not a failure, but the operator still needs
            to see why.
        worktree_excluded -- (state/bug-backlog/2026-08-10-scoped-git-commit-
            reports-success-while-334e90d707f9.yaml) mirrors
            `git_native.GitResult.worktree_excluded` verbatim on the
            private-index success branch: repo-relative paths whose
            WORKING-TREE content was NOT included in this commit because it
            diverged from the staged (index) content that was committed
            instead. Empty tuple (default) on every other outcome, including
            the agree branch's own success -- never populated there because
            the agree branch never diverges by construction.
        reason -- AC7 (docs/plans/2026-08-14-the-tool-stages-what-it-commits.md,
            C3): a short machine-readable tag distinguishing WHY this commit
            did not land, one of `"patch-did-not-apply"`, `"head-blob-cas-
            refusal"`, `"index-head-cas-refusal"`, `"commit-failure"` -- ""
            on success. `commit()`'s own docstring names which condition maps
            to which tag; a caller building a distinct exit path per failure
            mode (the CLI) keys off this, not off `stderr` text. Never set for
            the ordinary already-committed no-op (`exit_code == 1`,
            `landed=False`) -- that is `"empty-commit-set"`, decided one layer
            up (`scoped_git_commit._classify_uncommitted`, which alone has the
            `git status` probe this distinction needs).
        unprovenanced_paths -- AC4: when `stage_patch` (below) was supplied,
            the subset of `commit_paths` the patch did NOT write new content
            for -- these take today's ordinary staged-or-worktree path,
            unchanged, so the resulting commit is provenanced for some paths
            and worktree-sourced for others. Empty tuple whenever
            `stage_patch` is not supplied, or every named path was covered by
            the patch. Mirrors `worktree_excluded`'s own never-a-silent-drop
            posture -- never inferred by a caller from absence.
        stdout_diagnostic -- AC10 (docs/plans/2026-08-15-the-ceremony-tail-
            stops-lying-about-why-it-failed.md, C6): `condense_git_
            diagnostic(result.stdout)` on the `not result.ok` branch,
            populated UNCONDITIONALLY (both for the genuine "nothing to
            commit" no-op and for a real refusal whose diagnosis happens to
            land on stdout rather than stderr -- `git commit -F ... --
            paths` is confirmed to sometimes leave `stderr` empty with the
            real diagnosis on `stdout` instead, see the 2026-08-03 comment at
            this class's own `commit()` call site). "" on every other
            outcome, including success.

            Deliberately ADDITIVE, never folded into `stderr`: `stderr` is a
            MATCHED field two consumers key off in its exact bare
            `exit_code=N` shape (`coordinator/bin/scoped-git-commit`'s
            `_BARE_EXIT_CODE_RE`, and `scoped_git_commit.py::
            _classify_uncommitted`'s `_BARE_EXIT_CODE_STDERR_RE` conjunct of
            `reclassifiable`) to render/reclassify the ordinary already-
            committed no-op quietly; folding stdout text into `stderr` would
            insert git's own no-op vocabulary into that matched shape and
            flip the most common benign outcome into a loud refusal at the
            op layer. This field carries the same information through a
            separate channel instead, for a caller to surface once it has
            independently determined the outcome is NOT the benign no-op.

            NOT YET THREADED into `PipelineResult.diagnostics` by
            `run_commit_pipeline` (see that function's own AC10-STOP comment,
            same call site): only `scoped_git_commit.py::_classify_
            uncommitted`'s `git status --porcelain` probe can distinguish the
            benign no-op from a real failure whose diagnosis happens to land
            on stdout, and `PipelineResult.diagnostics` has a SECOND raw
            reader that never reaches that probe -- `wsc_tail.py`'s own
            commit step extends its own diagnostics from `pipeline_result.
            diagnostics` unconditionally, no reclassifier in between. This
            field exists and is populated (AC10's first half) so a future,
            reclassification-aware consumer-side chunk (touching
            `scoped_git_commit.py` and/or `wsc_tail.py`) can surface it
            without a `commit_pipeline.py` change; wiring that surfacing is
            explicitly out of THIS chunk's scope (its `writes:` names only
            `commit_pipeline.py` and its own test file).
        reconcile_decline -- populated ONLY on the `landed=False` commit-
            failure return: the `ReconcileProbe.decline` tag (suffixed with
            the range actually searched) explaining why
            `_reconcile_landed_despite_failure` did not name a landed commit
            on a path where one may nonetheless exist. "" everywhere else,
            including every success return and every pre-commit refusal --
            those never ran a reconcile, and an empty string must not be read
            as "the reconcile found nothing".

            Diagnostic-only, never a predicate: no consumer may derive
            `committed` from it. It exists because "the reconcile declined"
            and "the reconcile never ran" render identically at the operator's
            end (`committed: false`), which is exactly what cost the
            2026-08-19 investigation a session to separate -- see
            `ReconcileProbe`'s own docstring. Surfaced to the caller by
            `scoped_git_commit.py::_handler` on the uncommitted branch, where
            it sits next to the `empty-commit-set` reason it most often
            contradicts.
    """

    exit_code: int
    committed_sha: Optional[str] = None
    landed: bool = False
    stderr: str = ""
    worktree_excluded: Tuple[str, ...] = ()
    reason: str = ""
    unprovenanced_paths: Tuple[str, ...] = ()
    stdout_diagnostic: str = ""
    reconcile_decline: str = ""


def _write_commit_message_tempfile(
    worktree_root: Path, message: str, common_dir: Optional[Path] = None,
) -> Path:
    """Write `message` to a PID-scoped temp file under the git common dir.

    Purpose: `.git/COMMIT_EDITMSG.workstream-complete.XXXXXX` equivalent,
    built via `tempfile` (never a hardcoded path) so two concurrent
    `wsc-commit` invocations on the SAME worktree never collide on the same
    filename (see the Windows port-hazard checklist cited in the module
    docstring).

    `common_dir` -- optional pre-resolved git common dir, threaded from
    `run_commit_pipeline`'s single resolution (`_resolve_pass_common_dir`) so this
    call costs no `git rev-parse` of its own. Omitted (the direct-caller
    path), it resolves exactly as before via `lifecycle.git_common_dir`;
    both produce byte-identical values (the SAME
    `rev-parse --path-format=absolute --git-common-dir` output), so this is a
    pure spawn dedup with no behavioural difference.
    """
    if common_dir is None:
        common_dir = git_common_dir(worktree_root)
    fd, raw_path = tempfile.mkstemp(
        prefix="COMMIT_EDITMSG.workstream-complete.", dir=str(common_dir)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(message)
    except BaseException:
        try:
            os.unlink(raw_path)
        except OSError:
            print(f"skip: _write_commit_message_tempfile: os.unlink(raw_path) failed: {sys.exc_info()[1]}", file=sys.stderr)
            pass
        raise
    return Path(raw_path)


#: AC7 (docs/plans/2026-08-14-the-tool-stages-what-it-commits.md, C3) --
#: `CommitOutcome.reason` tags. Named here, once, so `commit()`'s own body
#: and any test asserting on them share one spelling rather than a
#: hand-typed string re-derived at each site.
_REASON_PATCH_DID_NOT_APPLY = "patch-did-not-apply"
_REASON_STAGE_INFRA_FAILURE = "stage-infra-failure"

#: `git_native.stage_from_patch` reports failures in its OWN vocabulary
#: (`apply-failed`, `index-infra-failure`); AC7's contract at this layer and at
#: the CLI is the `patch-did-not-apply` family. Translate rather than pass
#: through — a raw pass-through silently replaces the AC7 vocabulary, and
#: collapsing every failure onto `patch-did-not-apply` tells an operator their
#: patch is bad when `read-tree`/`ls-files` was the real fault. An unrecognized
#: primitive reason maps to `stage-infra-failure`, never to a bad-patch claim
#: this layer cannot substantiate.
_STAGE_FAILURE_REASON_MAP = {
    "apply-failed": _REASON_PATCH_DID_NOT_APPLY,
    "index-infra-failure": _REASON_STAGE_INFRA_FAILURE,
}
_REASON_HEAD_BLOB_CAS_REFUSAL = "head-blob-cas-refusal"
_REASON_INDEX_HEAD_CAS_REFUSAL = "index-head-cas-refusal"
_REASON_COMMIT_FAILURE = "commit-failure"

#: Substring `_agree_branch_cas_refusal` (git_native.py) always leads its
#: `stderr` with -- the ONLY way `commit()` can distinguish that specific
#: refusal from `commit_scoped()`'s other failure shapes without `git_native.
#: py` itself carrying a `reason` field (out of this chunk's file scope; see
#: docs/plans/2026-08-14-the-tool-stages-what-it-commits.md chunk C3's own
#: `writes:` list). A string match, not a new sentinel object, precisely
#: because the producing module is not this chunk's to touch.
_INDEX_HEAD_CAS_MARKER = "compare-and-swap refused"


def _classify_commit_scoped_failure_reason(result: "git_native.GitResult") -> str:
    """AC7: map a failed `git_native.commit_scoped()` (or `_commit_scoped_
    private_index()`) result onto one of the two reasons that can still fire
    once `stage_from_patch()`/`stage_from_patch_cas_refusal()` have already
    passed (or were never in play) -- the existing agree-branch index/HEAD
    CAS refusal (`_agree_branch_cas_refusal`), or every other commit failure
    (a `pre-commit` hook BLOCK, the private-index branch's own `update-ref`
    compare-and-swap, a genuine git error). Never called for the ordinary
    already-committed no-op -- that reclassification happens one layer up,
    in `scoped_git_commit._classify_uncommitted`, which alone has the `git
    status` probe the distinction needs.
    """
    if _INDEX_HEAD_CAS_MARKER in (result.stderr or ""):
        return _REASON_INDEX_HEAD_CAS_REFUSAL
    return _REASON_COMMIT_FAILURE


#: Commits searched backwards from HEAD when `commit()` has no `pre_sha` to
#: bound the range with -- i.e. when the pre-commit `git rev-parse HEAD` itself
#: failed, which at this repo's load norm (CLAUDE.md, 50-70 concurrent LLM
#: sessions) is a timeout, not a broken repo. Declining the reconcile there
#: silences it exactly when the box is loaded enough to need it. The window is
#: a BOUND, not a correctness input: the `Commit-Token:` search key is
#: collision-free by construction (see `_reconcile_landed_despite_failure`'s
#: own SAFETY paragraph), so a match inside the window is ours no matter how
#: wide the window is, and a peer's commit can never match however many of
#: theirs it spans. Sized to cover the peer traffic one slow commit can sit
#: behind on a shared branch, not tuned.
#:
#: `git log -n <N> --grep=... HEAD` does NOT actually bound the walk to this
#: many commits -- `-n`/`--max-count` on `git log` caps the OUTPUT count,
#: never the commit graph WALK, whenever a filter (`--grep` and/or a
#: pathspec) is present (measured on this repo: `git log -n 5 --grep=<no-
#: match> HEAD`, no pathspec at all, took 1.13s and walked the full
#: ~20,067-commit history). `git rev-list --max-count=<N>`, UNFILTERED, is
#: the one form where `--max-count` is a true walk bound (measured 0.6s on
#: the same repo) -- see `_reconcile_landed_despite_failure`'s fallback path,
#: which uses this constant to size that call, not a `git log -n` call.
_RECONCILE_FALLBACK_WINDOW_COMMITS = 200


@dataclass(frozen=True)
class ReconcileProbe:
    """Why `_reconcile_landed_despite_failure` answered as it did.

    Exists because the reconcile's silence is indistinguishable from its
    absence at the operator's end: both render as `committed: false` over a
    commit that exists. A live occurrence (2026-08-19, four instances across
    two sessions in one day) cost a whole session to narrow to "the reconcile
    did not execute" and still could not say WHY, because the function's
    `Optional[str]` return threw away every decline reason on the way out.
    This type is that reason, carried to the response so the NEXT occurrence
    self-diagnoses instead of costing another investigation.

    Fields:
        sha -- the reconciled commit sha, or `None` on every decline.
        decline -- "" when `sha` is set; otherwise a short machine-readable
            tag naming which precondition answered: `"log-grep-raised"`,
            `"log-grep-failed"`, `"no-candidate"` (the search ran and matched
            nothing -- the genuinely-did-not-land shape), or
            `"ambiguous-candidates:<n>"`.
        range_spec -- the revision range that produced this answer
            (`"<pre>..HEAD"` on the `pre_sha`-present path, or `"<base>..HEAD"`
            / `"HEAD"` on the fallback path -- see
            `_reconcile_landed_despite_failure`'s own docstring), so a reader
            can tell which range answered without re-deriving it.
    """

    sha: Optional[str] = None
    decline: str = ""
    range_spec: str = ""


def _reconcile_landed_despite_failure(
    root: Path,
    token_trailer: str,
    pre_sha: Optional[str],
    commit_paths: Sequence[str],
) -> ReconcileProbe:
    """The sha this call's own commit landed under DESPITE `commit_scoped()`
    reporting failure, as a `ReconcileProbe` whose `sha` is `None` -- with a
    `decline` tag naming why -- when nothing of ours is found.

    Exists because a reported failure is not proof no commit was created, and
    on this machine the common case is not a crash but a CLOCK. `git_native.
    _git` synthesizes `GitResult(returncode=-1)` for `subprocess.
    TimeoutExpired`, so a `git commit` that merely ran LONG -- entirely
    ordinary at this repo's stated load norm of 50-70 concurrent LLM
    sessions, with a Python pre-commit hook in the path -- returns
    `result.ok == False` while git itself goes on to create the commit. The
    timeout kills the wrapper, never the work: project CLAUDE.md § Load norm
    states it outright ("A timeout here is a slow op, not a hung one -- and
    it does NOT stop the engine, so reconcile before retrying"). This
    function is that reconcile, performed once at the seam instead of left
    to every operator.

    Downstream damage when it is skipped: `landed=False` reaches
    `scoped_git_commit`'s `committed` predicate as False, which falls through
    to `_classify_uncommitted`, which probes `git status`, finds the tree
    clean BECAUSE the commit landed, and reports the benign
    `reason="empty-commit-set"`. The operator is told "no commit landed"
    about a commit that exists -- and the natural next move, re-running, is
    how a duplicate commit or a swept peer file happens on a shared branch.
    Live incident: peer session 1021e7bf, 26ce6a671 (2026-08-19), reported
    against a tree that already carried the earlier W3/W3b predicate fixes --
    those widened what counts as landed, but could not help a
    `CommitOutcome` that says `landed=False` in the first place, which is why
    the repair belongs HERE and not one layer up.

    SAFETY -- why this cannot adopt a peer's commit on a shared branch. The
    search key is this call's own `Commit-Token:` trailer, whose match
    `_FULL_SHA_RE`'s own docstring already establishes as collision-free by
    construction: no peer can author this exact token string. That is the
    same key, over the same `pre_sha..HEAD` range, with the same
    `--full-history` merge-pruning guard, that the SUCCESS path one screen
    down already uses to name its sha -- deliberately reused rather than
    re-derived, so both paths agree on what "this call's commit" means. A
    bare `rev-parse HEAD` fallback is NOT used and must never be added here:
    HEAD moves under concurrent peers, and adopting whatever sits there is
    precisely the misattribution the token search exists to prevent.

    Returns a probe with `sha=None` -- leaving the caller's failure return
    untouched -- on every uncertain shape: a failed `git log`, or a
    zero/ambiguous candidate count. Never raises; a reconcile that cannot
    answer must degrade to today's behaviour, since wrongly claiming a commit
    landed is worse than the reporting defect it repairs. It no longer
    declines merely for want of `pre_sha`, which is the one decline the
    2026-08-19 investigation could not rule out and the one that fires
    precisely under the load that produces the defect -- a missing `pre_sha`
    means the pre-commit `git rev-parse HEAD` itself timed out, not that
    there is no history. That case is the FALLBACK path below; the token
    bound is what makes either path's answer safe, the range only ever makes
    it cheaper.

    Two shapes, two costs:

      `pre_sha` present -- exactly ONE `git log --grep=<token> --fixed-
      strings <pre_sha>..HEAD -- <pathspec>` call, a real revision range and
      therefore a true walk bound (unlike a filtered `-n`/`--max-count`,
      which bounds OUTPUT, never the WALK -- see `_RECONCILE_FALLBACK_
      WINDOW_COMMITS`'s own comment for the measurement). A miss here is
      `"no-candidate"`, full stop -- there used to be a second, WIDENED pass
      here (`-n <N> HEAD`, no lower bound) for a shape observed live
      2026-08-19: this call's own commit sitting OUTSIDE its own `pre_sha..
      HEAD` range, because `pre_sha` named a PEER commit six seconds newer
      than the one this call had just landed. That observation was real, but
      its cause was never an ordering fault in `commit()` -- `rev_parse_
      head()` genuinely does run before `commit_scoped()`, in that order,
      every time. The cause was the warm-engine client re-executing an
      already-delivered mutation: a SECOND execution of this same call read
      `pre_sha` AFTER a FIRST execution had already committed, so the
      "peer" commit ahead of `pre_sha` was this call's own prior execution.
      That root cause is fixed at the client (`coordinator_core/warm/
      client.py`, this session) -- with one execution per invocation,
      `pre_sha` is an ancestor of this call's own commit by construction,
      and the widened pass was defending against a shape that can no longer
      occur, at the cost of an unbounded-by-filter `git log` on the
      COMMONEST failure-path outcome there is (the ordinary already-
      committed no-op). Removed, not merely disabled -- this is retiring a
      workaround whose defect was fixed at the root, not stripping
      defensive depth.

      `pre_sha` absent (a timed-out pre-commit `git rev-parse HEAD`, not an
      empty history) -- TWO spawns, because `-n`/`--grep` cannot supply its
      own bound here (no `pre_sha` to build a real range from). First,
      `git rev-list --max-count=<N+1> HEAD`, UNFILTERED, to resolve a real
      base commit -- `--max-count` on an unfiltered `rev-list` genuinely
      bounds the walk, unlike the `git log -n --grep` shape above. Its last
      line becomes an EXCLUSIVE lower bound (`<base>..HEAD` spans exactly N
      commits), and the token search runs over that real range. When the
      base cannot be resolved (history shorter than N, an unborn branch, or
      the `rev-list` call itself failing) this does NOT refuse -- it falls
      back to searching `HEAD` with no lower bound at all, through the same
      decline-safely `_search`/`_resolve` machinery as every other case:
      the token is what makes the match safe, not the range, so an
      unbounded range here still cannot adopt a peer's commit. Two spawns
      are acceptable on this path because it is rare by construction (it
      only fires when the pre-commit HEAD read itself timed out).

    The fallback path's wider, filter-only-bounded search keeps the ANCHORED
    trailer match (`^<token_trailer>$`, `--extended-regexp`) rather than the
    bounded path's plain `--fixed-strings` substring match: a commit whose
    message merely QUOTES a token in prose (this defect's own investigation
    notes do, repeatedly) must not be adopted as a match once the search is
    no longer confined to a tight, freshly-opened range."""
    # Same one-chunk argv bound as the success path's own search, and the
    # same reasoning: this call's commit touched every path in `commit_
    # paths`, so it touched every path in any non-empty subset too.
    chunks = _chunk_paths(list(commit_paths)) if commit_paths else []
    pathspec = ["--", *chunks[0]] if (chunks and chunks[0]) else []

    def _search(pattern: str, range_args: Sequence[str], *, literal: bool):
        """One `git log --grep` pass. Returns `(status, candidates)`, status
        being "ok", "raised" or "failed"."""
        extra_args = [
            "--fixed-strings" if literal else "--extended-regexp",
            "--format=%H",
            "--full-history",
            *range_args,
            *pathspec,
        ]
        try:
            match_result = git_native.log_grep(root, pattern, extra_args=extra_args)
        except Exception:
            return "raised", []
        if not match_result.ok:
            return "failed", []
        return "ok", [line for line in match_result.stdout.splitlines() if line]

    def _resolve(status, candidates, range_spec):
        """Maps one pass's outcome onto a probe, or `None` for "matched nothing
        -- the caller may keep looking"."""
        if status == "raised":
            return ReconcileProbe(decline="log-grep-raised", range_spec=range_spec)
        if status == "failed":
            return ReconcileProbe(decline="log-grep-failed", range_spec=range_spec)
        if len(candidates) > 1:
            return ReconcileProbe(
                decline=f"ambiguous-candidates:{len(candidates)}", range_spec=range_spec
            )
        if candidates:
            return ReconcileProbe(sha=candidates[0], range_spec=range_spec)
        return None

    if pre_sha:
        # A real revision range is a true walk bound (see
        # `_RECONCILE_FALLBACK_WINDOW_COMMITS`'s own comment for why a
        # filtered `-n`/`--grep` combination is NOT), so this is the whole
        # search on this path -- exactly one `git log`, never a second,
        # wider pass. There used to be one (see this function's own
        # docstring for why: the shape it defended against was this call's
        # own commit landing OUTSIDE its own `pre_sha..HEAD` range, which
        # was never an ordering fault in `commit()` -- it was the warm-
        # engine client re-executing an already-delivered mutation, fixed at
        # the root in `coordinator_core/warm/client.py` this session). With
        # one execution per invocation, `pre_sha` is an ancestor of this
        # call's own commit by construction, so a miss here is a genuine
        # "nothing of ours landed" -- the ordinary failed-commit case.
        bounded_spec = f"{pre_sha}..HEAD"
        status, candidates = _search(token_trailer, [bounded_spec], literal=True)
        probe = _resolve(status, candidates, bounded_spec)
        if probe is not None:
            return probe
        return ReconcileProbe(decline="no-candidate", range_spec=bounded_spec)

    # FALLBACK: no `pre_sha` to build a real range from (the pre-commit
    # `git rev-parse HEAD` itself timed out). `-n`/`--max-count` on a
    # FILTERED `git log --grep` call does not bound the walk (see
    # `_RECONCILE_FALLBACK_WINDOW_COMMITS`'s own comment) -- so a real range
    # is resolved first via an UNFILTERED `git rev-list --max-count`, where
    # `--max-count` genuinely is a walk bound, then the token search runs
    # over that real range exactly like the `pre_sha`-present path above.
    # Two spawns, acceptable here because this path is rare by construction.
    rev_list_result = git_native._git(
        ["rev-list", f"--max-count={_RECONCILE_FALLBACK_WINDOW_COMMITS + 1}", "HEAD"],
        cwd=root,
        timeout=_DIVERGENCE_CHECK_TIMEOUT_SECS,
    )
    base_lines = (
        [line for line in rev_list_result.stdout.splitlines() if line]
        if rev_list_result.ok
        else []
    )
    if len(base_lines) > _RECONCILE_FALLBACK_WINDOW_COMMITS:
        # The (N+1)th-oldest line is an EXCLUSIVE lower bound -- `base..HEAD`
        # then spans exactly `_RECONCILE_FALLBACK_WINDOW_COMMITS` commits,
        # the same window size the old `-n <N>` call named, just as a real
        # range instead of an output cap.
        window_spec = f"{base_lines[-1]}..HEAD"
    else:
        # History shorter than the window, an unborn branch, or the
        # `rev-list` call itself failed -- decline-safely to the unbounded
        # range rather than refusing outright: the ANCHORED token match
        # below is what makes even an unbounded search safe, so there is no
        # correctness reason to refuse just because a bound could not be
        # established.
        window_spec = "HEAD"

    # The wider (or unbounded) range admits one thing a tight range does
    # not: a commit whose message QUOTES a token in prose rather than
    # carrying it as its own trailer (this defect's own investigation notes
    # do, repeatedly) -- so this pass drops `--fixed-strings` for an
    # ANCHORED trailer match: the token must be the whole line, exactly as
    # `commit()` appends it. Strictly tighter matching than the bounded
    # path's plain substring match, not looser.
    status, candidates = _search(f"^{token_trailer}$", [window_spec], literal=False)
    probe = _resolve(status, candidates, window_spec)
    if probe is not None:
        return probe
    return ReconcileProbe(decline="no-candidate", range_spec=window_spec)


def commit(
    worktree_root: Union[str, Path],
    *,
    message: str,
    commit_paths: Sequence[str],
    common_dir: Optional[Path] = None,
    deliverable_id: Optional[str] = None,
    stage_patch: Optional[Union[str, Path]] = None,
    suppress_post_commit_auto_push: bool = False,
    attributed_session_id: Optional[str] = None,
) -> CommitOutcome:
    """Commit exactly `commit_paths`, via `git_native.commit_scoped()` (C3/C4).

    Explicit pathspec ALWAYS -- never a bare `git commit`. Writes `message`
    to a PID-scoped temp file, then delegates the actual commit MECHANISM to
    `commit_scoped()` -- which computes, from OBSERVED index/worktree
    divergence, whether `git add -- paths` + `git commit -F msgfile --
    paths` (the agree branch) is safe, or whether a diverged path forces the
    private-index branch (builds the commit tree under a throwaway index
    copy, preserving each diverged path's staged content verbatim, and lands
    via a compare-and-swap `update-ref`). See `commit_scoped`'s own
    docstring for the two incidents (claude-klabauter 506748a0, DoE-claude
    726925b2) neither commit form is safe against alone. Unlinks the temp
    file in a `finally` regardless of outcome.

    No `known_checked`/`known_diverged` dedup pass-through (C10,
    docs/plans/2026-08-07-excise-the-ceremony-lock.md -- removed here, was a
    same-lock-hold-only optimisation): after C1 there is no `ceremony_lock`
    bounding the window between `explicit_stage()`'s own divergence probe
    and this call, so a precomputed answer could be stale by the time
    `commit_scoped()` uses it to pick the commit mechanism -- the same
    claude-klabauter 506748a0 incident shape, through the function built to
    prevent it. `commit_scoped()` now always derives divergence fresh for
    every path in `commit_paths`, immediately before it picks the mechanism.
    Cost: up to two additional pathspec-scoped `git diff` spawns per commit
    -- exactly the cost the dedup removed, whose removal was justified by a
    lock C1 deletes.

    Sha capture (C11, same plan): neither commit branch's real HEAD-move is
    trusted via a blind post-commit `git rev-parse HEAD` -- that call, run
    after this pipeline's own lock hold ended, could return a concurrent
    sibling's own commit landing in the window instead of this call's. The
    private-index branch's `GitResult.stdout` IS the new commit sha already
    (CAS-verified by `update-ref`'s 4-argument compare-and-swap), detected
    here via `_FULL_SHA_RE` since `commit_scoped()` itself has no separate
    signal for which branch ran (git_native.py takes no executable-code
    change here, by design -- see this chunk's own scope note). The agree
    branch's `stdout` is `git commit`'s human-readable summary text, not a
    sha, so its `committed_sha` is instead resolved by matching a
    per-commit `Commit-Token: <uuid4().hex>` trailer -- minted HERE, inside
    this function, and appended to `message` before it is written to the
    temp file (docs/plans/2026-08-08-a-landed-commit-reported-as-failed.md,
    W1) -- against a pathspec-scoped `git log --grep=<token> --fixed-strings
    --format=%H <pre-commit-HEAD>..HEAD`, bounded to commits since this
    call's own pre-commit HEAD, scoped to `commit_paths` -- and failing loud
    (non-zero `exit_code`, `committed_sha=None`) on zero or more-than-one
    matches rather than guessing. The token (not the prior subject
    substring) is the match target because no peer can ever author this
    exact string, so the match is collision-free even against a peer commit
    whose message merely *contains* this call's subject (a revert, a
    rollup, a memo quoting it) -- the prior subject-substring match was
    vulnerable to exactly that spurious second candidate. Minted inside
    `commit()` rather than threaded from a caller deliberately: the value
    must be one only this function matches on, and no caller needs to know
    it exists.

    `CommitOutcome.landed` (W1, same plan): every one of these three
    verification-failure returns below -- HEAD unresolvable on an
    unborn-branch first commit, empty message subject, zero-or-ambiguous
    token match -- sets `landed=True` alongside `committed_sha=None` and
    `exit_code=1`, because in every one of them `commit_scoped()` already
    returned `ok`: `git commit` created a commit, only its sha is unknown.
    The earlier `not result.ok` return above is the ONLY commit()-internal
    path that stays `landed=False` -- see that branch's own comment for why
    the ordinary "nothing to commit" no-op must never be reported as landed.

    `common_dir` -- optional pre-resolved git common dir, passed straight
    through to `_write_commit_message_tempfile` (see there); purely a spawn
    dedup, never an outcome input.

    `deliverable_id` (C7b, docs/plans/2026-08-10-a-commit-trailer-that-names-
    the-session.md) -- optional, passed straight through to
    `git_native.commit_scoped()`'s own parameter of the same name. `None`
    (the default) leaves every existing caller's behaviour unchanged; a
    caller that already holds a provenance-bearing id (sourced from the
    plan it is executing against) may pass it here to have it land as this
    commit's `Deliverable-Id:` trailer. Not sourced or defaulted here --
    `commit()` performs no discovery of its own.

    `stage_patch` (C3, docs/plans/2026-08-14-the-tool-stages-what-it-commits.md)
    -- optional path to a patch file. When given, `git_native.stage_from_
    patch()` applies it under a process-private temporary index, bounded to
    `commit_paths`, IMMEDIATELY before the commit step below (no probe-then-
    act gap -- plan anti-scope: staging and committing happen in the same
    call, never cached across an intervening step). The tool stages what it
    commits: every path the patch writes new content for is committed with
    THAT blob, verbatim, never re-read from the worktree or the shared index
    -- provenance by construction (see `git_native.stage_from_patch`'s own
    docstring for why a private index makes this unforgeable by a peer). A
    named path the patch does NOT touch (AC4, additive per-path) falls
    through to `git_native.commit_scoped()`'s own ordinary staged-or-worktree
    resolution unchanged -- see `CommitOutcome.unprovenanced_paths` for how
    that mixed shape is named on the response, never left for a caller to
    infer from absence.

    AC3's atomicity and AC2's base-hole CAS are `stage_from_patch()`'s own
    (see that function's docstring) -- a failed apply returns
    `CommitOutcome(exit_code=-1, reason="patch-did-not-apply")` and a stale
    HEAD blob (a peer committed to a named path between the apply and this
    commit) returns `CommitOutcome(exit_code=-1, reason="head-blob-cas-
    refusal")`. Both use `exit_code=-1` -- the SAME Python-side-refusal
    sentinel `commit_scoped()`'s own `_agree_branch_cas_refusal` already
    uses, deliberately never `1` -- `0e80865cc` narrowed `scoped_git_commit.
    _classify_uncommitted`'s reclassifier to `exit_code == 1` precisely so a
    Python-side refusal is never read as "nothing to commit" (AC7's trap);
    using `1` here would silently launder either refusal back into that
    benign no-op the moment the caller's worktree happened to already match
    HEAD.

    `attributed_session_id` (state/bug-backlog/2026-08-18-scoped-git-commit-
    stamps-a-foreign-session-id-8d21f0c4e7b9.yaml) -- optional, passed
    straight through to `git_native.commit_scoped()`'s own parameter of the
    same name: the CALLER's own already-resolved committing-session
    identity, authoritative for the `Session-Id:` trailer over a blind
    env-var read. `None` (the default) leaves every existing caller's
    behaviour unchanged.
    """
    root = Path(worktree_root)
    # Mint a per-commit token (W1) and append it as a `Commit-Token:` trailer
    # BEFORE the message is written to the temp file. Review: staff-eng R1
    # F1 -- when `message` already ends in a trailer block (the common
    # `wsc_tail` case: `compose_message()` appended `Nature:`/`Plan:`/
    # `Plan-Id:`), the token MUST join that same block (a single "\n", no
    # blank-line paragraph break) rather than start a new paragraph --
    # starting a new one demotes every pre-existing trailer to body prose
    # for git's trailer parser (see `_ends_with_trailer_block`'s own
    # docstring). Only when `message` does NOT already end in a trailer
    # block does the token start its own paragraph, using the original
    # blank-line-before-trailers convention `commit_message.compose_message`
    # itself implements (`"\n" + trailers + "\n"`).
    token = uuid.uuid4().hex
    token_trailer = f"Commit-Token: {token}"
    if _ends_with_trailer_block(message):
        # Normalize to EXACTLY one trailing newline before joining. A bare
        # `endswith("\n")` test is not enough: `compose_message()` returns
        # `subject + "\n"`, so a caller whose `subject` already carried its
        # own trailing newline -- every `-F <file>` caller, since
        # `scoped-git-commit` passes the file's whole text as `subject` --
        # yields a message ending "\n\n". Retaining both newlines here puts a
        # BLANK line before the token, which is precisely the paragraph break
        # this branch exists to avoid: git's trailer parser then reads only
        # the token's own paragraph as trailers and demotes every trailer the
        # caller wrote to body prose. Observed on b1e0881d39a7 and 3301a8d1f68c,
        # whose `Deliverable-Id:` reads empty to
        # `%(trailers:key=Deliverable-Id,valueonly)` and so cannot be joined to
        # its plan by `close_out_and_stamp`.
        base = message.rstrip("\n") + "\n"
        message_with_token = base + token_trailer + "\n"
    elif message.endswith("\n\n"):
        message_with_token = message + token_trailer + "\n"
    elif message.endswith("\n"):
        message_with_token = message + "\n" + token_trailer + "\n"
    else:
        message_with_token = message + "\n\n" + token_trailer + "\n"
    msg_file = _write_commit_message_tempfile(root, message_with_token, common_dir)
    try:
        # Pre-commit HEAD, captured BEFORE `commit_scoped()` runs -- the
        # lower bound `committed_sha`'s post-commit verification (below)
        # scopes its `git log --grep` search to, so a peer's commit landing
        # before this call started is never a candidate match. `None` on an
        # unborn branch (this repo's very first commit) -- handled as its
        # own case below, not a failure.
        pre_sha_result = git_native.rev_parse_head(root)
        pre_sha = pre_sha_result.stdout.strip() if pre_sha_result.ok else None
        unprovenanced_paths: Tuple[str, ...] = ()
        supplied_blobs: Optional[Dict[str, str]] = None
        if stage_patch is not None:
            # AC1/AC3: applied HERE, immediately before the commit step
            # below -- never earlier, never cached (plan anti-scope: no
            # probe-then-act gap). A failed apply, or an unkeyable/directory/
            # empty pathspec, refuses the WHOLE call with `exit_code=-1`
            # (never `1` -- see this function's own docstring, AC7's trap).
            staged = git_native.stage_from_patch(stage_patch, commit_paths, root)
            if not staged.ok:
                # Negative spec: the primitive's own `reason` survives to the
                # caller. Collapsing every failure onto `patch-did-not-apply`
                # tells an operator their patch is bad when the real fault was
                # `read-tree`/`ls-files` (`index-infra-failure`) -- the exact
                # misdirection AC7's distinguishable-reasons requirement
                # exists to prevent. An unmapped reason still exits non-zero
                # (`_EXIT_BUSINESS_FAIL`), never success.
                return CommitOutcome(
                    exit_code=-1,
                    stderr=staged.stderr,
                    reason=_STAGE_FAILURE_REASON_MAP.get(
                        getattr(staged, "reason", None) or "",
                        _REASON_STAGE_INFRA_FAILURE,
                    ),
                )
            # AC2's base hole -- re-observed immediately before the commit
            # this guards (`stage_from_patch_cas_refusal`'s own docstring):
            # refuses rather than silently reverting a peer's commit that
            # landed on a named path between the apply above and here.
            cas_refusal = git_native.stage_from_patch_cas_refusal(
                root, commit_paths, staged.head_blobs
            )
            if cas_refusal is not None:
                return CommitOutcome(
                    exit_code=-1,
                    stderr=cas_refusal.stderr,
                    reason=_REASON_HEAD_BLOB_CAS_REFUSAL,
                )
            supplied_blobs = staged.blobs
            # AC4: additive per-path -- named exactly once here, never
            # inferred by a caller from absence (mirrors `worktree_excluded`'s
            # own never-a-silent-drop posture).
            unprovenanced_paths = tuple(p for p in commit_paths if p not in supplied_blobs)

        # `commit_scoped()` itself resolves `supplied_blobs` (C1/C2/C3,
        # docs/plans/2026-08-14-the-tool-stages-what-it-commits.md): a
        # supplied path always routes to `_commit_scoped_private_index` and
        # is NEVER exposed to the agree branch's `git add`, regardless of
        # what its own `diverging_paths()` call finds -- see that function's
        # own `supplied_blobs` docstring paragraph. Passing the map straight
        # through (rather than re-deriving the diverged/non_diverged split
        # here) is what keeps mechanism selection computed exactly once
        # (C1's own point) and keeps `_agree_branch_cas_refusal` on the path
        # for every other call.
        result = git_native.commit_scoped(
            commit_paths,
            msg_file,
            root,
            deliverable_id=deliverable_id,
            supplied_blobs=supplied_blobs,
            suppress_post_commit_auto_push=suppress_post_commit_auto_push,
            attributed_session_id=attributed_session_id,
        )
        if not result.ok:
            # Deliberately NOT `_reason_from_git_result()` here (2026-08-03
            # research note, kept for the next reader): `git commit -F ... --
            # paths`'s "nothing to commit" no-op prints its diagnosis to
            # STDOUT with an EMPTY `stderr` (confirmed empirically) -- but
            # this exact bare `exit_code=N` shape, with NO "git commit: "
            # prefix, is the on-purpose signal
            # `coordinator/bin/scoped-git-commit`'s `_BARE_EXIT_CODE_RE`
            # relies on (see its own module + `TestRefusalReporting` in
            # `coordinator/bin/tests/test_scoped_git_commit_cli.py`) to
            # render the benign already-committed no-op QUIETLY rather than
            # as a loud "REFUSED". Composing a real (non-bare) diagnosis here
            # would flip that renderer's classification and make the single
            # most common outcome there is cry wolf -- the exact regression
            # `test_benign_already_committed_noop_does_not_cry_wolf` guards.
            # `explicit_stage()`'s OWN `reason` composition (the incident's
            # actually-cited bug) is safe to fix because its `failed` entries
            # are always prefixed `"git add: {reason}"`, never bare -- this
            # call site has no such prefix, so bare is the load-bearing shape.
            #
            # AC10 (C6, same plan): `stdout_diagnostic` carries whatever
            # `result.stdout` actually said, unconditionally -- an ADDITIVE
            # field, never folded into `stderr` above (see `CommitOutcome.
            # stdout_diagnostic`'s own docstring for why: `stderr` stays
            # exactly bare `exit_code=N` here on purpose, the matched shape
            # both downstream consumers key off).
            # A reported failure is not proof no commit was created -- a
            # merely SLOW `git commit` times out in `git_native._git` (which
            # synthesizes returncode=-1) while git goes on to land it. Ask
            # before concluding; see `_reconcile_landed_despite_failure` for
            # why this is safe against adopting a peer's commit, and for the
            # live incident (26ce6a671) it closes. Runs on the failure path
            # only, so the hot path is untouched.
            reconcile = _reconcile_landed_despite_failure(
                root, token_trailer, pre_sha, commit_paths
            )
            reconciled_sha = reconcile.sha
            if reconciled_sha is not None:
                # This call's own Commit-Token found the commit -- the
                # "landed but reported as failed" shape, most often a slow
                # commit that outran its timeout under load. (The other
                # shape once reachable here -- this call's own commit
                # sitting OUTSIDE `pre_sha..HEAD`, an apparent ordering
                # violation -- was traced to the warm-engine client
                # re-executing an already-delivered mutation, not to
                # anything in `commit()`'s own step order; fixed at the
                # root in `coordinator_core/warm/client.py`, so it is no
                # longer a live shape this branch needs to distinguish.)
                _explanation = (
                    "commit: git reported failure "
                    f"(exit_code={result.returncode}) but this call's own "
                    f"Commit-Token names {reconciled_sha} in {reconcile.range_spec} "
                    "-- the commit LANDED and is reported as landed. Most "
                    "often a slow commit that outran its timeout under load; "
                    "do NOT re-run. "
                )
                return CommitOutcome(
                    exit_code=0,
                    committed_sha=reconciled_sha,
                    landed=True,
                    stderr=(
                        _explanation
                        + f"{condense_git_diagnostic(result.stderr) or ''}"
                    )[:500],
                    unprovenanced_paths=unprovenanced_paths,
                    stdout_diagnostic=condense_git_diagnostic(result.stdout),
                )
            # `stderr` stays EXACTLY the bare `exit_code=N` shape two
            # downstream consumers match on (see this branch's own comment
            # above and `stdout_diagnostic`'s docstring) -- the decline rides
            # its own additive field for the same reason, never folded in.
            return CommitOutcome(
                exit_code=result.returncode or 1,
                stderr=(
                    condense_git_diagnostic(result.stderr)
                    or f"exit_code={result.returncode}"
                ),
                reason=_classify_commit_scoped_failure_reason(result),
                unprovenanced_paths=unprovenanced_paths,
                stdout_diagnostic=condense_git_diagnostic(result.stdout),
                # The token is on the wire, not just the range. It is the
                # single datum that separates the two remaining theories for a
                # `no-candidate` decline over a commit that demonstrably
                # landed: if the landed commit carries THIS token, one call
                # both minted it and failed to find it (so the range is
                # wrong); if it carries a different one, a SECOND execution
                # minted a fresh token and searched for a commit only the
                # first ever made. `git log -1 --format=%B` on the commit
                # answers it in one step -- see the 2026-08-19 live
                # observation in this defect's own bug-backlog entry, where
                # the searched range began at a commit that POSTDATES the
                # landed one by six seconds.
                reconcile_decline=(
                    f"{reconcile.decline} (searched {reconcile.range_spec}"
                    f", {token_trailer})"
                    if reconcile.decline
                    else ""
                ),
            )

        stdout = result.stdout.strip()
        if _FULL_SHA_RE.fullmatch(stdout):
            # Private-index branch (C11) -- `stdout` IS the CAS-verified new
            # commit sha; no further verification needed, and no risk of
            # picking up a peer's commit (see `commit()`'s own docstring).
            return CommitOutcome(
                exit_code=0,
                committed_sha=stdout,
                landed=True,
                stderr=result.stderr,
                worktree_excluded=result.worktree_excluded,
                unprovenanced_paths=unprovenanced_paths,
            )

        # Agree branch (C11) -- `result.stdout` is `git commit`'s summary
        # text, not a sha. Resolve `committed_sha` by matching this call's
        # own minted `Commit-Token:` trailer against the bounded,
        # pathspec-scoped range instead of a blind `git rev-parse HEAD` (W1
        # -- token, not subject substring; see `commit()`'s own docstring).
        subject = message.splitlines()[0] if message else ""
        if pre_sha is None:
            # Unborn-branch edge case (this repo's very first commit): a
            # concurrent first-commit race on an unborn branch (a peer also
            # racing to create this branch's root commit) is not reachable in
            # the ceremony's own bootstrap -- residual accepted (S1 Finding
            # 8), not asserted impossible. Absent that race, the fresh HEAD
            # is unambiguously this call's own commit -- no message match
            # needed to disambiguate.
            post_sha_result = git_native.rev_parse_head(root)
            if post_sha_result.ok and post_sha_result.stdout.strip():
                return CommitOutcome(
                    exit_code=0,
                    committed_sha=post_sha_result.stdout.strip(),
                    landed=True,
                    unprovenanced_paths=unprovenanced_paths,
                )
            # `git commit` already created this commit (`result.ok` above) --
            # only its sha is unresolvable. W1: `landed=True` alongside
            # `committed_sha=None` so the caller never treats this as a no-op.
            return CommitOutcome(
                exit_code=1,
                landed=True,
                stderr="commit: landed but sha verification failed -- HEAD unresolvable "
                "on an unborn-branch first commit",
                unprovenanced_paths=unprovenanced_paths,
            )
        if not subject:
            # Same reasoning as the unborn-branch case immediately above:
            # the commit landed, only verification could not proceed.
            return CommitOutcome(
                exit_code=1,
                landed=True,
                stderr="commit: landed but sha verification requires a non-empty message subject",
                unprovenanced_paths=unprovenanced_paths,
            )

        # Pathspec restriction bounded to ONE argv-safe chunk (2026-08-15
        # sweep), not the full `commit_paths` -- the same Windows argv-
        # length hazard every other batched call in this module closes,
        # `git log --grep=... -- <commit_paths>` included. Unlike the
        # residue/classification probes above, this is not a per-path
        # membership question -- it is "find the ONE commit this call just
        # made", and the trailing pathspec exists only to narrow `git log`
        # to commits that touched at least one real path from THIS commit
        # (defense-in-depth: `_FULL_SHA_RE`'s own docstring already
        # establishes the `Commit-Token:` match is collision-free by
        # construction, since no peer can ever author this exact token
        # string). This call's own commit necessarily touched EVERY path in
        # `commit_paths`, so it necessarily touched every path in any
        # non-empty SUBSET of `commit_paths` too -- restricting to the
        # first argv-safe chunk (never the whole list) preserves the same
        # narrowing property without needing every path on argv, and finds
        # the identical single candidate a full-pathspec search would.
        log_grep_paths = _chunk_paths(commit_paths)[0]
        match_result = git_native.log_grep(
            root,
            token_trailer,
            extra_args=[
                "--fixed-strings",
                "--format=%H",
                # S1 Finding 4: `--full-history` -- plain history
                # simplification can prune THIS call's own commit from a
                # pathspec-limited `git log` when a merge lands in the
                # window, yielding a spurious zero-match that would
                # otherwise surface as a false commit failure (Finding 0).
                "--full-history",
                f"{pre_sha}..HEAD",
                "--",
                *log_grep_paths,
            ],
        )
        candidates = (
            [line for line in match_result.stdout.splitlines() if line]
            if match_result.ok
            else []
        )
        if len(candidates) != 1:
            # `git commit` already created this commit (`result.ok` above) --
            # only the token match came back zero-or-ambiguous. W1:
            # `landed=True` alongside `committed_sha=None`.
            return CommitOutcome(
                exit_code=1,
                landed=True,
                stderr=(
                    f"commit: landed but sha verification found {len(candidates)} "
                    f"candidate(s) matching this call's token trailer in {pre_sha}..HEAD -- "
                    "refusing to guess (never falling back to a bare rev-parse HEAD)"
                )[:200],
                unprovenanced_paths=unprovenanced_paths,
            )
        return CommitOutcome(
            exit_code=0,
            committed_sha=candidates[0],
            landed=True,
            unprovenanced_paths=unprovenanced_paths,
        )
    finally:
        try:
            msg_file.unlink()
        except OSError:
            print(f"skip: commit: msg_file.unlink() failed: {sys.exc_info()[1]}", file=sys.stderr)
            pass

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


def push_with_retry(
    worktree_root: Union[str, Path],
    *,
    allow_protected_branch: bool = False,
    protected_branch_override_reason: Optional[str] = None,
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

    for attempt in range(_PUSH_MAX_RETRIES):
        push_result = git_native.push(root)
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

        if not _is_push_reject(reason) or attempt == _PUSH_MAX_RETRIES - 1:
            break

        if upstream_info is None:
            last_reason = (
                f"git push: rejected and no upstream tracking ref resolvable ({reason})"
            )
            break

        fetch_result = git_native.fetch(root, upstream_info.remote_name)
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


# ---------------------------------------------------------------------------
# Orchestration: stage -> gates -> commit -> push
# (no longer "inside ceremony_lock" -- that mutex was deleted 2026-08-07,
# docs/plans/2026-08-07-excise-the-ceremony-lock.md)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PipelineResult:
    """Typed result of `run_commit_pipeline()` -- the full stage->commit->push pass.

    Outcome tri-state predicates (see individual fields):
        commit_failed -- True iff a gate failed OR the `git commit` step
            itself returned non-zero. The pipeline never reaches the push
            step when this is True.
        pushed -- see `derive_pushed_tristate`.
        integrity_breach -- True iff the commit LANDED locally but did NOT
            land on the remote (`pushed is False`) -- a state where the
            local ceremony invariant "every commit this op makes is
            pushed" no longer holds and a human/EM must resolve the
            divergence. A skipped push (`pushed is None`, no remote
            configured) is NOT a breach -- there is no remote invariant to
            violate. Widened to include `sha_unverified` (Review: staff-eng
            R2, state/review-trail/2026-08-08-landed-commit-close-review/
            r2-w2.md, finding 0): the predicate previously fired only when
            `committed_sha is not None`, which meant a landed-but-
            unverified commit whose push then failed reported
            `integrity_breach=False` -- a durable, UNPUSHED commit on a
            shared branch, whose sha nobody can name, is strictly WORSE
            than the named-sha breach this predicate exists to raise (there
            is no peer read anywhere in this pipeline or its consumers that
            ever back-fills `committed_sha` after the fact -- the prior
            docstring's claim that one does was false). The naming half of
            the old argument was right (there genuinely is no sha to name
            on the `sha_unverified` path) but the conclusion did not follow
            -- the breach is the divergence itself, not this pipeline's
            ability to spell it. Now True whenever the commit landed
            (`committed_sha is not None` OR `sha_unverified`) AND
            `push_status == "push-failed"` (C2, this plan) -- re-derived off
            `push_status` rather than `pushed is False`, because a
            branch-policy decline is also `pushed is False` under the old
            predicate and is emphatically NOT a breach: nothing was
            attempted that policy did not itself refuse. Only a genuine
            push failure raises this.
        sha_unverified -- True iff `git commit` landed a real commit but
            this pipeline could not resolve its sha (W2, same plan) --
            `commit_outcome.landed=True` with `commit_outcome.exit_code !=
            0`. `commit_failed` is False on this path (history changed;
            nothing here failed the caller's request) and the push still
            runs normally.
        push_status -- the fully-disambiguated companion to `pushed` (see
            the canonical-vocabulary comment above `PUSH_STATUS_PUSHED`):
            one of `"pushed"`, `"push-failed"`, `"declined"`, `"no-remote"`,
            `"not-attempted"`. Prefer this over `pushed` in new code --
            `pushed`'s `None` is shared by three distinct reasons (see
            `derive_pushed_tristate`'s docstring); `push_status` names which
            one applies without a second lookup.
        pushed_range -- (C3b, AC7) mirrors `PushOutcome.pushed_range` --
            the `<old-sha>..<new-sha>` range this pass actually pushed when
            `push_status == "pushed"`, else `None`. On a landed push, `None`
            is the explicit "could not resolve" sentinel (e.g. a first push
            with no upstream tracking ref yet), never a stand-in for "did
            not push".
        pushed_count -- (C3b, AC7) mirrors `PushOutcome.pushed_count`, same
            explicit-unknown rule as `pushed_range`.

    Fields (non-tri-state):
        committed_sha -- the landed commit's sha, as best this pipeline can
            still name it. Normally `commit_outcome.committed_sha`, captured
            BEFORE the push -- but `push_with_retry()` can fetch + `git
            rebase --onto` this very commit on a rejected push before
            re-pushing, which REWRITES its sha (same hazard as
            `consumed_handoff_stamp._commit_and_push_follow_up` and
            `post_commit_tail._commit_and_push_origin_stub_close` -- see
            those call sites' own Finding-1 comments). On the
            `push_status == "pushed"` landed-push path this pipeline
            re-resolves HEAD AFTER the push lands and reports that instead,
            falling back to the pre-push value only if the re-read itself
            fails -- never downgrading a known-good value to `None`. Every
            other outcome (decline, no-remote, not-attempted, a genuine push
            failure, or `sha_unverified=True`) reports the pre-push value
            unchanged, paying no second `rev-parse`.
    """

    stage: StageOutcome
    deletion_gate: Optional[GateOutcome]
    dirty_gate: Optional[DirtyTreeOutcome]
    carry_gate: Optional[GateOutcome]
    op_scope_gate: Optional[GateOutcome]
    commit: Optional[CommitOutcome]
    push: Optional[PushOutcome]
    committed_sha: Optional[str]
    pushed: Optional[bool]
    commit_failed: bool
    integrity_breach: bool
    sha_unverified: bool = False
    push_status: str = PUSH_STATUS_NOT_ATTEMPTED
    pushed_range: Optional[str] = None
    pushed_count: Optional[int] = None
    diagnostics: List[str] = field(default_factory=list)
    #: AC7 (docs/plans/2026-08-14-the-tool-stages-what-it-commits.md, C3) --
    #: mirrors `CommitOutcome.reason` verbatim when the commit step itself
    #: failed; "" on every other outcome (a gate failure, a benign no-op, or
    #: a landed commit). See that field's own docstring for the tag set.
    reason: str = ""
    #: AC4 -- mirrors `CommitOutcome.unprovenanced_paths` verbatim; empty
    #: tuple whenever `stage_patch` was never supplied to `run_commit_
    #: pipeline()`, or every named path was covered by the patch.
    unprovenanced_paths: Tuple[str, ...] = ()


def _resolve_pass_common_dir(cwd: str) -> Optional[Path]:
    """Resolve this worktree's git common dir ONCE per pipeline pass, through
    the resolver whose memo the rest of the pass reads.

    Purpose: two consumers in a single `run_commit_pipeline` pass need the
    same `git rev-parse --path-format=absolute --git-common-dir` answer --
    `_write_commit_message_tempfile` (msgfile dir) and the step-3 liveness
    gate (via the shared `sessions_dir` memo). (A third consumer,
    `ceremony_lock`'s own resolution, existed here until 2026-08-07,
    docs/plans/2026-08-07-excise-the-ceremony-lock.md deleted the lock
    entirely.) Two independent resolvers exist for that one fact, each with
    its own memo and its own deliberately-different failure contract:
    `lifecycle.git_common_dir` (raises `RuntimeError`, keyed on a `Path`) and
    `session.core.sessions_dir` (returns `""`, keyed on a `cwd` STRING) --
    see the latter's docstring for why the two stay separate. Neither memo
    can serve the other, so a pass touching both paid the spawn TWICE.

    Resolving here through `session.core.sessions_dir(cwd)` -- which cannot
    be primed from the outside -- collapses that to one spawn: the derived common dir is
    threaded explicitly into the one `lifecycle.git_common_dir` consumer
    (`_write_commit_message_tempfile`, which already accepts a pre-resolved
    value), and the liveness read is served from this call's own memo entry.
    The derivation is exact, not approximate: `sessions_dir` is defined as
    `<git-common-dir>/coordinator-sessions` over the identical git
    invocation, so `.parent` recovers `lifecycle.git_common_dir`'s value
    byte-for-byte.

    Returns None when the hub cannot be resolved (not a git repo, git
    missing, transient spawn failure -- `sessions_dir` reports all three as
    `""`, and a failure is deliberately NOT memoized there). None means "no
    pre-resolved value": every consumer then falls back to exactly the
    resolution it performed before this dedup existed. NOTHING about message
    content or commit outcome may ever branch on whether this returned a
    value -- it is a cost optimisation only.
    """
    hub = session_hub_dir(cwd)
    if not hub:
        return None
    return Path(hub).parent


def _preflight_reap_stale_lock(worktree_root: str) -> None:
    """Best-effort orphaned-`.git/index.lock` self-heal, run ONCE per commit
    ceremony at `run_commit_pipeline()`'s own entry -- before staging, gates,
    or commit ever run.

    state/bug-backlog/2026-08-12-scoped-git-commit-is-not-a-raw-git-invoc-
    f4fff3a626fa.yaml (P1): the CLI wrapper `coordinator/bin/scoped-git-
    commit` already carries this exact pre-flight (`_preflight_reap_stale_
    lock`, verbatim shape) for callers that invoke it directly -- but
    `ops/session/safe_commit_offer.py::_commit_group` resolves the op
    IN-PROCESS and never goes through that CLI at all, so it reached this
    pipeline with no self-heal of its own. `guard_reap_stale_git_lock`'s
    PreToolUse guard only recognizes a bare `git` in command position, so it
    can never cover an op-form caller either -- the pipeline has to
    self-heal for the same reason the CLI does.

    Belt-and-braces, INTENTIONALLY: the CLI keeps its own copy of this
    pre-flight rather than delegating here, because it serves callers that
    never reach this op-level pipeline at all (a direct subprocess
    invocation of the CLI outside any op route). The reaper itself
    (`coordinator_core.ops.reap_stale_locks`) is idempotent -- a lock already
    reaped by the CLI's own pre-flight costs this call nothing but the cheap
    `os.path.exists` check below finding no lock. Do NOT "de-duplicate" the
    two call sites; they serve disjoint caller populations.

    Ceremony-level, NOT per-`git` subprocess -- called exactly once, here,
    for the whole stage -> gate -> commit -> [push] critical section this
    function runs. This is a distinct mechanism from the per-invocation
    retry a peer chunk (C1, `git_native.py`) owns for individual `git`
    calls inside that same section; the two must never be conflated -- this
    function never retries, and the per-invocation retry never reaps a
    lock.

    Cheap by construction: the overwhelming common case is "no lock at
    all", and that case costs exactly one `os.path.exists`/`os.path.isdir`
    stat, no subprocess. The reaper (and its own `git rev-parse`
    resolution, age/stability re-sample, and exit-code ladder) is only
    invoked once a lock file is actually present at the cheap-checked
    candidate path.

    Negative-spec:
      - Never treats a reaper failure as a commit failure. ANY exception --
        import failure, subprocess failure, permission error, whatever --
        is swallowed here; the pipeline proceeds exactly as if this
        function had never been called. Fail-open is the whole point: a
        reaper defect must never block a commit that would otherwise
        succeed.
      - Never second-guesses or bypasses the reaper's own age/stability
        gate. `reap_stale_locks.main()`'s exit code 2 (a FRESH
        `index.lock` -- a live commit may genuinely be in progress) is NOT
        reaped, and is treated identically to exit 0 here: the pipeline
        proceeds to its normal git calls either way, and git itself
        reports if it is genuinely blocked. That gate is the only thing
        standing between this and corrupting a peer's in-flight commit on
        a shared tree.
      - Never reimplements `do_reap`/`stale_and_stable`'s lock-selection or
        removal logic here -- the whole-repo sweep in
        `reap_stale_locks.main()` is called as-is, so `index.lock`,
        `next-index-*.lock`, and `objects/maintenance.lock` share one
        reaper implementation regardless of caller.
      - Never assumes `<worktree_root>/.git` is a directory. In a linked
        `git worktree` or a submodule it is a FILE (a `gitdir: <path>`
        pointer to the real git dir elsewhere), so
        `<worktree_root>/.git/index.lock` can never exist there and a
        directory-only gate would silently never fire. This function does
        NOT parse that pointer file itself (that is git-dir-discovery
        reimplementation, reserved to the reaper); it pays one
        `os.path.isdir` call to tell directory from file, and for the file
        case delegates entirely
        to `reap_stale_locks.main()`, which already resolves the real git
        dir correctly via `--absolute-git-dir` / `--git-common-dir`.

    Thin trampoline over the shared leaf policy,
    `coordinator_core.lock_preflight.preflight_reap_stale_lock` — see that
    module's docstring for the full contract, negative-spec, and why BOTH
    this pipeline and `coordinator/bin/scoped-git-commit` call it
    (deliberately, not duplicatively).

    Contract pinned by `coordinator/bin/tests/test_scoped_git_commit_lock_
    reap.py` and `coordinator_core/tests/test_lock_preflight.py` (same four
    cases): (1) a stale, stable lock is reaped; (2) a fresh lock is left
    alone (exit 2 is "do not reap", not a failure); (3) a reaper exception
    never propagates; (4) no lock present costs no subprocess at all.

    AC3/AC5 (docs/plans/2026-08-13-claim-release-deadlock-and-the-doctrine-
    that-rejects-it.md, C3b): the reference model the plan's duration test
    is measured against -- REMOVED FROM AUDIT SCOPE, behaviour unchanged.
    What it protects that git does not: `git commit`/`git add` do not
    self-heal a genuinely orphaned `index.lock` (a lock left behind by a
    killed process) -- left alone, every subsequent git call on that
    worktree fails until something removes the file. Why it passes the
    duration test: the age/stability gate inside `reap_stale_locks` is the
    only thing distinguishing "orphaned" from "a peer's commit genuinely
    in progress right now", so a FRESH lock is never reaped (exit 2, left
    alone, this function returns immediately) -- the only work done here
    is milliseconds of stat/reaper-predicate cost, no wait, no retry loop,
    invisible in normal operation. Its outlet needs no human either way: a
    stale lock is reaped inline before the caller's own commit proceeds; a
    fresh lock is left for git's own subprocess to report as blocked, same
    as if this pre-flight did not exist.
    """
    from coordinator_core.lock_preflight import preflight_reap_stale_lock

    preflight_reap_stale_lock(worktree_root)


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


#: In-plane terminal-handoff-sweep cap (C4, docs/plans/2026-08-25-the-
#: terminal-handoff-sweep-stops-being-an-op.md § C4) -- a cited literal
#: mirroring `archive_terminal_handoffs._RECOMMENDED_CAP_CHOICE`, the SAME
#: recommendation that module's own docstring makes for any caller of
#: `plan_sweep`. `plan_sweep`'s own `cap` param is required with no
#: unbounded default (C0's binding cap-axis decision) -- this call site's own
#: choice of that value, not a second computation of it.
#:
#: Read through a function, not bound as a module-level constant: binding it
#: eagerly is what forced `archive_terminal_handoffs` (and, through it,
#: `asyncio`) to import on EVERY commit, whether or not the cadence gate below
#: was even open. Measured 2026-08-26: asyncio ~29ms and the fleet module
#: ~13ms of a ~88ms cold `import commit_pipeline`, on a path that awaits
#: nothing unless the sweep actually runs.
def _archive_sweep_cap() -> int:
    from coordinator_core.ops.fleet import archive_terminal_handoffs

    return archive_terminal_handoffs._RECOMMENDED_CAP_CHOICE

#: How often the in-plane sweep is allowed to do corpus work, in seconds.
#: The occasion is every ceremony commit; the JOB is due far less often than
#: that. 15 minutes is chosen against what the sweep is for, not against a
#: measurement: archiving a terminal handoff has no latency requirement at all
#: -- nothing reads state/handoffs/ expecting a record to have already left --
#: so the only cost of waiting is that the record sits one interval longer.
#: The cost of NOT waiting is a corpus-sized classification pass on every
#: commit across ~50 concurrent sessions.
#: Lower it only with a named consumer that needs fresher archival than this.
_ARCHIVE_SWEEP_INTERVAL_S: float = 15 * 60.0


def _archive_sweep_marker(common_dir: Path) -> Path:
    """Machine-local cadence marker for the in-plane sweep.

    Under <common_dir>/coordinator-sessions/ -- the same git-common-dir-rooted
    location as this sweep's own single-flight lock
    (`archive_terminal_handoffs._sweep_lock_path`) and the claim-dir
    convention, so a linked-worktree caller reads the same marker as the main
    worktree.

    NOT `housekeeping_liveness`, and the reason is load-bearing rather than
    stylistic: that store's record lives under `state/`, and this fires on the
    commit hot path. A hot-path write into the worktree leaves an untracked
    file behind every ceremony commit -- caught here by seven
    `_porcelain(repo) == []` assertions in test_commit_pipeline.py, which are
    right. It is gitignored in THIS checkout, which is a property of this
    repo's .gitignore and not of the mechanism. Under `.git/` the question
    does not arise.

    mtime IS the timestamp: no JSON, no format to parse, no unparseable-value
    arm to degrade through.
    """
    return common_dir / "coordinator-sessions" / "archive-terminal-handoffs.cadence"


def _archive_sweep_due(common_dir: Path, interval_s: float) -> bool:
    """Is the in-plane sweep due to do corpus work again?

    DEGRADES OPEN: a missing or unreadable marker returns True, and so does a
    marker stamped in the future (a clock step, or a checkout carrying someone
    else's mtime -- never a reason to stop archiving until it catches up). A
    false True costs one extra run of the sweep; a false False disables
    archival silently and indefinitely, which is the failure this repo has
    just spent a whole plan on.
    """
    try:
        age_s = time.time() - _archive_sweep_marker(common_dir).stat().st_mtime
    except OSError:
        return True
    return age_s >= interval_s or age_s < 0


def _stamp_archive_sweep(common_dir: Path) -> None:
    """Record that the sweep just did its corpus pass.

    Best-effort: a failed stamp means the next ceremony commit sweeps again,
    which is the safe direction to fail in.
    """
    marker = _archive_sweep_marker(common_dir)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        os.utime(marker, None)
    except OSError:
        pass


def _run_in_plane_archive_sweep(
    worktree_root: Path, common_dir: Optional[Path]
) -> Tuple[List[str], List[str]]:
    """Classify + apply one terminal-handoff archival sweep IN-PROCESS, on the
    ceremony's own commit hot path (C4, docs/plans/2026-08-25-the-terminal-
    handoff-sweep-stops-being-an-op.md § C4 -- replaces the killed
    `tail_ops.fire_archive_sweeps_detached`).

    Composes `archive_terminal_handoffs.plan_sweep` (classification, every
    exclusion rail -- live-claim, `shipped_in` resolvability, childlessness,
    dest-conflict, worktree-dirty) and `.apply_sweep` (`os.replace` only, ZERO
    git spawns) -- never re-implements either. Guarded by that module's own
    `_acquire_sweep_lock`/`_release_sweep_lock` single-flight rail so this
    leg and `fleet.archive_completed_handoffs`'s own act path never race the
    same `os.replace` targets; a contended lock is a first-class non-error
    skip (empty result), same as that op's own handler treats it, never a
    raised exception.

    Returns `(srcs, dsts)` -- repo-relative path lists, ONE entry per
    successfully-applied move, in the SAME order -- for the caller to union
    into `commit_paths` alongside `stage.swept_renames`'s own src/dst pair
    (see `run_commit_pipeline`'s call site). A move `apply_sweep` reports as
    `failed` contributes to NEITHER list -- its src still exists at the old
    path and its dst was never created, so naming either half here would
    hand `commit_scoped()` a pathspec entry with nothing real behind it.

    CADENCE-GATED, not per-commit. The OCCASION is every ceremony commit; the
    JOB is due every `_ARCHIVE_SWEEP_INTERVAL_S`. When the gate is closed this
    returns `([], [])` having read nothing at all -- see the gate's own comment
    for the measurement that motivated it. When it is open the sweep rides the
    ceremony commit exactly as before, so "zero additional processes, zero
    additional commits" is unchanged; what changed is how often the corpus work
    happens, never how its results are delivered.

    Non-fatal contract (module docstring "HARD CONSTRAINT" precedent,
    inherited from `archive_and_commit`): NEVER raises. `common_dir is None`
    (the pass's own git-common-dir resolution failed -- see
    `_resolve_pass_common_dir`) and any exception from `plan_sweep`/
    `apply_sweep` themselves both degrade to `([], [])` -- an archival
    failure must never flip the ceremony's exit code, and a failed sweep
    contributes no paths, leaving `commit_paths` exactly what it would have
    been without this call.

    Negative-spec:
      - Does NOT spawn git -- `plan_sweep`'s own rails spawn `git status
        --porcelain` (worktree-dirty) and `git cat-file --batch`
        (`shipped_in`); `apply_sweep` spawns nothing. Neither is a NEW spawn
        this call site adds -- both already run on the standalone op's own
        act path (AC-3's "zero ADDITIONAL git processes", not zero total).
      - Does NOT commit anything itself -- the caller folds the returned
        paths into its own `commit_paths` and lands them on the SAME commit
        it was already making (AC-3/AC-10).
      - Does NOT re-implement `_common.py :: archive_and_commit` -- that
        helper is untouched (AC-6) and stays the standalone op's own commit
        mechanism, never this leg's.
    """
    if common_dir is None:
        return [], []

    # `plan_sweep` walks ABSOLUTE handoff paths and ids them against this root
    # via `wire_paths.rel_id`, whose `relative_to` raises for a relative root.
    # The raise is caught below, so a caller passing `.` loses the whole sweep
    # to a logged warning while its commit still reports success -- the failure
    # shape this leg exists to avoid. Resolving here costs nothing for the
    # absolute callers and is a no-op when already resolved. Resolved BEFORE
    # the cadence gate too: `liveness_path` rejects a relative root, and that
    # rejection degrades the gate open, so a relative caller would silently
    # keep paying the per-commit cost the gate exists to stop.
    worktree_root = Path(worktree_root).resolve()

    # Cadence gate, BEFORE any corpus work. `plan_sweep` costs what it costs
    # whether or not there is anything to archive: it reads frontmatter for
    # EVERY live handoff, builds the reverse-edge index over all of them,
    # resolves live session ids, and probes a claim dir per candidate.
    # Measured 2026-08-26 on a 237-record corpus: p50 31.2ms CPU / 79.0ms wall,
    # max 78.1ms / 125.4ms -- and 0 records archived, because the cost scales
    # with the corpus and the outcome scales with what is terminal. Every
    # ceremony commit reached this line unconditionally, so the pass ran per
    # commit for a job that is due per interval. AC-3/AC-7 could not see it:
    # both measure processes and spawns ADDED, and this adds neither.
    #
    # The stamp is written after the sweep actually runs (below), never here --
    # a gate that opens must not record a run that has not happened yet.
    if not _archive_sweep_due(common_dir, _ARCHIVE_SWEEP_INTERVAL_S):
        return [], []

    # Deferred PAST the cadence gate, deliberately: `plan_sweep` is a coroutine,
    # so reaching it at module scope pulled `asyncio` (and its `ssl`/`socket`
    # subtree) plus the fleet module into every commit -- ~42ms of a ~88ms cold
    # import, for a job that runs once per `_ARCHIVE_SWEEP_INTERVAL_S`. Below
    # the gate, the interpreter pays it only on the commits that sweep.
    import asyncio

    from coordinator_core.ops.fleet import archive_terminal_handoffs

    lock_path = archive_terminal_handoffs._acquire_sweep_lock(common_dir)
    if lock_path is None:
        # Contended -- another instance (the standalone op's act path, or a
        # concurrent ceremony) holds the sweep right now. First-class
        # non-error skip, retained for the next sweep -- never an error that
        # flips this ceremony's exit code.
        return [], []

    try:
        try:
            moves, _skipped = asyncio.run(
                archive_terminal_handoffs.plan_sweep(
                    worktree_root, common_dir, _archive_sweep_cap()
                )
            )
        except Exception:
            _LOG.warning(
                "run_commit_pipeline: in-plane archive-sweep plan_sweep failed "
                "-- contributing no paths (non-fatal)", exc_info=True,
            )
            return [], []

        # The classification pass ran to completion -- that IS the run this
        # cadence gates, whether or not it found anything to move. Stamping
        # only on a non-empty result would reopen the gate on every commit for
        # exactly the corpus this sweep currently produces (0 moves against 237
        # records), which is the case the gate exists for.
        _stamp_archive_sweep(common_dir)

        if not moves:
            return [], []

        try:
            acted, _failed = archive_terminal_handoffs.apply_sweep(moves)
        except Exception:
            _LOG.warning(
                "run_commit_pipeline: in-plane archive-sweep apply_sweep failed "
                "-- contributing no paths (non-fatal)", exc_info=True,
            )
            return [], []

        acted_ids = {a["id"] for a in acted if a.get("archived")}
        srcs: List[str] = []
        dsts: List[str] = []
        for move in moves:
            if move.candidate_id not in acted_ids:
                continue
            srcs.append(_archive_sweep_rel_id(move.src, worktree_root))
            dsts.append(_archive_sweep_rel_id(move.dst, worktree_root))
        return srcs, dsts
    finally:
        archive_terminal_handoffs._release_sweep_lock(lock_path)


def run_commit_pipeline(
    worktree_root: Union[str, Path],
    *,
    session_id: str,
    subject: str,
    prose: str = "",
    deleted_paths: Sequence[str] = (),
    kept_entries: Sequence[str] = (),
    trailers: str = "",
    stage_paths: Sequence[str] = (),
    caller_paths: Optional[Set[str]] = None,
    on_committed: Optional[Callable[[str], None]] = None,
    push_mode: str = PUSH_MODE_SYNC,
    allow_protected_branch: bool = False,
    protected_branch_override_reason: Optional[str] = None,
    deliverable_id: Optional[str] = None,
    stage_patch: Optional[str] = None,
    attributed_session_id: Optional[str] = None,
) -> PipelineResult:
    """Run the full stage -> gate -> commit -> [push] critical section.

    `allow_protected_branch` (C4a, AC8/AC15) -- keyword-only, per-call
    override threaded straight to `push_with_retry()`'s own argument of the
    same name; default `False` leaves every existing caller's push
    behaviour unchanged (none in this repo passes `True`). When `True`, a
    non-`work/*` branch that `auto_push.branch_gate()` would otherwise
    decline is pushed anyway, and the decision is never silent --
    `push_with_retry()` prints an `"override-exercised"` line (C3/AC14)
    naming the branch and `protected_branch_override_reason`. Never an env
    var or a module-level flag (see the plan's Anti-scope) -- the ONE
    sanctioned consumer of this argument, as of this chunk, is
    DoE-claude's `merging-to-main` SKILL (`coordinator/skills/merging-to-
    main/SKILL.md`), Step 10 ("Completion-Log Status Flip") item 5: the
    post-merge, on-`main`, release-notes bookkeeping commit that runs
    AFTER a PR has already merged via `gh pr merge` -- never the work
    itself, which lands via a PR and must never push `main` directly. No
    op in this repo has a sanctioned reason to pass it; do not add one
    "just in case".

    `protected_branch_override_reason` -- the caller-supplied justification
    printed verbatim in the override-exercised line. Optional; `None`
    prints the line without a reason clause rather than failing the call.

    `deliverable_id` (C7b) -- optional, passed straight through to `commit()`
    (and from there to `git_native.commit_scoped()`); `None` by default,
    unchanged behaviour for every existing caller. Not sourced or validated
    here -- the caller must already hold a provenance-bearing id.

    `attributed_session_id` (state/bug-backlog/2026-08-18-scoped-git-commit-
    stamps-a-foreign-session-id-8d21f0c4e7b9.yaml) -- optional, passed
    straight through to `commit()` (and from there to `git_native.
    commit_scoped()`'s own parameter of the same name): the CALLER's own
    already-resolved committing-session identity, authoritative for the
    `Session-Id:` trailer. Deliberately its OWN parameter, never folded into
    the required `session_id` above -- that one is UNREAD across this
    function's entire body (W3, docs/plans/2026-08-08-a-landed-commit-
    reported-as-failed.md, item 4; `scoped_git_commit.py` still mints it as
    a private per-invocation nonce for a reason unrelated to attribution --
    see that call site's own comment) and repurposing a dead parameter here
    is out of this fix's scope. `None` (the default) leaves every existing
    caller's `Session-Id:` resolution unchanged.

    `stage_patch` (C3, docs/plans/2026-08-14-the-tool-stages-what-it-commits.md)
    -- optional path to a patch file, passed straight through to `commit()`.
    `None` by default, unchanged behaviour for every existing caller. When
    given, this pass NEVER stages `stage_paths` via `explicit_stage()`'s
    ordinary `git add` -- see `explicit_stage`'s own negative-spec ("never
    `git add`s a supplied-blob path"): a supplied blob's provenance comes
    from `commit()`'s own process-private `stage_from_patch()` apply,
    immediately before the commit step, and touching the SHARED index for
    any named path first would be exactly the incident class this plan
    exists to close (a peer's ordinary `git add`/`git commit` absorbing
    content that was never theirs). A synthetic `StageOutcome` reports every
    named path as already staged (nothing left for the gate/commit-paths
    derivation to do) without spawning `git add` at all -- a path the patch
    does not cover still lands via `git_native._commit_scoped_private_
    index()`'s own worktree-read for that path (AC4), never via this
    pipeline's shared-index staging.

    Purpose: the C4 orchestration entry point. Used to acquire `ceremony_lock`
    for the duration of the entire critical section -- that mutex was deleted
    2026-08-07 (docs/plans/2026-08-07-excise-the-ceremony-lock.md; see that
    plan for the safety argument covering the two residual unserialized
    windows it left, C10's divergence dedup and C11's sha capture, and S2
    Findings 3/4 for two further unserialized windows the plan's own
    enumeration does not name). In `push_mode="sync"` (default --
    `scoped_git_commit.py`'s untouched wire contract, DEC-1/F1), this
    function's own critical section spans stage through push-with-retry,
    exactly as before the lock's removal. In
    `push_mode="deferred"|"none"|"never"`, this section spans ONLY stage ->
    gates -> commit -- `push_with_retry()` is skipped entirely, `pushed` is
    always `None`, and `integrity_breach` is always `False` (there is no
    synchronous push outcome to breach against; see `wsc_tail.py`'s
    deferred-push design, DEC-1).

    `"never"` differs from `"deferred"|"none"` only outside this function's
    own push leg, and the difference is the whole point of it: it ALSO
    stands the `post-commit` hook's detached push down, so the commit is
    published by nobody rather than by the hook. A caller that must end at a
    local commit wants `"never"`; a caller that will push later itself wants
    `"deferred"`/`"none"`. See `PUSH_MODE_NEVER`.

    Sequence:
      0. `_resolve_pass_common_dir(str(root))` -- the pass's ONE git-common-dir
         resolution, taken up front and threaded into every consumer that
         would otherwise re-derive it (`commit()`'s msgfile writer, and --
         via the shared `sessions_dir` memo -- the step-3 liveness gate; a
         third consumer, `ceremony_lock`'s own resolution, existed here
         before the 2026-08-07 excision). See that helper for why one
         resolver cannot simply serve the other's memo. Cost-only: `None`
         (unresolvable) puts every consumer back on its own pre-existing
         resolution path.
      1. `explicit_stage(stage_paths, caller_paths)` -- tolerant staging.
      2. `gate_paths = compute_gate_paths(stage.staged_paths, deleted_paths)`;
         `commit_paths = compute_commit_paths(gate_paths, swept_srcs,
         swept_dsts)` from `stage.swept_renames` -- the full explicit
         pathspec (AC5). An empty `commit_paths` short-circuits HERE, before
         either C3 gate runs (2026-07-22 correction: moved above the gates --
         there is no message to validate and nothing to scope the dirty-tree
         gate to when there is nothing to commit; `commit_failed=False`, a
         benign no-op).
      3. `compose_message(...)` via C2, using caller-supplied `trailers`
         verbatim, and using
         `gate_paths`'s Deleted/Kept claims already supplied by the caller
         (`deleted_paths`/`kept_entries` are the SOURCE of the message
         blocks; `gate_paths` is the scope the deletion-block gate inspects
         them against).
      4. `deletion_block_gate(message, gate_paths, cwd)` + `dirty_tree_gate
         (worktree_root, gate_paths)` -- both from C3, both scoped to the
         SAME `gate_paths` (2026-07-22: dirty_tree_gate gained the same
         `gate_paths` scoping deletion_block_gate already had, so an
         unattributable dirty path outside the caller's own pathspec -- a
         live peer session's file on a shared branch, routine on a
         concurrent-EM tree -- never trips this gate; see commit_gates.py's
         module docstring for the incident that motivated it). This call
         ALWAYS passes a list (possibly empty), never `None` -- so this
         caller always gets `dirty_tree_gate`'s SCOPED behaviour, including
         the degenerate "scoped to nothing" case when `gate_paths == []`
         (step 2 short-circuits most of those, but not the swept-renames-
         only shape, where `commit_paths` is non-empty via swept_srcs/
         swept_dsts alone while `gate_paths` itself is still `[]`). See
         `commit_gates.dirty_tree_gate`'s own docstring for the
         `None`-vs-`[]` sentinel distinction this relies on. A gate failure
         short-circuits before any commit is attempted (`commit_failed=True`,
         `commit=None`, `push=None`).
      5. `commit(message, commit_paths)` -- writes temp msgfile, commits,
         captures `committed_sha`. Commit is a THREE-way outcome, not
         binary (W2, docs/plans/2026-08-08-a-landed-commit-reported-as-
         failed.md): (a) genuine failure / the ordinary already-committed
         no-op (`commit_outcome.landed=False`) -- `commit_failed=True`,
         `push` never runs; (b) an ordinary successful commit with a
         resolved sha -- `commit_failed=False`, `committed_sha` set; (c)
         `commit_outcome.landed=True` but its sha could not be verified --
         `commit_failed=False`, `committed_sha=None`,
         `sha_unverified=True`, and the push still runs (case (c) is
         collapsed with case (a)'s `exit_code != 0` at the `git`
         subprocess level, but NOT at this pipeline's own outcome level --
         see the `commit_outcome.landed` branch inline). On a successful
         commit with a resolved sha, `on_committed` (when supplied) is
         invoked with the real `committed_sha` BEFORE `push_with_retry()`
         runs (step 6) -- this is the AC18 crash-resumption hook: the
         caller (`wsc_tail.py`) uses it to persist the commit sentinel the
         instant the commit has landed, so a crash during the push-with-
         retry network round-trip (fetch/rebase/re-push -- the most
         crash-exposed sub-window in the pipeline) is still covered by
         AC18's "resume from stamp step, never double-commit" guarantee.
         Never called on a failed/short-circuited commit, and (deliberately,
         not accidentally -- see the guard's own inline comment) never
         called on case (c) either, since there is no real sha to persist.
         Any exception `on_committed` itself raises propagates -- the
         sentinel write is intentionally NOT best-effort (a silently-failed
         sentinel write would silently reopen the exact duplicate-commit
         gap this hook exists to close).
      6. `push_with_retry()` -- when the commit landed (cases (b) and (c)
         above) AND `push_mode="sync"`. Skipped entirely for
         `push_mode="deferred"|"none"` -- see the DEC-1 paragraph above.

    A `StageOutcome.exit_code == 2` (missing-caller anomaly) does NOT by
    itself set `commit_failed` -- it is a degraded-but-not-failed signal the
    caller (C9's orchestrator) surfaces separately; a genuine `StageOutcome.
    failed` (non-empty) DOES set `commit_failed`, since nothing was staged.

    Spec backlink: pln-rebuild-the-wsc-commit-ceremon-f7c2a0 § C9
    (AC18 review Finding 2 -- `on_committed` closes the narrower-than-claimed
    crash-resumption window: previously the sentinel was only written by the
    caller AFTER this whole function returned, which meant a crash during
    `push_with_retry()` left the sentinel at its empty pre-commit placeholder
    and a re-invocation re-ran the entire pipeline, producing a duplicate
    commit).
    """
    root = _require_worktree_root(worktree_root)
    diagnostics: List[str] = []
    # C2 span bookkeeping (see `_COMPOSITION_SPAN_PRE_PUSH`/`_COMPOSITION_SPAN_
    # PUSH` above): one `composition_id` shared by both legs of THIS call, so
    # a reader can join the pre-push and push rows for the same invocation.
    # `_pipeline_t_start` is this call's own wall-clock start -- the pre-push
    # span's `t_start` and the base its elapsed_secs is measured from.
    _pipeline_t_start = time.time()
    _composition_id = uuid.uuid4().hex
    _span_sid = attributed_session_id if attributed_session_id is not None else session_id
    _preflight_reap_stale_lock(str(root))
    common_dir = _resolve_pass_common_dir(str(root))

    # Pre-stage directory-pathspec guard (session fb5fa766, 2026-07-31
    # incident): `commit_scoped()` (git_native.py) already refuses a
    # directory pathspec, but only AFTER `explicit_stage()` below has
    # already run `git add -- <dir>/` -- staging everything currently
    # inside it. The refusal then reaches the caller correctly, but the
    # staged residue survives it (this pipeline's own post-stage
    # rollback deliberately drops directory entries from `reset_paths()`
    # -- see that function's docstring -- so nothing cleans it up). The
    # REPORTED incident shape was exactly this: an EM passing a
    # directory pathspec (`state/subagent-share/<session-id>/`) into
    # this pipeline. Refusing here, before `explicit_stage()` ever runs,
    # means no `git add` for the offending batch ever happens, so there
    # is no residue to roll back. Reuses `commit_scoped()`'s own
    # predicate/wording (`git_native.directory_pathspecs()` /
    # `directory_pathspec_diagnostic()`) rather than forking a second
    # notion of "is a directory pathspec" -- `commit_scoped()`'s own
    # check stays in place unchanged as the load-bearing guard for
    # direct callers that never go through this pipeline; this is
    # defence in depth, not a replacement. Checked against the FULL
    # `stage_paths` batch (not just the caller-flagged subset) so a
    # mixed batch -- one real file plus one directory -- refuses as a
    # whole and stages neither.
    # AC3/AC11 (C3b): what this protects THAT GIT DOES NOT -- `git add
    # <dir>/` has no concept of "residue that survives a later refusal";
    # it stages everything under the directory unconditionally, and only
    # `commit_scoped()` downstream refuses the directory pathspec itself,
    # by which point the staged residue is already there with nothing
    # left to clean it up (the incident this guard closes, above).
    # Outlet, no human: the returned diagnostics name the offending
    # pathspec directly -- the caller re-issues the SAME call with
    # explicit file paths instead of the directory, a single-request
    # reject, never a hold.
    dir_paths = git_native.directory_pathspecs(root, stage_paths)
    if dir_paths:
        pre_stage_diagnostics = [
            f"run_commit_pipeline: pre-stage guard: {git_native.directory_pathspec_diagnostic(p)}"
            for p in dir_paths
        ]
        diagnostics.extend(pre_stage_diagnostics)
        return PipelineResult(
            stage=StageOutcome(exit_code=-1, failed=list(pre_stage_diagnostics)),
            deletion_gate=None,
            dirty_gate=None,
            carry_gate=None,
            op_scope_gate=None,
            commit=None,
            push=None,
            committed_sha=None,
            pushed=False,
            commit_failed=True,
            integrity_breach=False,
            push_status=PUSH_STATUS_NOT_ATTEMPTED,
            diagnostics=diagnostics,
        )

    # Rollback bookkeeping (session fb5fa766, 2026-07-31 incident, widened
    # 2026-07-31 per code-reviewer Finding 1): `staged_this_call` starts
    # at the safe empty default and the `try` wraps `explicit_stage()`
    # itself, not just the post-stage steps -- `explicit_stage()`'s own
    # `git add -- <to_stage>` (via `git_native.add_paths`) is a single
    # batched subprocess covering potentially many paths, and on a
    # genuine failure `StageOutcome.acted` defaults to `[]` regardless of
    # whether git partially staged some of the batch before erroring
    # (see `explicit_stage()`'s failure-branch return). Wrapping only
    # AFTER the `stage.failed` early-return -- as this looked prior to
    # the widening -- left that partial-add-then-fail residue outside
    # the rollback entirely, reproducing the exact "staged-and-abandoned"
    # shape this commit exists to close, just behind a rarer trigger
    # (see `test_stage_add_paths_partial_failure_residue_is_reconciled_into_acted`
    # for the empirical atomicity check this assumption now rests on,
    # not just faith).
    #
    # Every post-stage exit below this point -- a gate failure, a
    # non-zero commit subprocess (INCLUDING the ordinary
    # already-committed `exit_code == 1` empty-commit-set no-op, which
    # must roll back silently, never loudly), or an interrupted/killed
    # process (the SessionEnd hook runs under a timeout) -- must not
    # leave `staged_this_call` sitting at index state `A ` for the next
    # bare `git commit` on this shared branch to absorb. `landed` flips
    # True only at the two points nothing further needs undoing: the
    # benign nothing-to-commit short-circuit (nothing of this call's own
    # staging survives uncommitted -- see that branch) and immediately
    # after a successful `commit()` (the staged content is now part of
    # history, not index-only residue). Scoped to exactly `stage.acted`
    # -- the paths THIS call's own `git add` touched, never
    # `stage.staged_paths` (which also covers an already-staged diverged
    # path this call deliberately left untouched) and never a
    # bare/directory pathspec -- so a peer EM's own concurrently-staged
    # work outside this set is never touched by the rollback.
    staged_this_call: List[str] = []
    #: Paths whose committed content comes from `stage_patch`'s private
    #: index rather than the worktree -- see the `dirty_tree_gate` scoping
    #: note below for why that distinction is load-bearing. Empty whenever
    #: `stage_patch` is None (keeping every pre-stage-patch caller's gate
    #: scope byte-identical) and also empty-but-still-safe whenever
    #: `stage_patch` is supplied but its patch touches none of
    #: `stage_paths` -- same effect as the None case, computed rather than
    #: special-cased.
    patch_covered: List[str] = []
    landed = False
    try:
        if stage_patch is not None:
            # C3 (see this function's own docstring, `stage_patch`): never
            # `git add`s a path the patch itself will cover onto the SHARED
            # index -- `commit()` (below) is the sole place a covered path's
            # content is touched, and it only ever touches its OWN process-
            # private index for that path. AC4 (additive per-path): a
            # NAMED path the patch does NOT cover still needs an ordinary
            # `git add` here, same as any other invocation -- without it,
            # the dirty-tree gate below cannot attribute that path's own
            # worktree edit to this call (it would see a dirty, un-staged
            # path outside this call's own staged set and refuse the whole
            # commit). `patch_touched_paths()` answers "which of
            # `stage_paths` will the patch cover" by parsing the patch text
            # ONLY (`git apply --numstat`, no repo mutation, no read of
            # index/worktree/HEAD) -- a cheap pre-check, never the
            # authority on what actually lands (that is `stage_from_patch()`
            # itself, inside `commit()`, immediately before the commit
            # step -- no probe-then-act gap on the supplied-blob content
            # itself, only on this staging-split decision, which never
            # answers a divergence question the plan's anti-scope guards).
            patch_touched = git_native.patch_touched_paths(stage_patch, root)
            # Review: coordinator:code-reviewer (88f5accd, finding 3) -- reuse
            # git_native._normalize_path_key instead of inlining its logic,
            # so a future change to that key's normalization (e.g. added
            # case-folding) cannot silently drift the two copies apart --
            # path-key drift has already caused a live incident here (a
            # C-quoted path never matching its key, silently exempting a
            # guard).
            patch_covered = [
                p for p in stage_paths if git_native._normalize_path_key(p) in patch_touched
            ]
            remainder = [p for p in stage_paths if p not in patch_covered]
            remainder_stage = (
                explicit_stage(root, remainder, caller_paths)
                if remainder
                else StageOutcome(exit_code=0)
            )
            stage = StageOutcome(
                exit_code=remainder_stage.exit_code,
                acted=remainder_stage.acted,
                skipped=list(remainder_stage.skipped)
                + [f"stage-patch-covered:{p}" for p in patch_covered],
                failed=remainder_stage.failed,
                checked_paths=remainder_stage.checked_paths,
                diverged_paths=remainder_stage.diverged_paths,
                staged_paths=list(remainder_stage.staged_paths) + patch_covered,
                swept_renames=remainder_stage.swept_renames,
                deletion_paths=remainder_stage.deletion_paths,
                missing_caller_paths=remainder_stage.missing_caller_paths,
                ignored_caller_paths=remainder_stage.ignored_caller_paths,
            )
        else:
            stage = explicit_stage(root, stage_paths, caller_paths)
        staged_this_call = list(stage.acted)

        if stage.failed:
            diagnostics.extend(stage.failed)
            return PipelineResult(
                stage=stage,
                deletion_gate=None,
                dirty_gate=None,
                carry_gate=None,
                op_scope_gate=None,
                commit=None,
                push=None,
                committed_sha=None,
                pushed=False,
                commit_failed=True,
                integrity_breach=False,
                sha_unverified=False,
                push_status=PUSH_STATUS_NOT_ATTEMPTED,
                diagnostics=diagnostics,
            )

        swept_srcs = [old for old, _new in stage.swept_renames]
        swept_dsts = [new for _old, new in stage.swept_renames]

        # C4 (docs/plans/2026-08-25-the-terminal-handoff-sweep-stops-being-
        # an-op.md § C4): the in-plane terminal-handoff archival sweep runs
        # HERE -- in the ceremony's own process, immediately before
        # `commit_paths` is finalised below -- and its moved src/dst paths
        # join `swept_srcs`/`swept_dsts` exactly like a caller-managed
        # archival rename already does (`compute_commit_paths`'s own
        # docstring: "Including swept src and dst folds a caller-managed
        # archival rename into the commit atomically without widening the
        # gate's inspection scope"). Every ceremony commit reaches this CALL,
        # so there is no carrier to test for and no follow-up leg to
        # disambiguate (AC-3/AC-10) -- but the call is cadence-gated inside
        # and does corpus work only when the sweep is actually due; see
        # `_run_in_plane_archive_sweep`. A gated, failed, or contended sweep
        # contributes `([], [])` -- `commit_paths` is then byte-identical to
        # what it would have been without this call.
        archive_sweep_srcs, archive_sweep_dsts = _run_in_plane_archive_sweep(
            root, common_dir
        )
        swept_srcs = swept_srcs + archive_sweep_srcs
        swept_dsts = swept_dsts + archive_sweep_dsts

        # `gate_paths`/`commit_paths` derivation is UNCHANGED here --
        # `stage.staged_paths` already includes every deletion this call
        # is about to commit (see `explicit_stage`'s "Deletion staging"
        # section), so `compute_gate_paths` needs no separate union for
        # scope purposes; adding `stage.deletion_paths` again here would
        # only duplicate entries already present.
        gate_paths = compute_gate_paths(stage.staged_paths, list(deleted_paths))
        commit_paths = compute_commit_paths(gate_paths, swept_srcs, swept_dsts)

        # 2026-08-04 fix (defect A/B, see `explicit_stage`'s "Deletion
        # staging" docstring section and `StageOutcome.deletion_paths`):
        # the commit MESSAGE's "Deleted (Step 2.67):" block still needs
        # `stage.deletion_paths` even though `gate_paths` does not --
        # without a claim in the message, `commit_gates.
        # deletion_block_gate`'s Assertion-3 hard-fails a staged
        # deletion in scope with no Step 2.67 block declaring it. Union,
        # never substitution -- a caller (e.g. `wsc_tail.py`) that
        # already names its own deletions keeps that authorship; this
        # only adds a deletion `explicit_stage` newly made committable
        # that the caller never separately declared. Ordered (caller's
        # own `deleted_paths` first, dedup-preserving) so a
        # caller-authored ordering in the message is never disturbed.
        message_deleted_paths = list(deleted_paths) + [
            p for p in stage.deletion_paths if p not in deleted_paths
        ]

        if not commit_paths:
            # Nothing to commit -- benign no-op (e.g. every stage_paths
            # entry was swept/missing and no deleted_paths were
            # supplied). Checked BEFORE the C3 gates run (2026-07-22
            # correction) -- an empty pathspec has no message to
            # validate and nothing for the dirty-tree gate to be scoped
            # to; running the gates here would mean `gate_paths == []`,
            # which -- since 2026-07-22 -- means "scoped to nothing" for
            # dirty_tree_gate, i.e. a guaranteed pass, but there is no
            # reason to pay the git-status-porcelain walk for a commit
            # that was never going to happen.
            #
            # Review: code-reviewer — Finding 2: `pushed=False` here read as
            # a genuine push rejection to callers (wsc_tail.py derives
            # push_status="failed" whenever `pushed is False`), indistinguishable
            # from an actual failed push -- but no push was ever attempted for
            # a benign nothing-to-commit no-op. `pushed=None` already means
            # "no push attempted, not a breach" per this dataclass's own
            # docstring (`pushed is None` -- no remote configured -- is NOT a
            # breach); a nothing-to-commit no-op is the same shape: nothing to
            # push, no invariant violated.
            landed = True
            return PipelineResult(
                stage=stage,
                deletion_gate=None,
                dirty_gate=None,
                carry_gate=None,
                op_scope_gate=None,
                commit=None,
                push=None,
                committed_sha=None,
                pushed=None,
                commit_failed=False,
                integrity_breach=False,
                sha_unverified=False,
                push_status=PUSH_STATUS_NOT_ATTEMPTED,
                diagnostics=diagnostics,
            )

        message = compose_message(
            subject=subject,
            prose=prose,
            deleted_paths=message_deleted_paths,
            kept_entries=kept_entries,
            trailers=trailers,
        )

        # C2 (docs/plans/2026-08-26-the-close-path-spends-its-last-known-
        # levers.md): one `.git/index` parse serves every `read_index` call
        # this block and the `commit()` call below make for the SAME
        # resolved index path -- `dirty_tree_gate` (below),
        # `git_native._v2_state_records_chunked`, and `_index_blobs`'s
        # commit_scoped-side pre-snapshot. `_agree_branch_cas_refusal`'s own
        # CURRENT re-observation passes `fresh=True` and is unaffected (see
        # `index_read_cache_scope`'s own docstring) -- it must keep seeing
        # a peer's mid-window write, which this scope does not change.
        with index_read_cache_scope():
            deletion_gate = deletion_block_gate(message, gate_paths, cwd=root)
            # A `stage_patch`-covered path is scoped OUT of the dirty-tree gate,
            # and only out of that one gate -- see this function's own commit
            # message (subject "dirty-tree gate stops judging stage-patch-covered
            # paths") for the full trace. The one-line version: this gate asks
            # "can this call attribute the WORKTREE edit it is about to commit,"
            # and a covered path's committed content never comes from the
            # worktree at all -- `stage_from_patch()` commits a blob built in a
            # process-private index seeded from `read-tree HEAD`, provenanced by
            # construction. Do not "simplify" by dropping the exclusion or
            # widening it to the other three gates below: they answer questions
            # (message-declared deletions, carry, op scope) that stay valid for a
            # covered path, and this gate's own question does not apply to one.
            dirty_gate = dirty_tree_gate(root, [p for p in gate_paths if p not in patch_covered])
            carry_outcome = carry_gate(root, gate_paths)
            op_scope_outcome = op_scope_coverage_gate(root, gate_paths)

            if not deletion_gate.passed:
                diagnostics.extend(deletion_gate.diagnostics)
            if not dirty_gate.passed:
                diagnostics.append(
                    "dirty-tree gate: unattributable paths: " + ", ".join(dirty_gate.unattributable)
                )
            if not carry_outcome.passed:
                diagnostics.extend(carry_outcome.diagnostics)
            if not op_scope_outcome.passed:
                diagnostics.extend(op_scope_outcome.diagnostics)

            if (
                not deletion_gate.passed
                or not dirty_gate.passed
                or not carry_outcome.passed
                or not op_scope_outcome.passed
            ):
                return PipelineResult(
                    stage=stage,
                    deletion_gate=deletion_gate,
                    dirty_gate=dirty_gate,
                    carry_gate=carry_outcome,
                    op_scope_gate=op_scope_outcome,
                    commit=None,
                    push=None,
                    committed_sha=None,
                    pushed=False,
                    commit_failed=True,
                    integrity_breach=False,
                    sha_unverified=False,
                    push_status=PUSH_STATUS_NOT_ATTEMPTED,
                    diagnostics=diagnostics,
                )

            # opro-01 C-01: stand the post-commit hook's own detached push down
            # for this commit IFF this call will publish it synchronously below.
            # Two publishers for one branch tip is what makes `integrity_breach`
            # racy -- see `git_native._sole_publisher_env`. In `deferred`/`none`
            # this call does NOT push, so the hook's push is the only one there
            # is and suppressing it would strand the commit.
            # `never` also suppresses, for the opposite reason: that caller is
            # not authorized to publish this commit at all, so the hook standing
            # down is the point rather than a stranding
            # (§ `_PUSH_MODES_SUPPRESSING_POST_COMMIT_HOOK`).
            commit_outcome = commit(
                root,
                message=message,
                commit_paths=commit_paths,
                common_dir=common_dir,
                deliverable_id=deliverable_id,
                stage_patch=stage_patch,
                attributed_session_id=attributed_session_id,
                suppress_post_commit_auto_push=(
                    push_mode in _PUSH_MODES_SUPPRESSING_POST_COMMIT_HOOK
                ),
            )
        if commit_outcome.exit_code != 0:
            if not commit_outcome.landed:
                # Unchanged in every respect (W2, docs/plans/2026-08-08-a-
                # landed-commit-reported-as-failed.md): this is the
                # ordinary failure and the already-committed no-op (`git
                # commit` exits 1 on an empty commit set) -- the `finally`
                # below rolls `staged_this_call` back to HEAD either way;
                # for that shape the staged content already matches HEAD,
                # so the rollback is a true no-op (no diagnostic added
                # here beyond the pre-existing `commit_outcome.stderr`,
                # which stays exactly `exit_code=1` -- see
                # `test_scoped_git_commit_cli.py::TestRefusalReporting` for
                # the loud-vs-quiet rendering contract this must not
                # regress).
                #
                # AC10 STOP (C6, docs/plans/2026-08-15-the-ceremony-tail-
                # stops-lying-about-why-it-failed.md): `commit_outcome.
                # stdout_diagnostic` (see that field's own docstring) is
                # deliberately NOT appended to `diagnostics` here.
                # `scoped_git_commit.py::_handler` is not this list's only
                # reader -- `wsc_tail.py`'s own commit step
                # (`diagnostics.extend(pipeline_result.diagnostics)`, its own
                # call site, unconditional, no porcelain-probe reclassifier
                # in between) reads `PipelineResult.diagnostics` RAW on the
                # benign already-committed no-op path too. Appending here
                # would decorate that no-op's diagnostics with git's own
                # "nothing to commit" text in wsc_tail's ceremony trail as
                # well as the CLI, which only `scoped_git_commit.py::
                # _classify_uncommitted`'s `git status --porcelain` probe can
                # tell apart from a real failure -- see that function's own
                # docstring for why a stderr-shape discriminator here cannot
                # substitute for it. Surfacing `stdout_diagnostic` therefore
                # needs a reclassification-aware consumer-side change (in
                # `scoped_git_commit.py` and/or `wsc_tail.py`, both outside
                # this chunk's `writes:`) -- flagged to the EM rather than
                # guessed at here.
                diagnostics.append(commit_outcome.stderr)
                return PipelineResult(
                    stage=stage,
                    deletion_gate=deletion_gate,
                    dirty_gate=dirty_gate,
                    carry_gate=carry_outcome,
                    op_scope_gate=op_scope_outcome,
                    commit=commit_outcome,
                    push=None,
                    committed_sha=None,
                    pushed=False,
                    commit_failed=True,
                    integrity_breach=False,
                    sha_unverified=False,
                    push_status=PUSH_STATUS_NOT_ATTEMPTED,
                    diagnostics=diagnostics,
                    reason=commit_outcome.reason,
                    unprovenanced_paths=commit_outcome.unprovenanced_paths,
                )

            # `commit_outcome.landed=True` with a non-zero `exit_code`
            # (W2, same plan): `git commit` DID create a commit here --
            # `commit()`'s own verification of WHICH sha it landed
            # failed (HEAD unresolvable on an unborn-branch first commit,
            # an empty message subject, or a zero-or-ambiguous
            # `Commit-Token:` match), not the commit itself. Set the
            # local `landed` flag True BEFORE any return so the `finally`
            # below skips the rollback for the STATED reason (history
            # already changed -- there is nothing index-only left to
            # reset) rather than happening to be a content no-op by
            # coincidence, which is what a `landed=False` misclassification
            # here would have produced. `commit_failed` stays False --
            # nothing about the caller's request failed -- and
            # `sha_unverified=True` carries the actionable fact forward.
            # (Review: staff-eng R2 nitpick/maintainability -- passed as
            # a literal at both use sites below rather than through a
            # local, since it is constant at both reads.)
            landed = True
            diagnostics.append(
                "run_commit_pipeline: commit landed but its sha could not be "
                f"verified -- {commit_outcome.stderr}"
            )

            if push_mode != PUSH_MODE_SYNC:
                return PipelineResult(
                    stage=stage,
                    deletion_gate=deletion_gate,
                    dirty_gate=dirty_gate,
                    carry_gate=carry_outcome,
                    op_scope_gate=op_scope_outcome,
                    commit=commit_outcome,
                    push=None,
                    committed_sha=None,
                    pushed=None,
                    commit_failed=False,
                    integrity_breach=False,
                    sha_unverified=True,
                    push_status=PUSH_STATUS_NOT_ATTEMPTED,
                    diagnostics=diagnostics,
                    unprovenanced_paths=commit_outcome.unprovenanced_paths,
                )

            # Let the push proceed (W2): `push_with_retry()` pushes
            # whatever is at HEAD -- it never needs `committed_sha` -- and
            # a commit that genuinely landed but was stranded local-only
            # on this shared branch, because its own sha happened to be
            # unresolvable, is the worse failure. `on_committed` is
            # deliberately NOT invoked below (see the guard immediately
            # after this block for why that is correct, not incidental).
            _pre_push_elapsed = time.time() - _pipeline_t_start
            record_composition_span(
                composition_id=_composition_id,
                name=_COMPOSITION_SPAN_PRE_PUSH,
                invocation_count=1,
                elapsed_secs=_pre_push_elapsed,
                outcome="success",
                t_start=_pipeline_t_start,
                repo_root=root,
                sid=_span_sid,
            )
            _push_t_start = time.time()
            push_outcome = push_with_retry(
                root,
                allow_protected_branch=allow_protected_branch,
                protected_branch_override_reason=protected_branch_override_reason,
            )
            record_composition_span(
                composition_id=_composition_id,
                name=_COMPOSITION_SPAN_PUSH,
                invocation_count=1,
                elapsed_secs=time.time() - _push_t_start,
                outcome="directive_failed" if push_outcome.failed else "success",
                t_start=_push_t_start,
                repo_root=root,
                sid=_span_sid,
            )
            if push_outcome.failed:
                diagnostics.extend(push_outcome.failed)
            pushed = derive_pushed_tristate(push_outcome)
            push_status = derive_push_status(push_outcome)
            if push_status == PUSH_STATUS_PUSHED:
                diagnostics.append(_pushed_range_diagnostic(push_outcome))
                _drain_pending_push_after_sync(root)
            return PipelineResult(
                stage=stage,
                deletion_gate=deletion_gate,
                dirty_gate=dirty_gate,
                carry_gate=carry_outcome,
                op_scope_gate=op_scope_outcome,
                commit=commit_outcome,
                push=push_outcome,
                committed_sha=None,
                pushed=pushed,
                commit_failed=False,
                # Review: staff-eng R2 finding 0 -- `committed_sha is None`
                # here unconditionally, but `sha_unverified=True` means the
                # commit DID land; widened per `PipelineResult.
                # integrity_breach`'s own docstring so a failed push on this
                # path (durable local commit, unpushed, unnameable) reports
                # a breach instead of silently passing as `False`.
                # C2: re-derived off `push_status == "push-failed"` rather
                # than `pushed is False` -- a branch-policy decline is also
                # `pushed is False` and must NOT report a breach.
                integrity_breach=(push_status == PUSH_STATUS_FAILED),
                sha_unverified=True,
                push_status=push_status,
                pushed_range=push_outcome.pushed_range,
                pushed_count=push_outcome.pushed_count,
                diagnostics=diagnostics,
                unprovenanced_paths=commit_outcome.unprovenanced_paths,
            )

        # The commit landed -- `staged_this_call` is now committed
        # history, not index-only residue; nothing left to roll back
        # regardless of what push_with_retry() below does.
        landed = True

        if on_committed is not None and commit_outcome.committed_sha is not None:
            # AC18 crash-resumption hook (Finding 2 fix) -- persist the
            # sentinel with the REAL sha now (before 2026-08-07, this ran
            # "still inside ceremony_lock" -- that mutex is gone; the
            # ordering guarantee this hook needs is "before push", not "under
            # a lock", and still holds) and BEFORE push_with_retry()'s
            # network round-trip, so a crash
            # during push/fetch/rebase is covered by "resume from stamp
            # step" rather than triggering a full re-run and a duplicate
            # commit. Intentionally not wrapped in try/except -- see
            # docstring. The `commit_outcome.committed_sha is not None`
            # half of this guard is exactly right, not accidental (W2,
            # same plan): on the `sha_unverified` path above,
            # `committed_sha` is None precisely because this function
            # cannot NAME the sha that landed -- and this hook's entire
            # contract is "persist the REAL sha" (its own docstring, one
            # line up). There is no sha to persist on that path; calling
            # it with `None`, or inventing a placeholder, would corrupt
            # the AC18 crash-resumption sentinel rather than skip it, so
            # staying unfired here is the correct behaviour for an
            # unverifiable commit, not a bug this plan leaves open.
            on_committed(commit_outcome.committed_sha)

        if push_mode != PUSH_MODE_SYNC:
            # Deferred/none (DEC-1): the push half never runs inside this
            # function's own critical section -- before 2026-08-07 this was
            # phrased "inside the locked section" / "after the lock
            # releases"; `ceremony_lock` is gone, so the caller instead
            # spawns ONE detached push after THIS function returns
            # ("deferred") or issues none at all ("none"). No synchronous
            # push outcome exists, so `pushed` is
            # always None and there is no breach to detect.
            return PipelineResult(
                stage=stage,
                deletion_gate=deletion_gate,
                dirty_gate=dirty_gate,
                carry_gate=carry_outcome,
                op_scope_gate=op_scope_outcome,
                commit=commit_outcome,
                push=None,
                committed_sha=commit_outcome.committed_sha,
                pushed=None,
                commit_failed=False,
                integrity_breach=False,
                sha_unverified=False,
                push_status=PUSH_STATUS_NOT_ATTEMPTED,
                diagnostics=diagnostics,
                unprovenanced_paths=commit_outcome.unprovenanced_paths,
            )

        _pre_push_elapsed = time.time() - _pipeline_t_start
        record_composition_span(
            composition_id=_composition_id,
            name=_COMPOSITION_SPAN_PRE_PUSH,
            invocation_count=1,
            elapsed_secs=_pre_push_elapsed,
            outcome="success",
            t_start=_pipeline_t_start,
            repo_root=root,
            sid=_span_sid,
        )
        _push_t_start = time.time()
        push_outcome = push_with_retry(root)
        record_composition_span(
            composition_id=_composition_id,
            name=_COMPOSITION_SPAN_PUSH,
            invocation_count=1,
            elapsed_secs=time.time() - _push_t_start,
            outcome="directive_failed" if push_outcome.failed else "success",
            t_start=_push_t_start,
            repo_root=root,
            sid=_span_sid,
        )
        if push_outcome.failed:
            diagnostics.extend(push_outcome.failed)
        pushed = derive_pushed_tristate(push_outcome)
        push_status = derive_push_status(push_outcome)
        final_committed_sha = commit_outcome.committed_sha
        if push_status == PUSH_STATUS_PUSHED:
            diagnostics.append(_pushed_range_diagnostic(push_outcome))
            _drain_pending_push_after_sync(root)
            # `push_with_retry()` can fetch + `git rebase --onto` THIS
            # commit on a rejected push before re-pushing, which rewrites
            # its sha (same hazard as `consumed_handoff_stamp.
            # _commit_and_push_follow_up` and `post_commit_tail.
            # _commit_and_push_origin_stub_close` -- see those call sites'
            # own Finding-1 comments for the full mechanism). The
            # pre-push `commit_outcome.committed_sha` captured above is
            # therefore stale in exactly the retry case this ladder
            # exists to handle. Re-resolve HEAD now, after the push
            # actually landed, so `committed_sha` names the commit that
            # is really on the remote. Only the landed path pays this
            # second `rev-parse` -- decline/no-remote/not-attempted/
            # failure paths below never rewrite anything and keep the
            # pre-push value untouched. If the re-read itself fails,
            # fall back to the pre-push sha rather than downgrading a
            # known-good value to None -- it is correct unless a
            # rebase-retry actually fired, and a stale-but-real sha is a
            # better audit trail than a hole. state/bug-backlog/2026-08-11-
            # run-commit-pipeline-reports-a-concurrent-0a91ea7dc77b.yaml
            # (P1): that fallback comment used to describe the ONLY
            # hazard here, but the bare read below fired on every landed
            # push (this whole `if` is `push_status == PUSH_STATUS_PUSHED`,
            # true of an ordinary first-try push too) -- not just the
            # rebase-retry case, so a peer's push landing in this window
            # was silently adopted as ours. `resolve_post_push_sha` re-reads
            # HEAD exactly as before but only ADOPTS it once its tree
            # matches `commit_outcome.committed_sha`'s (see that helper's
            # own docstring for why tree identity, not another `git log
            # --grep`, is the right discriminator here); a mismatch keeps
            # the pre-push value instead of guessing.
            final_committed_sha = resolve_post_push_sha(root, commit_outcome.committed_sha)
        # C2: re-derived off `push_status == "push-failed"` rather than
        # `pushed is False` -- a branch-policy decline is also `pushed is
        # False` and must NOT report a breach; nothing was breached, the
        # engine did what doctrine says.
        integrity_breach = (
            commit_outcome.committed_sha is not None and push_status == PUSH_STATUS_FAILED
        )

        return PipelineResult(
            stage=stage,
            deletion_gate=deletion_gate,
            dirty_gate=dirty_gate,
            carry_gate=carry_outcome,
            op_scope_gate=op_scope_outcome,
            commit=commit_outcome,
            push=push_outcome,
            committed_sha=final_committed_sha,
            pushed=pushed,
            commit_failed=False,
            integrity_breach=integrity_breach,
            sha_unverified=False,
            push_status=push_status,
            pushed_range=push_outcome.pushed_range,
            pushed_count=push_outcome.pushed_count,
            diagnostics=diagnostics,
            unprovenanced_paths=commit_outcome.unprovenanced_paths,
        )
    finally:
        # `BaseException`-safe via `finally` (not a bare `except`) --
        # covers a normal failure return above AND any in-process
        # Python-level exception (an unhandled error raised inside the
        # `try`, or a `KeyboardInterrupt`/exception-from-signal-handler
        # unwind) reaching this frame. Scoped strictly to
        # `staged_this_call`; see the bookkeeping comment above for why
        # this must never widen to a bare/derived pathspec.
        #
        # Review: code-reviewer -- Finding 1 (verified, not just softened):
        # this is now a two-halves fix, both landed. The SessionEnd hook
        # (`DoE-claude coordinator/hooks/scripts/sessionend-auto-commit.py`,
        # `a762df6f9888`) no longer hard-kills on timeout: it soft-
        # terminates this pipeline's own CLI (`coordinator/bin/safe-
        # commit-offer.py`), waits a 5s grace window, and only hard-kills
        # if still alive. On our side, that CLI's `main()` installs a
        # SIGTERM handler converting the signal into `sys.exit(1)`, so
        # `SystemExit` propagates through this `finally` on POSIX the way
        # `SIG_DFL` never would. The residual gap is now narrower and
        # named: (a) Windows, where `Popen.terminate()` is
        # `TerminateProcess` and no handler runs regardless, and (b) a
        # hard kill after the grace window expires (`Popen.kill()` --
        # SIGKILL on POSIX -- same shape this repo's own `cs_timeout()`
        # in `coordinator_core/watchdog.py` documents). In either
        # residual case this `finally` does not run, and any
        # `staged_this_call` residue is left in the index for a future
        # commit on this shared branch to absorb.
        if not landed and staged_this_call:
            git_native.reset_paths(root, staged_this_call)
