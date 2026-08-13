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
subprocess in this module (directly, or transitively via `commit_scoped()`)
routes through `git_native._git` (AC2/AC3) -- no bare `subprocess.run`.

Spec backlink: docs/plans/2026-07-16-wsc-pure-python-tail-rebuild.md § C4 (AC5).
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
    `push_with_retry()` runs inside this critical section. `scoped_git_
    commit.py` never passes `push_mode`, so it always gets `"sync"` (today's
    byte-for-byte contract) by construction; do not add a call site that
    defaults it to anything else.
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

import os
import re
import tempfile
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

#: `push_mode` values for `run_commit_pipeline()` (wsc-tail-sub-2s-invoke-
#: budget DEC-1/F1). "sync" (default) is `scoped_git_commit.py`'s wire
#: contract, untouched -- that caller never passes `push_mode`, so it always
#: gets "sync" by construction. "deferred"/"none" both skip the in-pipeline
#: `push_with_retry()` call (the caller becomes responsible for the push, or
#: for never issuing one); "deferred" additionally signals the caller
#: (`wsc_tail.py`) to spawn ONE detached background push after its own
#: locked critical section completes.
PUSH_MODE_SYNC = "sync"
PUSH_MODE_DEFERRED = "deferred"
PUSH_MODE_NONE = "none"

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
#: auth, gh-server-reject, network, unknown, empty-stderr) names a failure
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
#: package's `__init__` before a submodule), which eagerly registers all 15
#: `hooks.*` ops as a side effect UNLESS the caller has already armed the
#: shared lazy-hooks channel (`COORDINATOR_CORE_LAZY_OPS` /
#: `sys._coordinator_core_lazy_ops` -- see that `__init__.py`'s own
#: docstring). That side effect is idempotent, additive (registers ops into
#: a dict, no I/O, no mutation of shared state), and already paid by any
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
    #: Caller-supplied paths that landed in `missing_caller_paths` while ONE
    #: OR BOTH of the two probes that rule out "this is actually a rename or
    #: a deletion" (`git diff --cached --name-status --find-renames` for
    #: `swept_rename`/`swept_delete`, `git ls-files --deleted` for
    #: `worktree_deleted`) returned a non-ok `GitResult` -- i.e. the "genuinely
    #: absent" classification was never actually TESTED for this path, only
    #: defaulted to on a probe failure (see this function's own inline
    #: comments at each probe: both degrade to "found nothing" on failure,
    #: best-effort, never raising). A member of THIS set is always also a
    #: member of `missing_caller_paths` -- never a separate bucket a caller
    #: could miss by only reading one field -- it exists purely so a caller
    #: rendering a decline reason (`scoped_git_commit._declined_paths`) can
    #: tell "confirmed absent" from "absence merely assumed because a probe
    #: could not answer" and word the reason honestly instead of asserting a
    #: fact this function never verified.
    unverifiable_missing_caller_paths: List[str] = field(default_factory=list)


_MAX_DIAGNOSTIC_CHARS = 2000

_GIT_NOISE_LINE_RE = re.compile(r"^\s*(?:warning|hint):", re.IGNORECASE)


