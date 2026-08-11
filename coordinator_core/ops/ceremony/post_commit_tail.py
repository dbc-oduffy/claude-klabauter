"""
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
across the repo boundary into the example-doctrine-repo skill occasion without re-deriving the
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
`_OP_TIMEOUT_OVERRIDES` entry (not long-tail — same global-30s-guard
precedent as `ceremony.wsc_tail` post-DEC-2, and this op does strictly less
work than that one); `coordinator_core/authz/classification.py` →
`OpClass.MUTATING` with the DR-208 five-question affirmation;
`coordinator_core/ops/__init__.py::_EAGER_OP_MODULES` eager-import entry;
`coordinator_core/ops/_registry_map.py::OP_MODULE_MAP` lazy-import entry.

Spec backlink: docs/plans/2026-07-23-wsc-tail-slim-down.md § C3a.

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

Negative-spec (hard-won):
  - Does NOT acquire any ceremony-wide serialization lock — see HARD
    CONSTRAINT above. Do not add one back; DEC-3 removed the hold around this
    op deliberately, and the 2026-08-07 PM ruling removed the underlying
    `ceremony_lock` mechanism entirely.
  - Does NOT move the `wsc_tail` call site — that is C3b, a separate chunk
    with its own PM-recorded fallback (moving invocation across the repo
    boundary into a example-doctrine-repo skill occasion needs a durable pending-work
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
"""

from __future__ import annotations

import logging
import sys
import tempfile
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from coordinator_core.dag import _read_meta
from coordinator_core.ipc import get_op_handler, register_op
from coordinator_core.ops.ceremony import consumed_handoff_stamp
from coordinator_core.ops.ceremony.commit_pipeline import (
    PUSH_MODE_SYNC,
    PUSH_STATUS_DECLINED,
    PUSH_STATUS_FAILED,
    PUSH_STATUS_NO_REMOTE,
    PUSH_STATUS_NOT_ATTEMPTED,
    PUSH_STATUS_PUSHED,
    derive_push_status,
    push_with_retry,
)
from coordinator_core.ops.ceremony.git_native import (
    commit_scoped,
    rev_parse_head,
)
from coordinator_core.ops.fleet._common import main_worktree_root

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
    worktree_root: Path, closed_paths: list[str], committed_sha: str, push_mode: str = PUSH_MODE_SYNC
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
        commit_result = commit_scoped(closed_paths, msg_path, worktree_root)
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

    if push_mode != PUSH_MODE_SYNC:
        return follow_up_sha, None, PUSH_STATUS_NOT_ATTEMPTED, None

    push_outcome = push_with_retry(worktree_root)
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
        post_push = rev_parse_head(worktree_root)
        landed_sha = post_push.stdout.strip() if post_push.ok else follow_up_sha
        return landed_sha, True, push_status, None
    if push_status == PUSH_STATUS_FAILED:
        reason = push_outcome.message or "; ".join(push_outcome.failed) or "unknown push failure"
        return follow_up_sha, False, push_status, f"git push failed: {reason}"
    # PUSH_STATUS_DECLINED / PUSH_STATUS_NO_REMOTE / PUSH_STATUS_NOT_ATTEMPTED
    # -- no push landed, but this is NOT an error (see docstring above): a
    # policy decline or a missing remote is `push_with_retry`'s own honest
    # "did not push, on purpose/by environment" outcome.
    return follow_up_sha, None, push_status, None


