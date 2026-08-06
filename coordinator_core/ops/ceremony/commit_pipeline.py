"""
coordinator_core.ops.ceremony.commit_pipeline -- native stage -> commit -> push
critical section, integrating C1 (git_native), C2 (commit_message), and C3
(commit_gates). This is the C4 chunk of the `wsc_tail` rebuild
(docs/plans/2026-07-16-wsc-pure-python-tail-rebuild.md).

`run_commit_pipeline()` is the single entry point: it acquires
`ceremony_lock` for the worktree, classifies the caller-supplied paths
against staged git state (tolerant explicit-stage -- a path already swept by
a concurrent archival op is skipped, not treated as a `git add` failure),
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
  - `committed_sha` is captured immediately post-commit, still inside
    `ceremony_lock` -- never re-derived later via a racy `git rev-parse HEAD`
    that could pick up a concurrent sibling's own commit on the same shared
    branch.
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
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

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
from coordinator_core.ops.ceremony.ceremony_lock import ceremony_lock
from coordinator_core.ops.ceremony.commit_gates import (
    DirtyTreeOutcome,
    GateOutcome,
    deletion_block_gate,
    dirty_tree_gate,
)
from coordinator_core.ops.ceremony.commit_message import (
    compose_message,
    compute_commit_paths,
    compute_gate_paths,
)
from coordinator_core.ops.session.safe_commit_offer import compute_offer
from coordinator_core.session.core import session_dir as _session_core_session_dir
from coordinator_core.session.core import sessions_dir as session_hub_dir
from coordinator_core.session.liveness import live_session_ids

#: Default named lock this pipeline's critical section acquires. A distinct
#: name from any other `ceremony_lock` caller in this rebuild -- see
#: `ceremony_lock.py` module docstring for the per-name-per-worktree contract.
LOCK_NAME = "wsc-commit"

DEFAULT_LOCK_TIMEOUT_SECS = 75.0

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

_PUSH_REJECT_MARKERS = (
    "non-fast-forward",
    "fetch first",
    "rejected",
    "failed to push some refs",
)
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
    detail = result.stderr.strip() or result.stdout.strip()
    if detail:
        return detail[:200]
    paths_preview = ", ".join(attempted[:5])
    if len(attempted) > 5:
        paths_preview += f", ... ({len(attempted)} total)"
    return (
        f"exit_code={result.returncode} (no diagnostic output on stdout/stderr "
        f"-- attempted paths: {paths_preview})"
    )[:200]


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
      "Deletion staging" below) ->
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

    existing: List[str] = []
    skipped: List[str] = []
    swept_renames: List[Tuple[str, str]] = []
    missing_caller_paths: List[str] = []
    already_staged_deletions: List[str] = []
    to_delete: List[str] = []

    for p in paths:
        if (root / p).exists():
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
                else:
                    skipped.append(f"missing:{p}")
            else:
                skipped.append(f"swept:{p}->{new}")
                swept_renames.append((p, new))
        elif p in swept_delete:
            if p in caller_paths:
                # This IS the caller's own deletion, already staged (e.g. a
                # prior `git rm`) -- no `git add` needed, but it belongs in
                # the commit set exactly like a diverged path does (staged,
                # not re-touched by this call).
                skipped.append(f"already-staged-deleted:{p}")
                already_staged_deletions.append(p)
            else:
                skipped.append(f"swept-deleted:{p}")
        elif p in worktree_deleted:
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
            f"partially staged ({(residue_result.stderr.strip() or f'exit_code={residue_result.returncode}')[:200]})",
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


@dataclass(frozen=True)
class CommitOutcome:
    """Typed result of the `git commit -F <msgfile> -- <paths>` step.

    Fields:
        exit_code -- 0 on success.
        committed_sha -- the post-commit HEAD SHA, captured immediately
            (still inside `ceremony_lock`) -- None on any commit or capture
            failure.
        stderr -- git's stderr on a non-zero exit, "" on success.
    """

    exit_code: int
    committed_sha: Optional[str] = None
    stderr: str = ""


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
    known_checked: Optional[Set[str]] = None,
    known_diverged: Optional[Set[str]] = None,
    common_dir: Optional[Path] = None,
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
    726925b2) neither commit form is safe against alone. Either branch
    leaves the real HEAD pointed at the new commit, so the post-commit
    `rev_parse_head()` capture below is unchanged regardless of which
    branch ran. Captures the post-commit HEAD SHA immediately (race-safe --
    see module docstring), and unlinks the temp file in a `finally`
    regardless of outcome.

    `known_checked`/`known_diverged` -- optional pass-through of a divergence
    answer `explicit_stage()` already computed earlier in THIS SAME locked
    critical section (see `run_commit_pipeline`'s call site). Threading these
    lets `commit_scoped()` skip re-deriving divergence for every path already
    vetted, instead of re-spawning its own `diverging_paths()` pair -- see
    that function's own docstring for the correctness argument (only sound
    within one lock hold; never cached, never reused across a pass).

    `common_dir` -- optional pre-resolved git common dir, passed straight
    through to `_write_commit_message_tempfile` (see there); purely a spawn
    dedup, never an outcome input.
    """
    root = Path(worktree_root)
    msg_file = _write_commit_message_tempfile(root, message, common_dir)
    try:
        result = git_native.commit_scoped(
            commit_paths,
            msg_file,
            root,
            known_checked=known_checked,
            known_diverged=known_diverged,
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
                stderr=(result.stderr.strip() or f"exit_code={result.returncode}")[:200],
            )
        sha_result = git_native.rev_parse_head(root)
        committed_sha = sha_result.stdout.strip() if sha_result.ok else None
        return CommitOutcome(exit_code=0, committed_sha=committed_sha)
    finally:
        try:
            msg_file.unlink()
        except OSError:
            print(f"skip: commit: msg_file.unlink() failed: {sys.exc_info()[1]}", file=sys.stderr)
            pass