def condense_git_diagnostic(text: str, *, limit: int = _MAX_DIAGNOSTIC_CHARS) -> str:
    """Reduce a raw git stdout/stderr blob to the part that names the failure.

    2026-08-10 fix (live incident: four consecutive `scoped-git-commit`
    refusals reported nothing but CRLF line-ending warnings, hiding a
    `detect-staged-rollback` pre-commit BLOCK that was the actual cause).
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
      above (2026-08-11 fix, example-doctrine-repo-em memo `cross-repo/inbox/2026-08-11-
      example-doctrine-repo-em-two-gaps-that-let-machine-local-files-stay-tracked.md`
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

    Untrack vs. add (2026-08-11 fix, example-doctrine-repo-em memo `cross-repo/inbox/
    2026-08-11-example-doctrine-repo-em-two-gaps-that-let-machine-local-files-stay-
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
    function).

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
    root = Path(worktree_root)
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
    # Distinct from `swept_delete` above -- see `git_native.ls_files_deleted`'s
    # own docstring for why the staged and unstaged cases need two separate
    # git reads. A failing/indeterminate probe degrades to "no unstaged
    # deletions found" (fail-closed toward the PRE-FIX "missing" bucket,
    # never toward fabricating a deletion this call cannot confirm).
    worktree_deleted: Set[str] = set()
    deleted_result = git_native.ls_files_deleted(root, list(paths))
    if deleted_result.ok:
        worktree_deleted = {line for line in deleted_result.stdout.splitlines() if line}

    # Whether the two probes that rule out "this missing path is actually a
    # rename or deletion" (`diff_result`, `deleted_result` above) actually
    # ANSWERED, rather than degrading to their empty best-effort default on
    # failure. A caller path classified "genuinely absent" while this is
    # False was never actually tested against a rename/deletion -- see
    # `StageOutcome.unverifiable_missing_caller_paths`'s own docstring for
    # why that distinction matters to a reason string rendered downstream.
    rename_delete_probes_ok = diff_result.ok and deleted_result.ok

    existing: List[str] = []
    skipped: List[str] = []
    swept_renames: List[Tuple[str, str]] = []
    missing_caller_paths: List[str] = []
    unverifiable_missing_caller_paths: List[str] = []
    already_staged_deletions: List[str] = []
    to_delete: List[str] = []

    for p in paths:
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
        if p in swept_delete:
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
        elif p in swept_rename:
            new = swept_rename[p]
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
                    if not rename_delete_probes_ok:
                        unverifiable_missing_caller_paths.append(p)
                else:
                    skipped.append(f"missing:{p}")
            else:
                skipped.append(f"swept:{p}->{new}")
                swept_renames.append((p, new))
        elif p in worktree_deleted:
            if p in caller_paths:
                skipped.append(f"deleted:{p}")
                to_delete.append(p)
            else:
                skipped.append(f"worktree-deleted:{p}")
        elif p in caller_paths:
            skipped.append(f"missing-caller:{p}")
            missing_caller_paths.append(p)
            if not rename_delete_probes_ok:
                unverifiable_missing_caller_paths.append(p)
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
            set(
                diverging_paths(
                    existing,
                    cwd=str(root),
                    timeout=_DIVERGENCE_CHECK_TIMEOUT_SECS,
                    fail_loud=True,
                )
            )
            if existing
            else set()
        )
    except DivergenceCheckFailed as exc:
        return StageOutcome(
            exit_code=-1,
            skipped=skipped,
            failed=[
                f"explicit_stage: divergence check indeterminate for {len(existing)} "
                f"path(s) -- refusing to guess which are safe to `git add` ({exc})"
            ],
            swept_renames=swept_renames,
            missing_caller_paths=missing_caller_paths,
            unverifiable_missing_caller_paths=unverifiable_missing_caller_paths,
            ignored_caller_paths=ignored_caller_paths,
        )

    to_stage: List[str] = []
    for p in existing:
        if p in diverged:
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
            unverifiable_missing_caller_paths=unverifiable_missing_caller_paths,
            ignored_caller_paths=ignored_caller_paths,
            checked_paths=set(existing),
            diverged_paths=diverged,
            deletion_paths=deletion_paths,
        )

    add_result = git_native.add_paths(root, to_stage)
    if add_result.ok:
        deletion_paths.extend(to_delete)
        return StageOutcome(
            exit_code=missing_caller_exit_code,
            acted=list(to_stage),
            skipped=skipped,
            staged_paths=staged_paths,
            swept_renames=swept_renames,
            missing_caller_paths=missing_caller_paths,
            unverifiable_missing_caller_paths=unverifiable_missing_caller_paths,
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
    residue_result = git_native.diff_cached_name_only(root, paths=to_stage, nul_separated=True)
    if residue_result.ok:
        residue = set(residue_result.stdout.split("\0")) - {""}
        reconciled_acted = [p for p in to_stage if p in residue]
        failed_entries = [f"git add: {reason}"]
    else:
        reconciled_acted = []
        failed_entries = [
            f"git add: {reason}",
            "explicit_stage: post-failure residue check indeterminate -- "
            "cannot confirm whether any of this call's paths were "
            f"partially staged ({condense_git_diagnostic(residue_result.stderr) or f'exit_code={residue_result.returncode}'})",
        ]

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
        unverifiable_missing_caller_paths=unverifiable_missing_caller_paths,
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
    """

    exit_code: int
    committed_sha: Optional[str] = None
    landed: bool = False
    stderr: str = ""
    worktree_excluded: Tuple[str, ...] = ()


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
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(message)
    except BaseException:
        try:
            os.unlink(raw_path)
        except OSError:
            print(f"skip: _write_commit_message_tempfile: os.unlink(raw_path) failed: {sys.exc_info()[1]}", file=sys.stderr)
            pass
        raise
    return Path(raw_path)