async def _run_origin_stub_close(
    worktree_root: Path,
    common_dir: Path,
    committed_sha: str,
    governing_plan_slug: str,
    initial_consumed: list[tuple[str, dict]],
    close_origin_stub_handler: Callable[[dict, Path], Awaitable[dict]],
    *,
    push_mode: str = PUSH_MODE_SYNC,
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
            result = await close_origin_stub_handler(
                {"plan_path": plan_path, "handoff_path": handoff_path, "sha": committed_sha},
                common_dir,
            )
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
            f"{s.get('roadmap_id')}:{s.get('stub_id')}:{s.get('reason')}"
            for s in (result.get("skipped") or [])
        )

    if not closed_by_stub:
        return {
            "acted": [],
            "skipped": skipped or [f"{OP_CLOSE_ORIGIN_STUB}:no-op"],
            "failed": failed,
        }

    closed_paths = list(closed_by_stub.keys())
    follow_up_sha, _pushed, follow_up_push_status, follow_up_error = await _to_thread_commit_and_push(
        worktree_root, closed_paths, committed_sha, push_mode
    )
    if follow_up_error:
        failed.append(f"follow-up: {follow_up_error}")
    elif follow_up_sha is None:
        # Should be unreachable (commit_result.ok implies a resolvable HEAD),
        # but never silently drop a genuine no-sha outcome as a clean success.
        failed.append("follow-up: commit landed but HEAD sha unresolved")
    elif follow_up_push_status in (PUSH_STATUS_DECLINED, PUSH_STATUS_NO_REMOTE):
        # A policy decline (or missing remote) is NOT an error -- see
        # `_commit_and_push_origin_stub_close`'s docstring. Named here
        # (skipped, never failed) purely for observability: the commit
        # itself landed, the push was deliberately withheld.
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
        else:
            skipped.append(
                f"{OP_DELIVERABLE_CASCADE}:{relpath}: {result.get('error', 'no downstream artifact advanced')}"
            )

    return {"acted": acted, "skipped": skipped, "failed": failed}