# ---------------------------------------------------------------------------
# Push-with-retry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PushOutcome:
    """Typed result of `push_with_retry()`.

    Fields:
        exit_code -- 0 iff the push genuinely LANDED on the remote (or was
            skipped because no remote is configured).
        acted -- `["push"]` on a landed push, else empty.
        skipped -- `["push:no-remote"]` when no remote is configured, else
            empty.
        failed -- non-empty only on a genuine push failure (rejected after
            exhausting retries, or a fetch/rebase step itself failed).
    """

    exit_code: int
    acted: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)


def _is_push_reject(reason: str) -> bool:
    """True if a git-push stderr `reason` looks like a non-fast-forward reject."""
    lowered = reason.lower()
    return any(marker in lowered for marker in _PUSH_REJECT_MARKERS)


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
    """
    mb_result = git_native.merge_base(worktree_root, "HEAD", upstream_ref)
    if not mb_result.ok:
        reason = (mb_result.stderr.strip() or "merge-base failed")[:200]
        return mb_result.returncode or 1, f"git merge-base: {reason}"
    merge_base_sha = mb_result.stdout.strip()
    if not merge_base_sha:
        return 1, "git merge-base: empty output"

    rebase_result = git_native.rebase_onto(worktree_root, upstream_ref, merge_base_sha)
    if rebase_result.ok:
        return 0, ""
    reason = (
        rebase_result.stderr.strip()
        or rebase_result.stdout.strip()
        or f"exit_code={rebase_result.returncode}"
    )[:200]
    git_native.rebase_abort(worktree_root)
    return rebase_result.returncode or 1, f"git rebase: {reason}"


def push_with_retry(worktree_root: Union[str, Path]) -> PushOutcome:
    """Push with reject-detect -> fetch -> rebase --onto -> re-push, bounded.

    No `--force` at any point. Bounded to `_PUSH_MAX_RETRIES` attempts. When
    no remote is configured, the push is skipped (`exit_code == 0`, nothing
    to sync -- not a failure). On a rejected push, fetches the remote,
    rebases this session's own commit range onto the updated ref (never a
    bare rebase on the shared branch), and re-pushes. If the rebase itself
    refuses, or retries are exhausted while still rejected, returns a hard
    non-zero failure -- never a silent skip that lets the caller believe the
    push landed.
    """
    root = Path(worktree_root)

    remote_check = git_native.remote(root)
    if not remote_check.stdout.strip():
        return PushOutcome(exit_code=0, skipped=["push:no-remote"])

    upstream_ref: Optional[str] = None
    last_reason = ""
    last_exit_code = 1

    for attempt in range(_PUSH_MAX_RETRIES):
        push_result = git_native.push(root)
        if push_result.ok:
            return PushOutcome(exit_code=0, acted=["push"])

        reason = (push_result.stderr.strip() or f"exit_code={push_result.returncode}")[:200]
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
            fetch_reason = (fetch_result.stderr.strip() or "fetch failed")[:200]
            last_reason = f"git fetch: {fetch_reason}"
            last_exit_code = fetch_result.returncode or 1
            break

        rebase_exit_code, rebase_reason = _rebase_onto_fetched_ref(root, upstream_ref)
        if rebase_exit_code != 0:
            last_reason = rebase_reason
            last_exit_code = rebase_exit_code
            break

    return PushOutcome(exit_code=last_exit_code, failed=[f"git push: {last_reason}"])


def derive_pushed_tristate(push_outcome: Optional[PushOutcome]) -> Optional[bool]:
    """Derive the `pushed` tri-state from a `PushOutcome` (or None -- never attempted).

    True  -- the push genuinely synced this run.
    False -- attempted and did not land, or the pipeline never reached the
              push step (a gate or the commit itself failed first).
    None  -- no remote configured -- must NOT be conflated with a failed push.
    """
    if push_outcome is None:
        return False
    if push_outcome.exit_code != 0:
        return False
    if "push:no-remote" in push_outcome.skipped:
        return None
    return True


# ---------------------------------------------------------------------------
# Orchestration: stage -> gates -> commit -> push, inside ceremony_lock
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PipelineResult:
    """Typed result of `run_commit_pipeline()` -- the full stage->commit->push pass.

    Outcome tri-state predicates (see individual fields):
        commit_failed -- True iff a gate failed OR the `git commit` step
            itself returned non-zero. The pipeline never reaches the push
            step when this is True.
        pushed -- see `derive_pushed_tristate`.
        integrity_breach -- True iff the commit LANDED locally
            (`committed_sha` is not None) but did NOT land on the remote
            (`pushed is False`) -- a state where the local ceremony
            invariant "every commit this op makes is pushed" no longer
            holds and a human/EM must resolve the divergence. A skipped
            push (`pushed is None`, no remote configured) is NOT a breach --
            there is no remote invariant to violate.
    """

    stage: StageOutcome
    deletion_gate: Optional[GateOutcome]
    dirty_gate: Optional[DirtyTreeOutcome]
    commit: Optional[CommitOutcome]
    push: Optional[PushOutcome]
    committed_sha: Optional[str]
    pushed: Optional[bool]
    commit_failed: bool
    integrity_breach: bool
    diagnostics: List[str] = field(default_factory=list)


def _resolve_pass_common_dir(cwd: str) -> Optional[Path]:
    """Resolve this worktree's git common dir ONCE per pipeline pass, through
    the resolver whose memo the rest of the pass reads.

    Purpose: three consumers in a single `run_commit_pipeline` pass need the
    same `git rev-parse --path-format=absolute --git-common-dir` answer --
    `ceremony_lock` (lock dir), `_write_commit_message_tempfile` (msgfile
    dir), and `_live_peer_exists` (session hub, reached via
    `session.liveness.live_session_ids` -> `session.core.sessions_dir`). Two
    independent resolvers exist for that one fact, each with its own memo and
    its own deliberately-different failure contract:
    `lifecycle.git_common_dir` (raises `RuntimeError`, keyed on a `Path`) and
    `session.core.sessions_dir` (returns `""`, keyed on a `cwd` STRING) --
    see the latter's docstring for why the two stay separate. Neither memo
    can serve the other, so a pass touching both paid the spawn TWICE.

    Resolving here through `session.core.sessions_dir(cwd)` -- the one whose
    memo `_live_peer_exists` will later read, and which cannot be primed from
    the outside -- collapses that to one spawn: the derived common dir is
    threaded explicitly into the two `lifecycle.git_common_dir` consumers
    (both already accept a pre-resolved value), and the liveness read is
    served from this call's own memo entry. The derivation is exact, not
    approximate: `sessions_dir` is defined as
    `<git-common-dir>/coordinator-sessions` over the identical git
    invocation, so `.parent` recovers `lifecycle.git_common_dir`'s value
    byte-for-byte.

    Returns None when the hub cannot be resolved (not a git repo, git
    missing, transient spawn failure -- `sessions_dir` reports all three as
    `""`, and a failure is deliberately NOT memoized there). None means "no
    pre-resolved value": every consumer then falls back to exactly the
    resolution it performed before this dedup existed, including
    `ceremony_lock`'s own `worktree_root / ".git"` last resort. NOTHING about
    lock identity, message content, or commit outcome may ever branch on
    whether this returned a value -- it is a cost optimisation only.
    """
    hub = session_hub_dir(cwd)
    if not hub:
        return None
    return Path(hub).parent


def _live_peer_exists(session_id: str, cwd: str) -> bool:
    """Is there a live session in this repo OTHER than `session_id`?

    Precondition gate for `_derive_absorbed_peer_claims_trailer`, and exact
    rather than approximate: an `Absorbed-peer-claims:` line can only ever be
    emitted for a path a LIVE PEER holds a claim on -- that is the whole
    content of the trailer -- so "no live peer exists" implies "the derivation
    can only produce the empty string". Whenever this returns False, the
    `compute_offer` walk (~6 git spawns) is provably pure cost, and the
    representative single-session commit keeps its pre-SC-DR-019 spawn count
    (`coordinator_core/ops/ceremony/tests/test_wsc_tail_parity.py`'s KPI
    bound). Liveness is read off `session.liveness.live_session_ids` --
    filesystem + pid, no per-claim git walk -- and this session's own resolved
    id is subtracted, since a claim held by oneself is never "absorbed".

    Costs NO subprocess of its own on the pipeline path: `live_session_ids`
    reaches `session.core.sessions_dir(cwd)`, which memoizes on an explicit
    `cwd`, and `run_commit_pipeline` has already resolved that same `cwd`
    through `_resolve_pass_common_dir` before acquiring the lock -- so this read
    is served from that memo entry. Pass the SAME `cwd` string the pipeline
    passed there (`str(root)`); a differently-spelled but equivalent path
    (trailing slash, unresolved symlink) is a cache MISS and silently
    reintroduces the spawn this gate exists to avoid.

    Fail-open in the SAME direction as the derivation it guards: an
    unreadable/raising liveness enumeration returns False (no trailer), NOT
    True. Falling through to `compute_offer` "just in case" would let a
    degraded liveness read silently reintroduce the cost this gate exists to
    remove, and the trailer is advisory by construction -- a missed record is
    the correct failure.

    NEGATIVE SPEC: this is a COST GATE on an advisory record, and must never
    be repurposed into an allow/deny path. It decides only whether a record is
    worth computing -- never whether a commit, a path, or a claim is permitted.
    Nothing about staging, gating, or scope enforcement may ever read it.
    """
    try:
        live = live_session_ids(cwd)
        return bool(frozenset(live) - {session_id})
    except Exception:  # noqa: BLE001 - fail-open toward "no trailer"
        return False


#: Gate (b) marker (PM ruling, plan "touched.txt sibling-path escape and the
#: suppressed absorbed-peer-claims trailer"): returned by
#: `_derive_absorbed_peer_claims_trailer` when a live peer exists but
#: `session_id` could not be resolved to a real session at all -- distinct by
#: construction from the ordinary `""` negative (see that function's own
#: docstring). Wording is this module's own; the trailer-line format
#: (`Absorbed-peer-claims:` prefix) matches the populated form.
#:
#: Shape divergence (Review: code-reviewer -- Finding 2, 2026-08-05): the
#: POPULATED trailer is `key:` followed by a newline and indented lines
#: (`Absorbed-peer-claims:\n  <path>: session <sid> (claimed, live)`); this
#: marker is `key: <value>` on a single line -- both share the
#: `Absorbed-peer-claims` key but disagree on shape. No current consumer
#: parses this trailer programmatically, so nothing breaks today, but a
#: future trailer-parser keyed on `Absorbed-peer-claims:` must special-case
#: this marker's single-line shape rather than treating it as a degenerate
#: empty block. Left as-is deliberately -- the string is already committed
#: and covered by test assertions; do not "fix" the shape mismatch by
#: changing it without also updating those.
_ABSORBED_PEER_CLAIMS_UNDETERMINED_MARKER = "Absorbed-peer-claims: <undetermined>"


def _derive_absorbed_peer_claims_trailer(
    session_id: str, commit_paths: Sequence[str], cwd: str,
) -> str:
    """Derive the `Absorbed-peer-claims:` commit trailer (SC-DR-019).

    Purpose: cross-repo ruling SC-DR-019 (example-doctrine-repo, `coordinator/docs/wiki/
    scoped-safety-commits.md` @ bdc0aa697) asks for a DERIVED record, at
    commit time, of which paths in THIS commit's own pathspec were claimed by
    a LIVE PEER session -- never author-supplied prose (the predecessor
    mechanism, `Substrate-changes-attribution:`, failed precisely because it
    asked the author to reconstruct attribution by hand, and so could be
    wrong or omitted without anyone noticing). Composing
    `coordinator_core.ops.session.safe_commit_offer.compute_offer` here --
    the same allow-list source `scope_report.assert_paths_in_session_scope`
    reads -- means this trailer can never be laundered by narrative: it is
    read straight off `compute_offer`'s own `excluded` narration for exactly
    the paths this commit is about to land.

    WARN-ONLY BY CONSTRUCTION (DR-256, narrowed by PM ruling on the plan
    "touched.txt sibling-path escape and the suppressed absorbed-peer-claims
    trailer"): this function only RECORDS and never raises -- but the
    never-raising, warn-only channel now carries TWO DISTINGUISHABLE
    outcomes rather than one:

      - `""` (empty) -- "nothing to report", covering both an ordinary
        negative (no live peer, or a live peer exists but claims nothing in
        `commit_paths`) AND an ordinary degraded read (a raising/exception
        `compute_offer`, a malformed `excluded` shape). These two stay
        indistinguishable from each other, exactly as DR-256 originally
        specified -- a caller cannot tell "nothing was absorbed" from "the
        read degraded", and must not need to.
      - `_ABSORBED_PEER_CLAIMS_UNDETERMINED_MARKER` -- a live peer exists
        (gate a passed) but `session_id` is empty or names a session with no
        existing session directory (gate b): the derivation could not even
        ATTEMPT to resolve attribution, as distinct from attempting it and
        finding nothing. This is the shape a fabricated/unresolvable
        `session_id` takes -- P2 (see this function's own defect writeup at
        the module docstring, "the marker") -- and it must not be laundered
        into the same silent "" as an ordinary negative, or a caller
        upstream of the C4c ownership gate that fails CLOSED on a fabricated
        identity would see this trailer fail OPEN on the exact same
        identity.

    Any exception in the derivation body itself (gate c: a raising/degraded
    `compute_offer`) still yields `""`, and the caller commits exactly as it
    would have with no derivation at all -- this function must never gate,
    delay, or fail a commit.

    Scoped to `commit_paths` -- the pipeline's own actual staged/commit
    pathspec -- not a survey of the whole dirty tree, so a peer claim on an
    unrelated file never appears here. Returns "" (no header) when no path
    in `commit_paths` is peer-claimed. Otherwise returns a single
    `Absorbed-peer-claims:` block, one deterministically-sorted line per
    absorbed path:

        Absorbed-peer-claims:
          <path>: session <sid> (claimed, live)

    Never touches the index or staged/working tree -- `compute_offer` is
    read-only (see its own module docstring).

    Gated on `_live_peer_exists` BEFORE `compute_offer` is called at all: with
    no live peer the derivation's only possible output is `""`, so the walk is
    skipped rather than run and discarded (see that helper for the exactness
    argument and the cost-gate negative spec).
    """
    if not commit_paths:
        return ""

    if not _live_peer_exists(session_id, cwd):
        return ""

    # Gate (b), PM ruling (see this function's docstring): a live peer
    # exists, but `session_id` itself cannot be resolved to a real session --
    # either empty, or naming an id `session.core.session_dir` reports has no
    # existing directory (exactly P2's shape: a lock-only id minted for
    # `ceremony_lock` re-entrancy, never registered as a session). Guarded on
    # the CONDITION, not by letting `compute_offer` raise and catching it --
    # a fabricated id can resolve `compute_offer` successfully (see
    # `scoped_git_commit.py`'s own "REVERTED 2026-08-03" docstring section
    # for the sibling lesson: two individually-correct fail-closed fixes
    # composing into a live wedge when one of them is exception-shaped
    # instead of condition-shaped). Distinct from an ordinary "" negative --
    # see the docstring above.
    if not session_id or not os.path.isdir(_session_core_session_dir(session_id, cwd)):
        return _ABSORBED_PEER_CLAIMS_UNDETERMINED_MARKER

    try:
        offer = compute_offer(session_id, cwd)
    except Exception:  # noqa: BLE001 - fail-open, never blocks a commit
        return ""

    excluded = offer.get("excluded") if isinstance(offer, dict) else None
    if not isinstance(excluded, list):
        return ""

    claims: Dict[str, str] = {}
    for entry in excluded:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        reason = str(entry.get("reason", ""))
        if not isinstance(path, str) or not reason.startswith("owned by session"):
            continue
        sid = reason.removeprefix("owned by session ").strip()
        if sid:
            claims[path] = sid

    commit_path_set = set(commit_paths)
    absorbed = sorted(p for p in commit_path_set if p in claims)
    if not absorbed:
        return ""

    lines = [f"  {p}: session {claims[p]} (claimed, live)" for p in absorbed]
    return "Absorbed-peer-claims:\n" + "\n".join(lines)


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
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT_SECS,
    on_committed: Optional[Callable[[str], None]] = None,
    push_mode: str = PUSH_MODE_SYNC,
    attribution_session_id: Optional[str] = None,
) -> PipelineResult:
    """Run the full stage -> gate -> commit -> [push] critical section.

    Purpose: the C4 orchestration entry point. Acquires `ceremony_lock` for
    the duration of the entire critical section. In `push_mode="sync"`
    (default -- `scoped_git_commit.py`'s untouched wire contract, DEC-1/F1),
    the critical section spans stage through push-with-retry, exactly as
    before. In `push_mode="deferred"|"none"`, the lock's critical section
    spans ONLY stage -> gates -> commit -- `push_with_retry()` is skipped
    entirely, `pushed` is always `None`, and `integrity_breach` is always
    `False` (there is no synchronous push outcome to breach against; see
    `wsc_tail.py`'s deferred-push design, DEC-1).

    `attribution_session_id` (PM ruling, plan "touched.txt sibling-path
    escape and the suppressed absorbed-peer-claims trailer"): the id
    `_derive_absorbed_peer_claims_trailer` (step 3 below) uses for its OWN
    `compute_offer` attribution lookup, kept deliberately SEPARATE from
    `session_id` -- the identifier `ceremony_lock` re-entrancy is keyed on
    (AC7). `session_id` alone is not always a resolvable session: `scoped_
    git_commit.py`'s `_handler` mints a private `scoped-git-commit-<uuid4>`
    purely as a lock key, with no session directory ever created for it
    (P2's defect shape -- the lock key and the committing session's own
    identity were the same variable). Defaults to `None`, which falls back
    to `session_id` itself (this parameter's ABSENCE reproduces every
    pre-existing caller's behavior byte-for-byte -- see this module's own
    test suite, `test_absorbed_peer_claims_trailer.py`, which calls this
    function with a single real `session_id` and no `attribution_session_id`
    at all). A caller that mints a lock-only id MUST pass its own
    separately-resolved committing-session id here: both `scoped_git_
    commit.py`'s `_handler` AND `execute_plan_assemble`/`close_out_and_
    stamp.py` mint a private per-invocation `session_id` purely as the lock
    key (P2's exact shape), so both now resolve and pass a real
    `attribution_session_id` (Review: code-reviewer -- Finding 1, 2026-08-05).
    `wsc_tail.py` is the one caller that already passes a real session id as
    `session_id` and needs no override.

    Sequence:
      0. `_resolve_pass_common_dir(str(root))` -- the pass's ONE git-common-dir
         resolution, taken before the lock and threaded into every consumer
         that would otherwise re-derive it (`ceremony_lock`, `commit()`'s
         msgfile writer, and -- via the shared `sessions_dir` memo -- the
         step-3 liveness gate). See that helper for why one resolver cannot
         simply serve the other's memo. Cost-only: `None` (unresolvable) puts
         every consumer back on its own pre-existing resolution path.
      1. `explicit_stage(stage_paths, caller_paths)` -- tolerant staging.
      2. `gate_paths = compute_gate_paths(stage.staged_paths, deleted_paths)`;
         `commit_paths = compute_commit_paths(gate_paths, swept_srcs,
         swept_dsts)` from `stage.swept_renames` -- the full explicit
         pathspec (AC5). An empty `commit_paths` short-circuits HERE, before
         either C3 gate runs (2026-07-22 correction: moved above the gates --
         there is no message to validate and nothing to scope the dirty-tree
         gate to when there is nothing to commit; `commit_failed=False`, a
         benign no-op).
      3. `_derive_absorbed_peer_claims_trailer(attribution_session_id or
         session_id, commit_paths, root)` (SC-DR-019) -- appended to
         caller-supplied `trailers` (never
         replacing it), then `compose_message(...)` via C2, using
         `gate_paths`'s Deleted/Kept claims already supplied by the caller
         (`deleted_paths`/`kept_entries` are the SOURCE of the message
         blocks; `gate_paths` is the scope the deletion-block gate inspects
         them against). The derivation call is scoped to `commit_paths` --
         the actual commit pathspec -- and is warn-only/fail-open by
         construction (see that function's own docstring): it never raises
         into this critical section and never changes any outcome below.
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
         captures `committed_sha`. On a successful commit, `on_committed`
         (when supplied) is invoked with the real `committed_sha` BEFORE
         `push_with_retry()` runs (step 6) -- this is the AC18 crash-
         resumption hook: the caller (`wsc_tail.py`) uses it to persist the
         commit sentinel the instant the commit has landed, so a crash
         during the push-with-retry network round-trip (fetch/rebase/re-push
         -- the most crash-exposed sub-window in the pipeline) is still
         covered by AC18's "resume from stamp step, never double-commit"
         guarantee. Never called on a failed/short-circuited commit. Any
         exception `on_committed` itself raises propagates -- the sentinel
         write is intentionally NOT best-effort (a silently-failed sentinel
         write would silently reopen the exact duplicate-commit gap this
         hook exists to close).
      6. `push_with_retry()` -- only when the commit landed AND
         `push_mode="sync"`. Skipped entirely for `push_mode="deferred"|
         "none"` -- see the DEC-1 paragraph above.

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
    common_dir = _resolve_pass_common_dir(str(root))

    with ceremony_lock(
        root,
        name=LOCK_NAME,
        session_id=session_id,
        timeout=lock_timeout,
        git_common_dir=common_dir,
    ):
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
                commit=None,
                push=None,
                committed_sha=None,
                pushed=False,
                commit_failed=True,
                integrity_breach=False,
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
                    commit=None,
                    push=None,
                    committed_sha=None,
                    pushed=False,
                    commit_failed=True,
                    integrity_breach=False,
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
                    commit=None,
                    push=None,
                    committed_sha=None,
                    pushed=None,
                    commit_failed=False,
                    integrity_breach=False,
                    diagnostics=diagnostics,
                )

            absorbed_trailer = _derive_absorbed_peer_claims_trailer(
                attribution_session_id if attribution_session_id is not None else session_id,
                commit_paths, str(root),
            )
            combined_trailers = (
                trailers + "\n" + absorbed_trailer
                if trailers and absorbed_trailer
                else trailers or absorbed_trailer
            )

            message = compose_message(
                subject=subject,
                prose=prose,
                deleted_paths=message_deleted_paths,
                kept_entries=kept_entries,
                trailers=combined_trailers,
            )

            deletion_gate = deletion_block_gate(message, gate_paths, cwd=root)
            dirty_gate = dirty_tree_gate(root, gate_paths)

            if not deletion_gate.passed:
                diagnostics.extend(deletion_gate.diagnostics)
            if not dirty_gate.passed:
                diagnostics.append(
                    "dirty-tree gate: unattributable paths: " + ", ".join(dirty_gate.unattributable)
                )

            if not deletion_gate.passed or not dirty_gate.passed:
                return PipelineResult(
                    stage=stage,
                    deletion_gate=deletion_gate,
                    dirty_gate=dirty_gate,
                    commit=None,
                    push=None,
                    committed_sha=None,
                    pushed=False,
                    commit_failed=True,
                    integrity_breach=False,
                    diagnostics=diagnostics,
                )

            commit_outcome = commit(
                root,
                message=message,
                commit_paths=commit_paths,
                known_checked=stage.checked_paths,
                known_diverged=stage.diverged_paths,
                common_dir=common_dir,
            )
            if commit_outcome.exit_code != 0:
                # Includes the ordinary already-committed no-op (`git
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
                    commit=commit_outcome,
                    push=None,
                    committed_sha=None,
                    pushed=False,
                    commit_failed=True,
                    integrity_breach=False,
                    diagnostics=diagnostics,
                )

            # The commit landed -- `staged_this_call` is now committed
            # history, not index-only residue; nothing left to roll back
            # regardless of what push_with_retry() below does.
            landed = True

            if on_committed is not None and commit_outcome.committed_sha is not None:
                # AC18 crash-resumption hook (Finding 2 fix) -- persist the
                # sentinel with the REAL sha now, still inside ceremony_lock and
                # BEFORE push_with_retry()'s network round-trip, so a crash
                # during push/fetch/rebase is covered by "resume from stamp
                # step" rather than triggering a full re-run and a duplicate
                # commit. Intentionally not wrapped in try/except -- see
                # docstring.
                on_committed(commit_outcome.committed_sha)

            if push_mode != PUSH_MODE_SYNC:
                # Deferred/none (DEC-1): the push half never runs inside this
                # locked section -- the caller either spawns ONE detached push
                # after the lock releases ("deferred") or issues none at all
                # ("none"). No synchronous push outcome exists, so `pushed` is
                # always None and there is no breach to detect.
                return PipelineResult(
                    stage=stage,
                    deletion_gate=deletion_gate,
                    dirty_gate=dirty_gate,
                    commit=commit_outcome,
                    push=None,
                    committed_sha=commit_outcome.committed_sha,
                    pushed=None,
                    commit_failed=False,
                    integrity_breach=False,
                    diagnostics=diagnostics,
                )

            push_outcome = push_with_retry(root)
            if push_outcome.failed:
                diagnostics.extend(push_outcome.failed)
            pushed = derive_pushed_tristate(push_outcome)
            integrity_breach = commit_outcome.committed_sha is not None and pushed is False

            return PipelineResult(
                stage=stage,
                deletion_gate=deletion_gate,
                dirty_gate=dirty_gate,
                commit=commit_outcome,
                push=push_outcome,
                committed_sha=commit_outcome.committed_sha,
                pushed=pushed,
                commit_failed=False,
                integrity_breach=integrity_breach,
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
