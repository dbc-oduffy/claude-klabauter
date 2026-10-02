"""
coordinator_core.ops.ceremony.post_commit_tail — composes the two post-commit
legs of a ceremony commit into ONE in-process `run()`: origin-stub-close
(`handoff.close_origin_stub`, injected as a handler) and the completion-entry
commit-ledger fold. The sole live caller of `run()` is
`execute_plan_assemble/close_out_and_stamp.py::_reach_post_commit_tail_stub_close`;
`workstream_complete.apply` reaches the fold through
`fold_completion_entry_commit`. This module is not a registered op.

Ship-stamping a consumed handoff is not composed here: it belongs to
`/workstream-complete`'s `directives_commit_tail.apply_ship_stamps`, keyed on
the claim ledger. Verdict:
`docs/research/spike-verdicts/2026-08-30-baton-ship-stamp-inside-a-500ms-close.md`.

HARD CONSTRAINT: this module MUST NOT acquire any ceremony-wide serialization
lock; neither leg holds one and the lock mechanism no longer exists. Do not add
one back anywhere in the commit path.

Origin-stub-close handler injection: `run()` takes `close_origin_stub_handler`
as an explicit parameter rather than importing
`coordinator_core.ops.handoff_close_origin_stub._handler`, so a caller (and a
test) patches the handler at its own call site.

Timing: `run()` accepts an optional `timing` object — anything exposing a
`.measure(name)` context manager — and records one span, "origin_stub_close".

Negative-spec:
  - Does NOT acquire any ceremony-wide serialization lock — see HARD
    CONSTRAINT above.
  - Does NOT re-implement `handoff.close_origin_stub`'s own logic — composes
    it via the injected handler callable.
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
from functools import partial
from time import perf_counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.completion_ops import _parse_existing_commits
from coordinator_core.ops.ceremony.push import (
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
    _ceremony_push_budget,
    push_with_retry,
    resolve_post_push_sha,
)
from coordinator_core.git.commit import (
    CommitRefused,
    FilterUnsupported,
    commit_paths,
    hash_worktree_blobs_via_spawn,
)
from coordinator_core.git.commit_trailers import apply_missing_trailers
from coordinator_core.git.index_write import IndexStaleAfterCommit, IndexWriteError
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.session import scope as session_scope

_LOG = logging.getLogger(__name__)

MUTATES = ["docs/plans/*.md", "archive/completed/**/*.md"]  # _fold_sha_into_entry_on_disk folds a landed commit sha into whichever caller-supplied completion_entry_path resolves under (contained to _FOLD_ALLOWED_ROOT_SUFFIXES); data-dependent target, not a fixed artifact

OP_NAME = "ceremony.post_commit_tail"

#: Label prefix for the origin-stub-close leg's skip/fail strings.
OP_CLOSE_ORIGIN_STUB = "handoff.close_origin_stub"


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

    Not subject to AC4's golden-format parity requirement (scoped to the MAIN
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
    """Computed-mechanism follow-up commit (`git.commit.commit_paths`) for
    the closed origin-stub file(s) -- its OWN small commit, never left as an
    unswept dirty working-tree edit. The COMMIT is unconditional; the PUSH
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

    ``follow_up_sha`` is `commit_paths`' ``outcome.sha`` -- Review: code-
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
    _pre_push_elapsed = perf_counter()
    message = _compose_origin_stub_close_message(closed_paths, committed_sha)
    message = apply_missing_trailers(message, worktree_root, closed_paths)
    # `closed_paths` is non-empty (the caller returns early on an empty
    # `closed_by_stub`). `prefer_deliberate_stage` keeps a peer's deliberately
    # staged bytes on a committed path (506748a0 shape).
    try:
        outcome = commit_paths(
            worktree_root,
            closed_paths,
            message,
            prefer_deliberate_stage=True,
            blob_fallback=partial(hash_worktree_blobs_via_spawn, cwd=worktree_root),
        )
        follow_up_sha: Optional[str] = outcome.sha
    except IndexStaleAfterCommit as exc:
        # Landed; only the shared index is stale. Precedes `IndexWriteError`,
        # its base class.
        follow_up_sha = getattr(getattr(exc, "outcome", None), "sha", None)
    except (CommitRefused, FilterUnsupported, IndexWriteError) as exc:
        return None, False, PUSH_STATUS_NOT_ATTEMPTED, f"git commit failed: {exc}"

    # Post-commit claim release (C3d, docs/plans/2026-08-11-claim-release-
    # and-the-gate-that-cannot-clear.md): eligible -- same worktree,
    # `closed_paths` is already repo-relative (each `stub_path` in the
    # composed `handoff.close_origin_stub` op's `closed` list is `rel_id(
    # stub_path, worktree)` -- see `handoff_close_origin_stub.py` line
    # ~703), and `sid` is `run()`'s own required "WSC session id" caller
    # param, threaded down through `_run_origin_stub_close` unchanged --
    # the SAME session this whole post-commit tail is running on behalf
    # of, not a guess. Run synchronously here -- this function already executes
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
    # takes; `release_committed_claims_or_retain` carries both rules.
    session_scope.release_committed_claims_or_retain(
        worktree_root, closed_paths, sid, "_commit_and_push_origin_stub_close"
    )

    if push_mode != PUSH_MODE_SYNC:
        return follow_up_sha, None, PUSH_STATUS_NOT_ATTEMPTED, None

    # elapsed-aware, not the flat slice — see _ceremony_push_budget.
    push_outcome = push_with_retry(
        worktree_root,
        budget_secs=_ceremony_push_budget(perf_counter() - _pre_push_elapsed),
    )
    push_status = derive_push_status(push_outcome)

    if push_status == PUSH_STATUS_PUSHED:
        # `push_with_retry` can
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
    caller), after `committed_sha` is known -- never raises. ``push_mode``
    gates the follow-up commit's push.

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
    _pre_push_elapsed = perf_counter()
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
    message = apply_missing_trailers(message, worktree_root, [entry_relpath])
    try:
        commit_paths(
            worktree_root,
            [entry_relpath],
            message,
            prefer_deliberate_stage=True,
            blob_fallback=partial(hash_worktree_blobs_via_spawn, cwd=worktree_root),
        )
    except IndexStaleAfterCommit:
        pass  # landed; only the shared index is stale (precedes its base class)
    except (CommitRefused, FilterUnsupported, IndexWriteError) as exc:
        return {
            "acted": [],
            "skipped": [],
            "failed": [f"{OP_COMPLETION_ENTRY_FOLD}: git commit failed: {exc}"],
        }

    if push_mode == PUSH_MODE_SYNC:
        push_outcome = push_with_retry(
            worktree_root,
            budget_secs=_ceremony_push_budget(perf_counter() - _pre_push_elapsed),
        )
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

    Why this exists rather than `run()`: `run()` composes two post-commit
    legs and is `async`, and its only live caller is `/execute-plan`'s
    close-out. The close ceremony that actually WRITES completion entries
    (`workstream_complete.apply`) is synchronous, reaches no other
    leg, and — until this seam — had no way to fold its own commit into
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
# Composed run -- origin-stub close + completion-entry fold, in one call.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PostCommitTailOutcome:
    """Composed outcome of both post-commit tail legs; each result is a
    tail_ops-shaped `{acted, skipped, failed}` dict."""

    origin_stub_result: dict = field(
        default_factory=lambda: {"acted": [], "skipped": [], "failed": []}
    )
    completion_entry_fold_result: dict = field(
        default_factory=lambda: {"acted": [], "skipped": [], "failed": []}
    )