def commit(
    worktree_root: Union[str, Path],
    *,
    message: str,
    commit_paths: Sequence[str],
    common_dir: Optional[Path] = None,
    deliverable_id: Optional[str] = None,
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
    docstring for the two incidents (claude-klabauter 506748a0, example-doctrine-repo
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

        result = git_native.commit_scoped(
            commit_paths, msg_file, root, deliverable_id=deliverable_id
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
            return CommitOutcome(
                exit_code=result.returncode or 1,
                stderr=(
                    condense_git_diagnostic(result.stderr)
                    or f"exit_code={result.returncode}"
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
                    exit_code=0, committed_sha=post_sha_result.stdout.strip(), landed=True
                )
            # `git commit` already created this commit (`result.ok` above) --
            # only its sha is unresolvable. W1: `landed=True` alongside
            # `committed_sha=None` so the caller never treats this as a no-op.
            return CommitOutcome(
                exit_code=1,
                landed=True,
                stderr="commit: landed but sha verification failed -- HEAD unresolvable "
                "on an unborn-branch first commit",
            )
        if not subject:
            # Same reasoning as the unborn-branch case immediately above:
            # the commit landed, only verification could not proceed.
            return CommitOutcome(
                exit_code=1,
                landed=True,
                stderr="commit: landed but sha verification requires a non-empty message subject",
            )

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
                *commit_paths,
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
            )
        return CommitOutcome(exit_code=0, committed_sha=candidates[0], landed=True)
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
        failed -- non-empty only on a genuine push failure (rejected after
            exhausting retries, or a fetch/rebase step itself failed).
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
    ["push"]` -> `pushed`; non-empty `failed` -> `push-failed`; push never
    reached (outcome is `None`) -> `not-attempted`.
    """
    if push_outcome is None:
        return PUSH_STATUS_NOT_ATTEMPTED
    if "push:branch-policy" in push_outcome.skipped or (
        "push:branch-unresolvable" in push_outcome.skipped
    ):
        return PUSH_STATUS_DECLINED
    if "push:no-remote" in push_outcome.skipped:
        return PUSH_STATUS_NO_REMOTE
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
    exhausted while still rejected, returns a hard non-zero failure -- never
    a silent skip that lets the caller believe the push landed.

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
    is example-doctrine-repo's `merging-to-main` SKILL, Step 10 item 5 (the
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

    remote_check = git_native.remote(root)
    if not remote_check.stdout.strip():
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
    pre_push_upstream_result = git_native.rev_parse_upstream(root)
    if pre_push_upstream_result.ok and pre_push_upstream_result.stdout.strip():
        pre_push_upstream_name = pre_push_upstream_result.stdout.strip()
        pre_push_sha_result = git_native.rev_parse(root, pre_push_upstream_name)
        if pre_push_sha_result.ok and pre_push_sha_result.stdout.strip():
            pre_push_upstream_sha = pre_push_sha_result.stdout.strip()

    upstream_ref: Optional[str] = None
    last_reason = ""
    last_exit_code = 1

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

        if not _is_push_reject(reason) or attempt == _PUSH_MAX_RETRIES - 1:
            break

        if upstream_ref is None:
            upstream_result = git_native.rev_parse_upstream(root)
            if not upstream_result.ok or not upstream_result.stdout.strip():
                last_reason = (
                    f"git push: rejected and no upstream tracking ref resolvable ({reason})"
                )
                break
            upstream_ref = upstream_result.stdout.strip()

        remote_name = upstream_ref.split("/", 1)[0] if "/" in upstream_ref else "origin"
        fetch_result = git_native.fetch(root, remote_name)
        if not fetch_result.ok:
            fetch_reason = condense_git_diagnostic(fetch_result.stderr) or "fetch failed"
            last_reason = f"git fetch: {fetch_reason}"
            last_exit_code = fetch_result.returncode or 1
            break

        # AC7 rebase-retry range fix (see docstring above): re-point the
        # lower bound at the tip this fetch just observed, so a landed
        # retry's reported range excludes commits that reached the remote
        # via someone else's push, not this call's.
        refetched_sha_result = git_native.rev_parse(root, upstream_ref)
        if refetched_sha_result.ok and refetched_sha_result.stdout.strip():
            pre_push_upstream_sha = refetched_sha_result.stdout.strip()

        rebase_exit_code, rebase_reason = _rebase_onto_fetched_ref(root, upstream_ref)
        if rebase_exit_code != 0:
            last_reason = rebase_reason
            last_exit_code = rebase_exit_code
            break

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
                    violated" reason.
              This amends the prior docstring, which named ONLY (a) --
              `None` was already double-booked with (c) before this plan
              touched the contract, and (b) is the meaning this plan adds
              on top of both.
    """
    if push_outcome is None:
        return False
    if push_outcome.exit_code != 0:
        return False
    if "push:no-remote" in push_outcome.skipped:
        return None
    if "push:branch-policy" in push_outcome.skipped or (
        "push:branch-unresolvable" in push_outcome.skipped
    ):
        return None
    return True


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
    """
    from coordinator_core.lock_preflight import preflight_reap_stale_lock

    preflight_reap_stale_lock(worktree_root)


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
    example-doctrine-repo's `merging-to-main` SKILL (`coordinator/skills/merging-to-
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

    Purpose: the C4 orchestration entry point. Used to acquire `ceremony_lock`
    for the duration of the entire critical section -- that mutex was deleted
    2026-08-07 (docs/plans/2026-08-07-excise-the-ceremony-lock.md; see that
    plan for the safety argument covering the two residual unserialized
    windows it left, C10's divergence dedup and C11's sha capture, and S2
    Findings 3/4 for two further unserialized windows the plan's own
    enumeration does not name). In `push_mode="sync"` (default --
    `scoped_git_commit.py`'s untouched wire contract, DEC-1/F1), this
    function's own critical section spans stage through push-with-retry,
    exactly as before the lock's removal. In `push_mode="deferred"|"none"`,
    this section spans ONLY stage -> gates -> commit -- `push_with_retry()`
    is skipped entirely, `pushed` is always `None`, and `integrity_breach` is
    always `False` (there is no synchronous push outcome to breach against;
    see `wsc_tail.py`'s deferred-push design, DEC-1).

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

    Spec backlink: docs/plans/2026-07-16-wsc-pure-python-tail-rebuild.md § C9
    (AC18 review Finding 2 -- `on_committed` closes the narrower-than-claimed
    crash-resumption window: previously the sentinel was only written by the
    caller AFTER this whole function returned, which meant a crash during
    `push_with_retry()` left the sentinel at its empty pre-commit placeholder
    and a re-invocation re-ran the entire pipeline, producing a duplicate
    commit).
    """
    root = Path(worktree_root)
    diagnostics: List[str] = []
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
    landed = False
    try:
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

        deletion_gate = deletion_block_gate(message, gate_paths, cwd=root)
        dirty_gate = dirty_tree_gate(root, gate_paths)
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

        commit_outcome = commit(
            root,
            message=message,
            commit_paths=commit_paths,
            common_dir=common_dir,
            deliverable_id=deliverable_id,
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
                )

            # Let the push proceed (W2): `push_with_retry()` pushes
            # whatever is at HEAD -- it never needs `committed_sha` -- and
            # a commit that genuinely landed but was stranded local-only
            # on this shared branch, because its own sha happened to be
            # unresolvable, is the worse failure. `on_committed` is
            # deliberately NOT invoked below (see the guard immediately
            # after this block for why that is correct, not incidental).
            push_outcome = push_with_retry(
                root,
                allow_protected_branch=allow_protected_branch,
                protected_branch_override_reason=protected_branch_override_reason,
            )
            if push_outcome.failed:
                diagnostics.extend(push_outcome.failed)
            pushed = derive_pushed_tristate(push_outcome)
            push_status = derive_push_status(push_outcome)
            if push_status == PUSH_STATUS_PUSHED:
                diagnostics.append(_pushed_range_diagnostic(push_outcome))
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
            )

        push_outcome = push_with_retry(root)
        if push_outcome.failed:
            diagnostics.extend(push_outcome.failed)
        pushed = derive_pushed_tristate(push_outcome)
        push_status = derive_push_status(push_outcome)
        final_committed_sha = commit_outcome.committed_sha
        if push_status == PUSH_STATUS_PUSHED:
            diagnostics.append(_pushed_range_diagnostic(push_outcome))
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
        # (`example-doctrine-repo coordinator/hooks/scripts/sessionend-auto-commit.py`,
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