async def _to_thread_commit_and_push(
    worktree_root: Path, closed_paths: list[str], committed_sha: str, push_mode: str
) -> tuple[Optional[str], Optional[bool], str, Optional[str]]:
    """`asyncio.to_thread` wrapper around `_commit_and_push_origin_stub_close`
    -- split out purely so the import stays local to this call (AC6 event-
    loop hygiene, same rationale as `wsc_tail.py`'s own `to_thread` calls)."""
    import asyncio

    return await asyncio.to_thread(
        _commit_and_push_origin_stub_close, worktree_root, closed_paths, committed_sha, push_mode
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
) -> PostCommitTailOutcome:
    """Compose steps 5c (post-commit consumed-handoff stamp+ship), 5d
    (origin-stub close), and C6b's second trigger (deliverable cascade) into
    ONE in-process call. Runs UNLOCKED (DEC-3, and repo-wide since the
    2026-08-07 removal) -- see module docstring HARD CONSTRAINT; the caller
    must not wrap this in a ceremony-wide lock.

    `close_origin_stub_handler` is caller-injected (see module docstring
    "Origin-stub-close handler injection"). `cascade_handler` is OPTIONAL --
    when omitted, it is resolved here via `get_op_handler(OP_DELIVERABLE_CASCADE)`
    (never re-implemented), so existing callers (`wsc_tail.py`) that predate
    C6b need no call-site change. `timing`, when supplied, records the SAME
    two named spans as before C6b ("stamp_and_ship", "origin_stub_close") --
    see module docstring "Timing-span preservation"; the deliverable-cascade
    step deliberately runs untimed so as not to widen `wsc_tail.py`'s pinned
    step-name contract.

    All three steps run unconditionally in sequence -- a stamp+ship exception
    propagates BEFORE origin-stub close or the deliverable cascade ever run
    (matches the pre-extraction inline sequencing exactly: a crash mid-stamp
    on the fresh pass must leave the origin stub untouched, recovered only on
    the AC18-resumed re-invoke). An origin-stub-close failure or a
    deliverable-cascade failure, by contrast, is caught and soft-failed
    inside their own helper -- neither propagates past this function.
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
        )

    resolved_cascade_handler = cascade_handler
    if resolved_cascade_handler is None:
        resolved_cascade_handler = get_op_handler(OP_DELIVERABLE_CASCADE)

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

    return PostCommitTailOutcome(
        stamp_outcome=stamp_outcome,
        origin_stub_result=origin_stub_result,
        deliverable_cascade_result=deliverable_cascade_result,
    )


# ---------------------------------------------------------------------------
# JSON-RPC entry point -- standalone dispatch (never invoked by wsc_tail.py,
# which calls `run()` directly, in-process, injecting its own
# `_close_origin_stub_handler` module-global -- see module docstring).
# ---------------------------------------------------------------------------


@register_op(OP_NAME)
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC `ceremony.post_commit_tail` handler -- standalone dispatch
    entry point for this op. `wsc_tail.py` does NOT call this handler; it
    calls `run()` directly (see module docstring). This handler exists so the
    op is independently reachable via the registry, same convention as
    `handoff.close_origin_stub` remaining independently reachable after its
    own fold into `wsc_tail.py`'s step 5d.

    Parameters (params dict):
        sid                  (str, required)  -- WSC session id, forwarded to
                                `consumed_handoff_stamp.post_commit_stamp_and_ship`.
        committed_sha        (str, required)  -- the real post-commit SHA
                                (Position A -- never re-derived here).
        chain_terminal       (bool, optional, default False).
        governing_plan_slug  (str, optional)  -- origin-stub-close join key.
        initial_consumed     (list[[str, dict]], optional) -- consumed-handoff
                                pairs from the caller's own step-1 resolve;
                                first entry is the origin-stub-close join
                                fallback.
        push_mode            (str, optional, default PUSH_MODE_SYNC).

    Returns `{exit_code, stamped, empty_consumed_set, follow_up_committed_sha,
    follow_up_pushed, origin_stub_close, deliverable_cascade}` on success;
    `{exit_code: 1, error}` on a setup error (missing required param,
    unregistered `handoff.close_origin_stub`, no `repo_root`).
    `deliverable.cascade_terminal` (C6b) is resolved on a best-effort basis --
    if it is not registered, `deliverable_cascade` comes back a clean
    `{"acted": [], "skipped": [...], "failed": []}`, not a setup error.
    """
    sid = str(params.get("sid") or "")
    if not sid:
        return {"exit_code": 1, "error": f"{OP_NAME}: 'sid' param is required"}

    committed_sha = str(params.get("committed_sha") or "")
    if not committed_sha:
        return {"exit_code": 1, "error": f"{OP_NAME}: 'committed_sha' param is required"}

    if repo_root is None:
        return {
            "exit_code": 1,
            "error": f"{OP_NAME}: repo_root arg is None -- common_dir not supplied by engine",
        }

    common_dir = Path(repo_root)
    worktree_root = main_worktree_root(common_dir)
    chain_terminal = bool(params.get("chain_terminal", False))
    governing_plan_slug = str(params.get("governing_plan_slug") or "")
    initial_consumed_raw = params.get("initial_consumed") or []
    initial_consumed = [(str(p), dict(fm)) for p, fm in initial_consumed_raw]
    push_mode = str(params.get("push_mode") or PUSH_MODE_SYNC)

    handler = get_op_handler(OP_CLOSE_ORIGIN_STUB)
    if handler is None:
        return {"exit_code": 1, "error": f"{OP_NAME}: {OP_CLOSE_ORIGIN_STUB} not registered"}

    outcome = await run(
        worktree_root,
        common_dir,
        sid,
        committed_sha,
        chain_terminal=chain_terminal,
        governing_plan_slug=governing_plan_slug,
        initial_consumed=initial_consumed,
        close_origin_stub_handler=handler,
        push_mode=push_mode,
    )

    has_failure = (
        bool(outcome.stamp_outcome.errors)
        or bool(outcome.origin_stub_result.get("failed"))
        or bool(outcome.deliverable_cascade_result.get("failed"))
    )

    return {
        "exit_code": 2 if has_failure else 0,
        "stamped": outcome.stamp_outcome.stamped,
        "empty_consumed_set": outcome.stamp_outcome.empty_consumed_set,
        "follow_up_committed_sha": outcome.stamp_outcome.follow_up_committed_sha,
        "follow_up_pushed": outcome.stamp_outcome.follow_up_pushed,
        "origin_stub_close": outcome.origin_stub_result,
        "deliverable_cascade": outcome.deliverable_cascade_result,
    }