async def run(
    worktree_root: Path,
    common_dir: Path,
    sid: str,
    committed_sha: str,
    *,
    governing_plan_slug: str,
    initial_consumed: list[tuple[str, dict]],
    close_origin_stub_handler: Callable[[dict, Path], Awaitable[dict]],
    push_mode: str = PUSH_MODE_SYNC,
    timing: Optional[Any] = None,
    delivery_proof: Optional[dict] = None,
    completion_entry_path: Optional[str] = None,
) -> PostCommitTailOutcome:
    """Compose origin-stub close and the completion-entry commit-ledger fold
    into ONE in-process call. Runs UNLOCKED -- see module docstring HARD
    CONSTRAINT; the caller must not wrap this in a ceremony-wide lock.

    `close_origin_stub_handler` is caller-injected. `delivery_proof` is
    OPTIONAL and forwarded verbatim into `_run_origin_stub_close`; `None`
    preserves guard-only behaviour. `timing`, when supplied, records one
    span, "origin_stub_close". `completion_entry_path` is OPTIONAL: the entry
    path is the one piece the fold cannot derive itself (`apply.py`'s
    `{d-complete-entry.entry_path}` token substitution is the sanctioned
    resolver); `None` is a clean skip, not a failure.

    Both legs soft-fail inside their own helper -- neither propagates past
    this function.
    """
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

    # A clean skip, not a failed leg, when the caller has no entry path to
    # give (`close_out_and_stamp._reach_post_commit_tail_stub_close` passes none).
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
        origin_stub_result=origin_stub_result,
        completion_entry_fold_result=completion_entry_fold_result,
    )
