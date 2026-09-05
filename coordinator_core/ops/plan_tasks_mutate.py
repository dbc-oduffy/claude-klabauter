"""
coordinator_core.ops.plan_tasks_mutate — plan `## Tasks` task-spine mutation op
(plan.tasks.mutate).

Purpose: authoritative-mutation engine for the plan `## Tasks` task-spine —
verbs `add-task` (append a chunk row, fail-loud on duplicate `id`), `stamp`
(update fields across multiple ids in one atomic `locked_rmw`), and `resolve`
(write a row's `disposition`/`disposition_ref`/`disposition_detail` atomically,
refusing an ungated closed disposition — D4). Zero-spawn hot-path: validates
rows in-process against the vendored `_PLAN_TASKS_SCHEMA`
(coordinator_core.frontmatter.schema_validate) — no runtime shell-out to DoE's
schema-cli.js.

Spec backlink: pln-pcli-need-1-plan-tasks-engine--53c00d § C3
    (add-task/stamp); docs/plans/2026-07-27-plan-line-item-resolution-model.md
    § C4 (resolve verb + stamp's reserved-field refusal); § C5 (resolve
    --backlogged delegates to coordinator-harvest-deferrals' own row-routing).

Verb contracts:

  add-task (params: plan_path, task) — appends `task` (a dict) as a new row
    to the `## Tasks` fenced block. Duplicate `id` -> MutateAbort, no write.
    `absent` spine (no fence located):
      - `## Tasks` heading exists, no adjacent fence -> fence inserted under
        the EXISTING heading (no second heading created).
      - No `## Tasks` heading at all -> a fresh `## Tasks` section + fence is
        synthesized at the end of the document.
    `malformed` spine (>1 fence, or fence not adjacent to heading) -> MutateAbort.

  stamp (params: plan_path, updates) — `updates` is a list of
    `{"id": <task id>, ...field-updates}` dicts; every named id must exist in
    the current spine and every row THIS BATCH WRITES must validate, or NONE
    of the updates are applied (all-or-nothing, one callback, one lock). A
    pre-existing invalid row this batch does not touch does not veto the
    write (2026-08-16 fix — see `_validate_all`'s docstring); it is instead
    surfaced in the reply's `warnings` list, naming its id, so a repair
    cannot silently normalize a broken spine into looking fine. Duplicate
    `id` within one `updates` batch -> MutateAbort, no write (F2; mirrors
    add-task's fail-loud-dup discipline rather than last-write-wins).
    `absent` spine -> MutateAbort regardless of heading/fence sub-case
    (nothing to stamp). `malformed` spine -> MutateAbort. RESERVED FIELDS
    (2026-07-27, D4): if ANY update entry in the batch names `disposition`,
    `disposition_ref`, or `disposition_detail`, the WHOLE batch is refused
    with an offer to use `--verb resolve` instead — those three fields are
    resolve's surface exclusively, so stamp cannot be used as a side door
    around resolve's pm_approved gate.

  resolve (params: plan_path, id, disposition, disposition_ref,
    disposition_detail — OR plan_path, resolves: [{id, disposition,
    disposition_ref, disposition_detail}, ...] for a BATCH) — writes N
    rows' `disposition` (required per row) plus optionally
    `disposition_ref` / `disposition_detail` atomically, in ONE
    `locked_rmw` transaction. Single-row resolve (the `id`/`disposition`
    param shape) is a batch of one — see `_handler`'s param-mapping. Batch
    resolve (C13, 2026-07-30) exists because a PM's grouping approval
    (below) ratifies a SET of rows in one motion, but the pre-batch
    `resolve` only ever closed one row per call — so a two-row approved
    cut-set was structurally unreachable: closing row 1 alone made the
    prospective membership `{row 1}`, which never matches a digest approved
    over `{row 1, row 2}`, and closing row 2 alone (after row 1 already
    failed) never runs. Batching multiple rows into ONE write is what makes
    the prospective membership equal the approved set at all.

    Refuses (MutateAbort, no write, whole batch aborts) a PM-GATED
    disposition on any row in the batch unless the plan's own authorization
    signal clears — mirrors `coordinator_core.ops.handoff_carry_gate`'s
    refuse-on-ungated-state pattern (D4). WHICH dispositions are PM-gated
    depends on the mode: GOVERNED gates `backlogged`/`wont_do`/`spun_off`
    (`_PLAN_TASKS_GOVERNED_PM_APPROVAL_GATED_DISPOSITIONS`), LEGACY gates
    `backlogged`/`wont_do` only (`_PLAN_TASKS_PM_APPROVAL_GATED_
    DISPOSITIONS`).

    `spun_off`'s history is why those are two sets and not one. DoE's
    2026-08-05 ruling took it out of the gate entirely ("the EM self-issues
    it now"), reasoning that moving a row to another plan drops no work.
    DR-183 (2026-08-29) reversed that. The reversal could not be honoured
    until `grouping_approvals` carried a `spun_off` key, since gating a
    grouping with no approval block makes every governed-plan `spun_off`
    resolve permanently unsatisfiable rather than merely PM-gated; the key
    could not originate here either, plan.schema.json being vendored
    byte-for-byte from DoE-claude under `check_schema_drift`. It arrived on
    2026-08-30 with plan.schema.json 2.13.0, and
    `check_plan_tasks_grouping_approval` widened that day. This gate did NOT
    — it kept keying on the legacy frozenset for both legs until 2026-09-04,
    so a governed `spun_off` close succeeded here and produced a record the
    lint then refused. See `check_plan_tasks_grouping_approval` for the full
    sequence and for why the legacy leg must stay narrow.

    Which signal clears the two PM-gated dispositions depends on the plan
    (2026-07-29 grouping-approval contract; see `is_governed_plan`):
      - GOVERNED (frontmatter carries a `grouping_approvals` key at all —
        bare presence, no schema_version conjunct): EVERY grouping
        (`defer`/`ruled_out`, from `_PLAN_TASKS_GROUPING_BY_DISPOSITION`)
        touched by `backlogged`/`wont_do` rows in the batch must have a
        block reading `status: approved`, AND that block's `digest` must
        match a fresh `compute_grouping_digest` recomputation over the
        membership the WHOLE BATCH is about to produce — not a per-row
        recomputation. A batch may span groupings (one row to `defer`,
        another to `wont_do`/`ruled_out` in the same call); each affected
        grouping is checked independently, against its own approval block
        and its own prospective membership, but the prospective membership
        itself is always computed with every row in the batch applied at
        once — the cut-set the PM approved, not the pre-write one, and not
        a one-row-at-a-time slice of it; checking a narrower set would
        refuse the very first application of a freshly approved multi-row
        cut.
      - LEGACY (no `grouping_approvals` key): each such row's existing
        `pm_approved` field must be `True`, checked per row, no batching
        semantics apply (legacy plans have no groupings).
    `coded` is NOT closed and needs no authorization in either mode (D3).
    The GOVERNED refusal names NO command — this is deliberate, not an
    omission: an earlier version worded the refusal as an offer naming what
    would satisfy it (e.g. "stamp pm_approved: true first"), which is the
    write guard's own key printed back at whoever hit the gate — see the
    retired `_PM_APPROVAL_OFFER` banner below for the full excision. The
    LEGACY refusal DOES name `pm_approved`, and must (DoE ruling 2026-08-12,
    exit 1): the excision's reasoning holds only where the impossibility
    claim is true, and on a per-row boolean the same agent can stamp it
    never was — see `_LEGACY_PM_APPROVAL_HINT`'s own banner for why naming
    the field is the honesty layer rather than a re-offer. `resolve` does
    NOT itself grant authorization in either mode: a GOVERNED plan's
    `grouping_approvals` blocks are authored and approved by the PM
    directly in the plan's frontmatter, outside this op's surface entirely;
    a LEGACY plan's `pm_approved` is set via a separate `stamp` call (it is
    not one of the three reserved fields) — reflecting the read that PM
    ratification is a distinct, already-existing gate this verb checks, not
    one it grants. `resolve`
    also refuses `spun_off`/`backlogged` lacking a non-empty
    `disposition_detail` (Defect 2 fix, 2026-07-27): a synthesised detail
    (e.g. "routed to <disposition_ref>") would only restate the ref, adding
    no information — so the caller must supply real PM-reasoning prose,
    with the same offer-shaped refusal voice as the pm_approved gate.
    `wont_do` needs no separate check here (no `disposition_ref` to pair a
    detail with; the vendored schema already hard-requires its detail).

    REPOSITIONING (2026-08-06, D5 ordering-deadlock fix — queue
    state/bug-backlog/2026-08-06-plan-tasks-mutate-d5-ordering-deadlocks-
    c223a7208a5a.yaml): once every `disposition` field in the batch is
    written, `resolve` repositions the WHOLE spine into D5's required
    grouping order (`_reposition_rows_for_d5`) as part of the same write,
    rather than leaving row position untouched and merely checking it. A
    disposition change IS what determines which grouping a row belongs to
    (`_PLAN_TASKS_GROUPING_BY_DISPOSITION`); a row's position never moving
    on a disposition change, combined with a PRE-write refusal whenever
    the spine's existing order already violated D5, made some single-row
    transitions unsatisfiable in EITHER direction — e.g. closing a spine's
    last open row to `wont_do`/`backlogged` once every earlier row is
    already `coded` (an entirely ordinary spine, reached by coding rows in
    forward order): left in place, the do-suborder rule (open must sort
    above coded) already called that spine invalid, so the OLD pre-write
    check refused every subsequent resolve call on it outright, regardless
    of what the call was trying to do; hoisted to the front by a hand-edit
    to dodge that, the OLD post-write check refused instead (a `ruled_out`
    row may not sort above a `do` row). With no un-resolve verb and no
    reorder verb, no ordering satisfied both checks, and the plan's own
    doc comment used to advise "resolve in reverse spine order" as the
    workaround — which does not help THIS transition, since the deadlocked
    row is the one that must close last.

    The fix removes the now-unsatisfiable half of that old contract: the
    PRE-write precondition on `old_text`'s existing order is retired (see
    the retired-banner comment at its old call site, just before `rows` is
    parsed, for the full excision) rather than kept alongside
    repositioning, because repositioning makes it BOTH unnecessary (the
    write it used to gate can no longer land in an invalid state) and
    actively harmful (it is exactly the check that fired first in the
    deadlock above, before repositioning ever got a chance to run).
    Removing it does not relax D5's invariant on the RESULTING spine —
    only the precondition on the spine's PRE-existing state is gone; the
    post-mutation check below still enforces the invariant on what
    `resolve` actually writes.

    `_reposition_rows_for_d5` is a STABLE sort (Python's `sorted()`) keyed
    by the same `(grouping rank, do-suborder rank)` tuple
    `check_plan_tasks_ordering` itself computes — never an independent
    reimplementation, so the two can never disagree about what "correct
    order" means. Stability is what keeps this narrow: a stable sort only
    ever reorders rows whose RANK differs; two rows that already shared a
    rank (same grouping, same do-suborder) keep their existing relative
    order untouched, so a batch that touches one row's disposition can
    relocate that row without shuffling any row the batch did not touch —
    an untouched row's rank never changes, so its position relative to
    every OTHER untouched row is preserved exactly, regardless of what the
    spine's order was before this call.

    A post-mutation check (C13, 2026-07-30) still runs
    `check_plan_tasks_ordering` against the batch's real, now-repositioned
    `rows` before any dispatch side effect fires — retained as a defensive
    invariant assertion (the repositioning above makes it unreachable in
    ordinary operation; a stable sort by rank cannot itself produce an
    invalid order), not as the correctness mechanism itself. It runs AFTER
    every row's `disposition` field is written and the spine repositioned,
    but BEFORE any dispatch side effect (`_dispatch_backlogged`/
    `_dispatch_spun_off`) fires, not on the rendered `new_text` afterward:
    `locked_rmw` covers the spine write only, so a refusal raised after
    `_dispatch_backlogged` had already appended a queue/lesson entry would
    leave that entry on disk describing a deferral the spine never
    recorded. Checking the real mutated+repositioned `rows` (rather than a
    synthetic prospective copy, and rather than a post-dispatch
    postcondition on `new_text`) is deliberate: it is the one placement
    that stays safe if a future change makes `_reposition_rows_for_d5`
    fallible in some case not yet imagined — a check that ran after
    dispatch would reproduce the exact orphaned-harvest-entry defect
    `test_resolve_d5_refusal_fires_before_any_harvest_dispatch` exists to
    prevent. Raises MutateAbort with zero write on failure.

    ATOMICITY (C13, 2026-07-30): every check above — id existence,
    duplicate-id-in-batch, grouping/pm_approved authorization (per
    affected grouping, or per row for LEGACY), disposition_detail
    presence — runs for the WHOLE batch BEFORE any row is mutated or any
    `backlogged`/`spun_off` dispatch side effect fires, exactly mirroring
    the single-row gate-before-dispatch ordering this verb already used.
    Only once every entry in the batch clears every gate does any row
    mutate: every `disposition` field in the batch is written first, then
    the post-mutation D5 check runs, and only after THAT passes does the
    dispatch loop run (`backlogged`/`spun_off` side effects + `disposition_
    ref`/`disposition_detail` writes), followed by one
    `_validate_all(rows, governed=...)` call and one `_dump_rows`
    serialization. Any MutateAbort raised anywhere in this sequence — pre-
    or post-mutation — propagates out of the `mutate` closure untouched,
    so `locked_rmw` never calls back with a mutated string and the file on
    disk is left byte-identical (F1's round-trip-fidelity boundary already
    guarantees this for any raised MutateAbort; batching does not change
    that contract, it only widens what a single `mutate` call attempts).

    `disposition == "backlogged"` (C5, 2026-07-27): once the pm_approved
    gate above passes, resolve DELEGATES row-routing to
    `coordinator/bin/coordinator-harvest-deferrals`'s own dispatch
    functions — loaded in-process (see `_load_harvest_module`), never
    re-implemented — inheriting that CLI's change_kind split
    (9-value project-tier subset -> coordinator-queue-append
    --schema improvement-queue; {doctrine-edit, snippet-sync-update} ->
    coordinator-lesson-promote) and its `(plan_id, row id)` idempotency key
    verbatim. The `disposition_ref` param supplied by the caller is IGNORED
    for `backlogged` — it is fully computed from the harvest dispatch's own
    result (the located queue/lesson entry's path) so the two writes (spine
    row + harvest entry) can never disagree about which file the row
    resolved to. One `resolve --backlogged` call is one operation from the
    caller's point of view: the disposition write and the queue/lesson
    write both land, or neither does (MutateAbort on any harvest-dispatch
    failure aborts before the disposition write). See `_dispatch_backlogged`.

    `disposition == "spun_off"` (C12, 2026-07-29): the pm_approved/grouping
    gate above passes the same way, then resolve VERIFIES the caller-
    supplied `disposition_ref` rather than recording it unchecked — it must
    resolve to a file that actually exists (the spinoff artifact `/spinoff`
    already created before this call), and the recorded ref is the
    re-derived canonical repo-relative form, not the caller's literal
    string. Unlike `backlogged`, resolve does not create the artifact here
    (that write already landed) — it only refuses to record a pointer to
    something that isn't really there. See `_dispatch_spun_off`.

Every verb shares ONE `locked_rmw(plan_path, mutate, repo_root=...)` call —
the mutate closure never calls locked_rmw re-entrantly.

Round-trip fidelity boundary (F1): byte-preservation applies to everything
OUTSIDE the fence-body span (frontmatter, surrounding prose, fence markers).
INSIDE the span, `safe_load` -> `safe_dump` normalization of comments/
key-order/quoting on every mutation is an ACCEPTED loss — the `## Tasks`
spine is machine-owned. Dump options (`sort_keys=False,
default_flow_style=False, allow_unicode=True, width=4096`) are pinned so
serialization is deterministic and diff-stable across invocations, which is
also what gives idempotency (F6) for free via `locked_rmw`'s byte-identity
skip — no separate semantic dict-compare path is needed.

Self-registration: importing this module fires @register_op("plan.tasks.mutate")
as a side-effect. Add the import to coordinator_core/ops/__init__.py to trigger
registration at start_server() time.

Negative-spec:
  - Does NOT re-implement the fenced-block locate rule inline — delegates to
    coordinator_core.frontmatter.body_blocks.locate_fenced_block exclusively.
  - Does NOT mutate plan frontmatter or any body text outside the fence-body
    span — this op is a body-block RMW, not a frontmatter transition; the
    memo/handoff replace_fm_field helpers do not apply here.
  - Does NOT support verbs beyond add-task / stamp / resolve (no
    delete/dedup, and no standalone reorder verb — `resolve`'s own D5
    auto-repositioning, 2026-08-06, moves ONLY the rows a call itself
    just closed, into their disposition-derived grouping position; it is
    not a general row-reorder facility a caller can invoke independently
    of changing a disposition).
  - Does NOT re-implement coordinator-harvest-deferrals' change_kind ->
    queue/lesson routing table for `resolve --backlogged` — loads and calls
    that CLI's own dispatch functions in-process (`_load_harvest_module`;
    C5) rather than duplicating the mapping.
  - Does NOT independently re-derive which allOf branches require
    `pm_approved`. Correction (2026-07-29, write-guard-bypass fix): this
    bullet used to claim `_validate_json_schema_node` "does not support
    allOf/if/then, so that block is silently ignored by design" — that was
    simply wrong. `_validate_json_schema_node` DOES evaluate allOf and
    if/then (one level deep; see schema_validate.py's own module-docstring
    "supported keywords" list), so the vendored schema's two
    pm_approved-required branches (deferred=>pm_approved, and the three
    CLOSED dispositions=>pm_approved) DO run at the schema layer whenever
    the raw `_PLAN_TASKS_SCHEMA_DICT` is used — this was the actual root
    cause of a defect where both write guards rejected closed rows on
    GOVERNED plans that the grouping-approval predicate had already
    cleared. `_validate_row` selects `_PLAN_TASKS_SCHEMA_GOVERNED_DICT`
    (both branches' `required: [pm_approved]` stripped) for governed rows
    precisely so this schema-layer presence check can never re-reject a
    row the grouping predicate already approved. That filtered schema is
    derived in `coordinator_core.frontmatter.schema_validate`
    (`_plan_tasks_schema_without_pm_approved_required`), not hand-copied
    here, and the two write guards share the identical derivation. For
    LEGACY rows the branches stay live; their presence-only requirement is
    redundant with, not a substitute for,
    `_cf_plan_tasks_disposition_shape`'s stronger truthiness check
    (`pm_approved` must equal `True`, not merely be present) — enforcing
    the actual PM-ratification semantics is that cross-field rule's job,
    not this schema branch's. Ratification of DEFERRED rows specifically
    (the first allOf branch, orthogonal to the closed-disposition gate
    above) remains a downstream (plan-coverage-checker) concern, not an
    add-time gate — that branch's presence-only requirement is likewise
    non-hard-failing by the vendored schema's own $comment.
  - Does NOT call locked_rmw re-entrantly within one op invocation.
"""

from __future__ import annotations

import asyncio
import glob
import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType
from typing import Optional

import yaml

_LOG = logging.getLogger(__name__)

from coordinator_core.frontmatter.body_blocks import (
    LocateStatus,
    _compile_heading_re,
    locate_fenced_block,
)
from coordinator_core.frontmatter.schema_validate import (
    _apply_cross_field_rules,
    _PLAN_TASKS_PM_APPROVAL_GATED_DISPOSITIONS,
    _PLAN_TASKS_GOVERNED_PM_APPROVAL_GATED_DISPOSITIONS,
    _GROUPING_APPROVAL_HINT,
    _PLAN_TASKS_GROUPING_BY_DISPOSITION,
    _PLAN_TASKS_GROUPING_ORDER,
    _PLAN_TASKS_SCHEMA_DICT,
    _PLAN_TASKS_SCHEMA_GOVERNED_DICT,
    _PLAN_TASKS_SUBORDER_BY_DISPOSITION,
    _plan_tasks_row_disposition,
    _validate_json_schema_node,
    check_plan_tasks_ordering,
    compute_grouping_digest,
    format_validation_errors,
    is_governed_plan,
    parse_frontmatter,
)
from coordinator_core.ipc import register_op
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.wire_paths import rel_id

# ---------------------------------------------------------------------------
# Reply helpers
# ---------------------------------------------------------------------------


def _ok(applied: bool, message: str, *, warnings: Optional[list] = None) -> dict:
    """Return exit_code=0 reply.

    `warnings` (2026-08-16, untouched-invalid-row deadlock fix): a
    (possibly empty/omitted) list of diagnostic strings surfaced alongside
    a successful write — currently used to name pre-existing invalid rows
    this mutation did NOT touch and therefore did not veto, mirroring the
    `warnings` list shape `completion_ops.py` already establishes for
    non-fatal diagnostics riding alongside a successful reply. Omitted
    entirely (not an empty list) when there is nothing to report, so an
    existing caller reading `applied`/`message` only sees no shape change.
    """
    reply = {"exit_code": 0, "applied": applied, "message": message}
    if warnings:
        reply["warnings"] = warnings
    return reply


def _err(message: str) -> dict:
    """Return exit_code=1 reply (error; no write performed)."""
    return {"exit_code": 1, "applied": False, "error": message}


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


class _PathNotContained(Exception):
    """Raised by _resolve_path when plan_path escapes docs/plans/.

    Mirrors handoff_transition.py::_PathNotContained (F0). Raised by
    _resolve_path BEFORE locked_rmw is invoked; caught immediately outside the
    mutate closure and mapped to {"exit_code": 1, "applied": False, "error": ...}
    — the same exit_code=1/no-write outcome MutateAbort produces, but via a
    dedicated exception rather than overloading MutateAbort for a pre-lock
    path-resolution failure.
    """


def _resolve_path(plan_path: str, worktree: Path) -> Path:
    """Resolve plan_path to an absolute Path, contained under docs/plans/.

    Absolute path -> used as-is. Relative path -> resolved against worktree
    root (e.g. docs/plans/foo.md becomes <worktree>/docs/plans/foo.md).

    Containment (F0): the resolved path MUST be under <worktree>/docs/plans/.
    Raises _PathNotContained if the resolved path escapes that root.
    """
    p = Path(plan_path)
    if not p.is_absolute():
        p = worktree / p
    allowed_roots = [worktree / "docs" / "plans"]
    resolved = contained_path(p, allowed_roots)
    if resolved is None:
        raise _PathNotContained(f"plan_path escapes docs/plans/: {plan_path!r}")
    return resolved


# ---------------------------------------------------------------------------
# Spine synthesis (add-task absent-spine sub-cases — F3)
# ---------------------------------------------------------------------------

_TASKS_HEADING_LINE = "## Tasks"
_FENCE_OPEN = "```yaml plan-tasks\n"
_FENCE_CLOSE = "\n```"


def _has_tasks_heading(source: str) -> bool:
    # Review: code-reviewer — reuse body_blocks._compile_heading_re instead of
    # a third hardcoded copy of the same heading regex; hoisted `import re` to
    # module scope (F4).
    return _compile_heading_re("Tasks").search(source) is not None


def _synthesize_fence_under_heading(source: str, body_yaml: str) -> str:
    """Insert a fresh fence directly under the existing '## Tasks' heading.

    Requires a '## Tasks' heading to already exist with no adjacent fence
    (LocateStatus.ABSENT sub-case). Inserts the fence immediately after the
    heading line, preserving everything else byte-for-byte.
    """
    heading_re = _compile_heading_re("Tasks")
    match = heading_re.search(source)
    assert match is not None  # caller has already verified the heading exists
    insert_at = match.end()
    fenced = f"\n\n{_FENCE_OPEN}{body_yaml}{_FENCE_CLOSE}\n"
    return source[:insert_at] + fenced + source[insert_at:]


def _synthesize_tasks_section(source: str, body_yaml: str) -> str:
    """Append a fresh '## Tasks' section + fence at the end of the document.

    Used only when no '## Tasks' heading exists at all.
    """
    # Review: code-reviewer — collapsed dead-branch ternary (middle and final
    # arms both produced "\n\n"; only two distinct outcomes exist) (F1/F6).
    separator = "" if source.endswith("\n\n") else "\n\n"
    section = f"{_TASKS_HEADING_LINE}\n\n{_FENCE_OPEN}{body_yaml}{_FENCE_CLOSE}\n"
    return source + separator + section


# ---------------------------------------------------------------------------
# Row validation
# ---------------------------------------------------------------------------


def _validate_row(row: dict, *, governed: bool = False, plan_created: Optional[str] = None) -> list:
    """Validate a single task row against the vendored base per-row shape
    PLUS the plan-tasks cross-field rules (DR-103 defect fix, 2026-07-29).

    Calls _validate_json_schema_node directly — NOT validate_frontmatter —
    for the base per-row shape (F5's own reasoning still applies unchanged:
    this module never has a schema_name-keyed dict to hand
    validate_frontmatter, only the bare vendored schema), passing
    _PLAN_TASKS_SCHEMA_DICT as both schema and root_schema for a LEGACY row,
    or its governed-filtered variant, _PLAN_TASKS_SCHEMA_GOVERNED_DICT, when
    `governed=True` (see the closing paragraph below for why the two
    diverge).

    ALSO runs `_apply_cross_field_rules(row, 'plan-tasks')` — the
    REGISTERED dispatch (`schema_validate._CROSS_FIELD_RULES_BY_SCHEMA
    ['plan-tasks']`), never the private `_cf_plan_tasks_disposition_shape`
    function imported directly, so the vendored schema's own $comment
    blocks (which document that hard-failing disposition-shape enforcement
    lives behind this registration, not in the schema itself) stay true.
    Before this fix, `_cf_plan_tasks_disposition_shape` had zero production
    callers — every verb here validated shape only, so a row could reach
    disk with e.g. `disposition: coded` and no `disposition_detail`, in
    direct violation of DR-103 ("`disposition_detail` holds prose and is
    required on every non-open row"). `close_out_and_stamp.py`'s own
    auto-resolve producer is fixed in the same change to always pair a
    `disposition_detail` with its `coded` stamp, so this newly-enforced gate
    does not red the very rows that op writes.

    Errors from both legs are merged into ONE list in the same ErrorDict
    shape, so `format_validation_errors` renders either source unchanged.
    Returns a (possibly empty) list of error dicts.

    `_PLAN_TASKS_SCHEMA_GOVERNED_DICT`/`_PLAN_TASKS_SCHEMA_DICT` live in
    `coordinator_core.frontmatter.schema_validate` (moved there 2026-07-29),
    not here. That module's `check_plan_tasks_source` is NOT a shared
    validation entrypoint for this logic — it has zero production callers
    repo-wide (one test only), and the write guards each inline their own
    copy of this same shape-then-cross-field sequence instead of calling it.
    Nor can they: `check_plan_tasks_source` hardcodes claude-klabauter's own vendored
    schema, while the write guards deliberately resolve DoE's vendored
    corpus copy (which its own docstring notes has drifted from claude-klabauter's),
    and it short-circuits on the first error where the guards need every
    row's errors. So this is genuinely THREE independent copies of
    governed-aware per-row validation — this one, the write-guard copy, and
    `check_plan_tasks_source` — not one shared implementation. What IS
    shared, and does keep the copies from disagreeing about MEANING, are the
    low-level primitives each copy calls:
    `_plan_tasks_schema_without_pm_approved_required` (the governed schema
    derivation), `is_governed_plan`, and `_apply_cross_field_rules` — a row
    cannot be "governed" in one copy and "legacy" in another. But the
    validation SEQUENCE itself — which schema to pick, when to run
    cross-field rules, how to merge the two error lists — is duplicated
    three times, and nothing enforces the three sequences stay in lockstep
    if one of them changes.
    """
    schema = _PLAN_TASKS_SCHEMA_GOVERNED_DICT if governed else _PLAN_TASKS_SCHEMA_DICT
    errors = _validate_json_schema_node(row, schema, schema)
    errors.extend(_apply_cross_field_rules(
        row, "plan-tasks", governed=governed, plan_created=plan_created,
    ))
    return errors


def _validate_all(
    rows: list,
    *,
    governed: bool = False,
    touched_ids: Optional[set] = None,
    plan_created: Optional[str] = None,
) -> list:
    """Validate rows in `rows`; raise MutateAbort on the first invalid TOUCHED
    row. Returns the list of ids of pre-existing invalid rows this call did
    NOT touch (untouched-invalid diagnostic — empty list when there are
    none), so a caller can surface them as a warning without vetoing the
    write.

    Review: code-reviewer — shared uniformly by add-task's ABSENT and LOCATED
    branches and by stamp's post-update validation loop (F3), replacing what
    was previously an asymmetric shape (ABSENT validated only the single new
    `task`, relying on `rows == []` making that equivalent to validating the
    whole `new_rows` list — correct today by coincidence, not by invariant).

    `governed` is PLAN-scoped and must be resolved by the caller from the
    plan's frontmatter — no row can answer it. Default False is the legacy
    predicate, i.e. today's behaviour exactly. `plan_created` (2026-08-19
    fix) is likewise PLAN-scoped — the plan document's own `created`
    frontmatter field, forwarded through to `_cf_plan_tasks_writes_declared`
    (see that rule's docstring) so a hand-authored open row missing
    `writes` is actually caught here, not only at `dispatch.emit`'s
    preflight. Every verb below resolves it from `old_text`'s own
    frontmatter, mirroring how `_resolve` already resolves `governed`.

    `touched_ids` (2026-08-16, untouched-invalid-row deadlock fix — queue
    entry for the live repro: two rows each schema-invalid for reasons
    unrelated to the call in question deadlocked each other's repair,
    because this function used to validate and veto on EVERY row in the
    spine regardless of whether the mutation touched it. A row this
    mutation did not write could not have made that row worse, so it must
    not be able to block the write. `None` (the default) preserves the old
    "validate every row, veto on any" behaviour for any caller that has not
    been updated to pass it — every in-repo caller below now passes the
    ids it actually wrote. A row IS "touched" if its id is in the set;
    every row this call itself just wrote must always be in that set, so
    invariant (1) — this op may never WRITE a newly-invalid row — is
    unchanged.
    """
    untouched_invalid: list = []
    for row in rows:
        errors = _validate_row(row, governed=governed, plan_created=plan_created)
        if not errors:
            continue
        row_id = row.get("id")
        if touched_ids is None or row_id in touched_ids:
            details = format_validation_errors(errors)
            raise MutateAbort(f"schema-invalid row {row_id!r}: {details}")
        untouched_invalid.append(row_id)
    return untouched_invalid


def _untouched_invalid_warnings(untouched_invalid: list) -> list:
    """Render `_validate_all`'s untouched-invalid-row id list as the
    `warnings` list `_ok` surfaces alongside a successful write — so a
    repair does not silently normalize a broken spine into looking fine
    (requirement 4 of the 2026-08-16 deadlock fix). Empty list in, empty
    list out — `_ok` omits the key entirely when this is falsy.
    """
    if not untouched_invalid:
        return []
    ids = ", ".join(repr(i) for i in untouched_invalid)
    return [
        f"pre-existing schema-invalid row(s) not touched by this call, still "
        f"invalid on disk: {ids} — this write did not veto on them because "
        "they were not part of the mutation, but they remain broken; resolve "
        "each with its own call."
    ]


class _PlanTasksDumper(yaml.SafeDumper):
    """SafeDumper subclass carrying the literal-block-scalar `str` representer
    below. A SUBCLASS, never `yaml.SafeDumper` itself (2026-08-21 fix,
    row-body-flattening defect) — the engine process is warm and long-lived,
    and every other op that dumps YAML via the module-level `SafeDumper`
    shares that same class object; registering a representer on it directly
    would leak this literal-style choice into every other op's dump calls,
    not only this one's.
    """


def _plan_tasks_str_representer(dumper: yaml.Dumper, data: str):
    """Emit any multi-line string (a row's `body:` field, chiefly) as a
    literal block scalar (`|`) instead of PyYAML's default double-quoted
    single line with embedded `\\n` escapes (2026-08-21 fix).

    Before this fix, `_dump_rows` re-serialized every row in the spine on
    EVERY mutation (F1's accepted normalization loss), and PyYAML's default
    `str` representer picks double-quoted style for any string containing a
    literal newline. On a multi-paragraph `body:` field that meant a single
    stamp/resolve call touching one row flattened every OTHER untouched
    row's `body` into one very long double-quoted line — observed on a real
    17-row plan as a 297-insertion/629-deletion diff for a one-row edit.
    Content was never lost (the flattened form round-trips through
    `safe_load` byte-for-byte-equivalent), but it destroyed line-level
    diffing and made a 3,000-character row body unreadable as one line —
    exactly the failure mode this representer exists to remove.

    Falls through to PyYAML's own default scalar style (whichever it picks
    — plain, single-, or double-quoted) for any string with no embedded
    newline, so this only ever changes MULTI-LINE strings, never single-line
    field values. A string containing a newline AND trailing whitespace on
    some line cannot round-trip as `style='|'` (PyYAML falls back to quoted
    form itself in that case) — this representer does not special-case that,
    it relies on PyYAML's own fallback rather than stripping content to
    force a style.
    """
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_PlanTasksDumper.add_representer(str, _plan_tasks_str_representer)


def _dump_rows(rows: list) -> str:
    """Re-serialize the row list per the pinned dump options (F1).

    Uses `_PlanTasksDumper` (not the bare `yaml.safe_dump`/`SafeDumper`) so a
    row's `body:` field round-trips as a literal block scalar instead of a
    flattened, escaped single line — see `_plan_tasks_str_representer`.
    """
    return yaml.dump(
        rows,
        Dumper=_PlanTasksDumper,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=4096,
    )


# ---------------------------------------------------------------------------
# add-task
# ---------------------------------------------------------------------------


def _parse_rows_or_abort(body: str, verb: str) -> list:
    """Parse a LOCATED spine body, refusing the write when it does not parse.

    `locate_fenced_block` blanks HTML comments length-preservingly for its
    own scan but slices `body` from the ORIGINAL source, so a comment (or
    any other malformation) written INSIDE the fence locates cleanly and
    only fails here. Left as a bare `yaml.safe_load`, that raised a
    `yaml.YAMLError` straight through `locked_rmw` as an uncaught
    traceback; downstream readers (`plan_tasks_render.load_rows`) degrade
    the same body to MALFORMED and every spine CLI then reports a visibly
    present spine as absent, first noticed at `/execute-plan`.

    Negative-spec: this is a parse gate, not a schema gate — shape and
    cross-field rules stay `_validate_all`'s and `schema_validate.py`'s.
    It names the 1-based line WITHIN the fence body, not the file, because
    that is the offset the caller's own error mark carries; a file-line
    translation would be a second, driftable computation.
    """
    try:
        rows = yaml.safe_load(body) or []
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1} of the fenced block" if mark else ""
        problem = getattr(exc, "problem", None) or str(exc)
        raise MutateAbort(
            f"{verb}: task spine does not parse as YAML{where}: {problem}. "
            "The fence is present and renders, but its body is not loadable — "
            "fix the block before writing to it."
        ) from exc
    if not isinstance(rows, list):
        raise MutateAbort(f"{verb}: task spine body is not a YAML list")
    return rows


def _add_task(plan_path: str, task: dict, worktree: Path, repo_root: Path) -> dict:
    """Apply the add-task verb: append `task` as a new row to the task spine.

    Routes the read-modify-write through locked_rmw for cross-process
    serialisation. Domain-abort paths (malformed spine, duplicate id,
    schema-invalid row) raise MutateAbort from inside the mutate closure so
    the lock is released and no write occurs.
    """
    try:
        path = _resolve_path(plan_path, worktree)
    except _PathNotContained as exc:
        return _err(f"add-task: {exc}")

    if not isinstance(task, dict) or not task.get("id"):
        return _err("add-task: 'task' must be a dict with a non-empty 'id'")

    _state: dict = {"applied": False, "message": "", "warnings": []}

    def mutate(old_text: str) -> str:
        result = locate_fenced_block(old_text)

        if result.status is LocateStatus.MALFORMED:
            raise MutateAbort(
                "add-task: task spine is malformed (multiple 'yaml plan-tasks' fences, "
                "or a fence not directly under the '## Tasks' heading)"
            )

        # PLAN-scoped context _validate_all forwards to the writes-declared
        # cross-field rule (2026-08-19 fix) — resolved once per mutate call,
        # mirroring how `_resolve` already resolves `governed` from the same
        # frontmatter parse.
        plan_fm = parse_frontmatter(old_text).get("frontmatter")
        plan_created = plan_fm.get("created") if isinstance(plan_fm, dict) else None
        # governed threaded into validation (2026-08-31 fix, cross-repo/archive/
        # 2026-08-13-doe-claude-em-plan-tasks-mutate-governed-flag-asymmetry.md):
        # mirrors `resolve`'s own resolution from the same frontmatter parse, so
        # add-task and resolve agree on row validity for the same governed plan.
        governed = is_governed_plan(plan_fm) if isinstance(plan_fm, dict) else False

        if result.status is LocateStatus.ABSENT:
            rows: list = []
            new_rows = rows + [task]
            # Review: code-reviewer — validate new_rows uniformly via the
            # shared _validate_all helper in both ABSENT and LOCATED branches,
            # instead of validating `task` alone here (F3).
            try:
                untouched_invalid = _validate_all(
                    new_rows, governed=governed, touched_ids={task["id"]}, plan_created=plan_created,
                )
            except MutateAbort as exc:
                raise MutateAbort(f"add-task: {exc.args[0] if exc.args else exc}") from exc
            body_yaml = _dump_rows(new_rows)
            if _has_tasks_heading(old_text):
                new_text = _synthesize_fence_under_heading(old_text, body_yaml)
            else:
                new_text = _synthesize_tasks_section(old_text, body_yaml)
            _state["applied"] = True
            _state["message"] = f"add-task: created task spine and added task {task['id']!r}"
            _state["warnings"] = _untouched_invalid_warnings(untouched_invalid)
            return new_text

        # LOCATED
        rows = _parse_rows_or_abort(result.body, "add-task")

        existing_ids = {row.get("id") for row in rows if isinstance(row, dict)}
        if task["id"] in existing_ids:
            raise MutateAbort(f"add-task: duplicate task id {task['id']!r}")

        new_rows = rows + [task]
        try:
            untouched_invalid = _validate_all(
                new_rows, governed=governed, touched_ids={task["id"]}, plan_created=plan_created,
            )
        except MutateAbort as exc:
            raise MutateAbort(f"add-task: {exc.args[0] if exc.args else exc}") from exc

        body_yaml = _dump_rows(new_rows)
        start, end = result.span
        new_text = old_text[:start] + body_yaml + old_text[end:]
        _state["applied"] = True
        _state["message"] = f"add-task: added task {task['id']!r}"
        _state["warnings"] = _untouched_invalid_warnings(untouched_invalid)
        return new_text

    try:
        locked_rmw(path, mutate, repo_root=repo_root)
    except FileNotFoundError:
        return _err(f"add-task: plan not found: {plan_path}")
    except LockTimeout as exc:
        return _err(f"add-task: timed out waiting for file lock on {plan_path}: {exc}")
    except MutateAbort as exc:
        return _err(exc.args[0] if exc.args else "add-task: mutation aborted")

    return _ok(_state["applied"], _state["message"], warnings=_state["warnings"])


# ---------------------------------------------------------------------------
# stamp
# ---------------------------------------------------------------------------

# D4 (2026-07-27): the three disposition fields are resolve's surface
# exclusively. Reserving them in stamp closes the side door that would
# otherwise let a caller bypass resolve's pm_approved gate by stamping
# `disposition: spun_off` directly.
_STAMP_RESERVED_DISPOSITION_FIELDS = frozenset(
    {"disposition", "disposition_ref", "disposition_detail"}
)


def _stamp(plan_path: str, updates: list, worktree: Path, repo_root: Path) -> dict:
    """Apply the stamp verb: update fields on N ids in a single locked_rmw.

    All-or-nothing: any id-not-found or schema-invalid resulting row aborts
    the whole batch via MutateAbort — one callback, one lock, zero writes.

    Reserved-field refusal (D4, 2026-07-27): if ANY update entry in the
    batch names disposition/disposition_ref/disposition_detail, the WHOLE
    batch is refused before locked_rmw is even invoked — worded as an offer
    naming the alternative (`--verb resolve`), not a bare denial.

    The offer names the VERB and stops there (2026-08-14, consult memo
    cross-repo/archive/2026-08-14-doe-claude-em-stamp-reserved-field-refusal-carries-retired-pm-approval-offer.md).
    It used to trail "needs pm_approved: true stamped on the row first" —
    the retired `_PM_APPROVAL_OFFER` shape (see its banner below `resolve`)
    printed by the one verb that sets that field, so the refusal supplied
    the key to its own door. `_LEGACY_PM_APPROVAL_HINT`'s naming of the
    field is not a precedent here: that is resolve's honesty layer on a
    branch the caller has already reached, not stamp's exit offer.
    """
    try:
        path = _resolve_path(plan_path, worktree)
    except _PathNotContained as exc:
        return _err(f"stamp: {exc}")

    if not isinstance(updates, list) or not updates:
        return _err("stamp: 'updates' must be a non-empty list of {id, ...fields} dicts")
    for u in updates:
        if not isinstance(u, dict) or not u.get("id"):
            return _err("stamp: every update entry must be a dict with a non-empty 'id'")

    for u in updates:
        reserved_present = _STAMP_RESERVED_DISPOSITION_FIELDS & set(u.keys())
        if reserved_present:
            fields = ", ".join(sorted(reserved_present))
            return _err(
                f"stamp: update entry {u.get('id')!r} carries reserved field(s) "
                f"{fields} — disposition is resolve's surface: use --verb "
                "resolve. A closed disposition (spun_off/backlogged/wont_do) "
                "records that the PM ratified this cut, so it waits on their "
                "ruling. Refusing the whole batch — no writes applied."
            )

    _state: dict = {"applied": False, "message": "", "warnings": []}

    def mutate(old_text: str) -> str:
        result = locate_fenced_block(old_text)

        if result.status is LocateStatus.MALFORMED:
            raise MutateAbort(
                "stamp: task spine is malformed (multiple 'yaml plan-tasks' fences, "
                "or a fence not directly under the '## Tasks' heading)"
            )
        if result.status is LocateStatus.ABSENT:
            raise MutateAbort("stamp: task spine is absent — nothing to stamp")

        # PLAN-scoped context forwarded to the writes-declared cross-field
        # rule (2026-08-19 fix) — see _add_task's identical resolution.
        plan_fm = parse_frontmatter(old_text).get("frontmatter")
        plan_created = plan_fm.get("created") if isinstance(plan_fm, dict) else None
        # governed threaded into validation (2026-08-31 fix, cross-repo/archive/
        # 2026-08-13-doe-claude-em-plan-tasks-mutate-governed-flag-asymmetry.md):
        # mirrors `resolve`'s own resolution from the same frontmatter parse, so
        # stamp and resolve agree on row validity for the same governed plan.
        governed = is_governed_plan(plan_fm) if isinstance(plan_fm, dict) else False

        rows = _parse_rows_or_abort(result.body, "stamp")

        rows_by_id = {row.get("id"): row for row in rows if isinstance(row, dict)}

        # Review: code-reviewer — fail-loud on a duplicate id within one
        # `updates` batch, mirroring add-task's fail-loud-dup discipline
        # (EM decision: reject, not last-write-wins) (F2). Checked before any
        # row mutation begins so an abort here leaves `rows` untouched.
        update_ids = [u["id"] for u in updates]
        seen: set = set()
        for uid in update_ids:
            if uid in seen:
                raise MutateAbort(f"stamp: duplicate task id in updates batch: {uid!r}")
            seen.add(uid)

        stamped_ids: list = []
        for update in updates:
            task_id = update["id"]
            row = rows_by_id.get(task_id)
            if row is None:
                raise MutateAbort(f"stamp: task id not found: {task_id!r}")
            for field, value in update.items():
                if field == "id":
                    continue
                row[field] = value
            stamped_ids.append(task_id)

        # Review: code-reviewer — reuse the shared _validate_all helper for
        # stamp's final validation loop (F3), consistent with add-task.
        # `touched_ids=set(stamped_ids)` (2026-08-16, untouched-invalid-row
        # deadlock fix): a pre-existing invalid row this batch did not
        # stamp must not veto the batch — see _validate_all's own docstring.
        try:
            untouched_invalid = _validate_all(
                rows, governed=governed, touched_ids=set(stamped_ids), plan_created=plan_created,
            )
        except MutateAbort as exc:
            raise MutateAbort(f"stamp: {exc.args[0] if exc.args else exc}") from exc

        body_yaml = _dump_rows(rows)
        start, end = result.span
        new_text = old_text[:start] + body_yaml + old_text[end:]
        _state["applied"] = True
        _state["message"] = f"stamp: updated {len(stamped_ids)} task(s): {stamped_ids}"
        _state["warnings"] = _untouched_invalid_warnings(untouched_invalid)
        return new_text

    try:
        locked_rmw(path, mutate, repo_root=repo_root)
    except FileNotFoundError:
        return _err(f"stamp: plan not found: {plan_path}")
    except LockTimeout as exc:
        return _err(f"stamp: timed out waiting for file lock on {plan_path}: {exc}")
    except MutateAbort as exc:
        return _err(exc.args[0] if exc.args else "stamp: mutation aborted")

    return _ok(_state["applied"], _state["message"], warnings=_state["warnings"])


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------

# RETIRED 2026-07-29 — `_PM_APPROVAL_OFFER` lived here and is deliberately
# not replaced with a new-field equivalent.
#
# It read: "stamp the row with pm_approved: true first (--verb stamp
# --updates '[{"id": "<id>", "pm_approved": true}]'), then re-run resolve" —
# a gate printing its own key, taped to its own door. The field it checked
# was one the same agent could set one command earlier, and the refusal
# helpfully supplied that command. The refusal TEXT was right; the offer
# defeated it.
#
# Under the grouping-approval contract the offer has no honest analogue,
# because there is no command an EM can run to approve a grouping — that is
# the entire point of moving the signal to the plan's frontmatter. So the
# refusals below name the one real next action (ask the PM) and reuse
# `_GROUPING_APPROVAL_HINT` from schema_validate, which is written as an
# offer — the better alternative on offer is "go get a decision" — without
# softening the substance into a missing-field nit. A nit teaches a
# well-meaning EM to satisfy the field, which reproduces this exact defect
# one layer up.
#
# Contract: cross-repo/archive/2026-07-29-doe-claude-em-grouping-approval-contract.md
# (actioned; moved from inbox/ to archive/) § "And a hard requirement on your
# refusal messages."

# LEGACY plans (no `grouping_approvals` key at all) have no groupings and no
# `pm_utterance` field anywhere in their schema — `_GROUPING_APPROVAL_HINT`
# above describes machinery that does not exist on the plan this branch
# fires for (Review: code-reviewer Finding 4).
#
# REWRITTEN 2026-08-12 (DoE ruling, exit 1 —
# cross-repo/inbox/2026-08-12-doe-claude-em-legacy-refusal-honesty-ruling.md;
# tripwire A-REFUSAL-MAY-NOT-CLAIM-IMPOSSIBILITY-IT-CANNOT-ENFORCE). The
# prior text carried the governed branch's impossibility claim ("there is
# deliberately no command that satisfies this from inside the session") onto
# a branch where a command does: `pm_approved` is a per-row boolean the same
# agent can set via the `stamp` verb. The claim was false, and false in the
# direction that costs the honest party everything and the self-certifying
# party one extra call — example-cockpit-repo-em read it as impossibility, could
# not record a verbatim PM ruling, and took a divergence (PM-ruled wont_do in
# plan prose, spine row still `open`).
#
# So the protection moves from the mechanism layer to the honesty layer,
# which is the strongest thing a self-settable boolean can carry: the
# assertion recording the field MAKES leads, and the field is named after it,
# never instead of it. Self-certification becomes a lie an agent has to tell
# rather than a door it cannot find. This is NOT a relaxation into a
# missing-field nit — "set this field to proceed" is the voice the retired
# `_PM_APPROVAL_OFFER` banner below correctly killed, because it teaches a
# well-meaning EM to satisfy the field. `_GROUPING_APPROVAL_HINT` above is
# untouched by this ruling: its impossibility claim is TRUE, and the
# membership digest is what makes it true.
_LEGACY_PM_APPROVAL_HINT = (
    "Recording pm_approved: true on this row asserts that the PM ratified "
    "this specific cut. Nothing in this session can verify that, so stamping "
    "it without their word puts a false statement in the record. "
    "plan-tasks-stamp sets the field once they have ruled."
)

# All three CLOSED dispositions require an explicit caller-supplied
# disposition_detail — D4: "the verbatim PM reasoning ... goes in
# disposition_detail." coded is excluded (D3: not a scope decision, carries
# no PM rationale to record). resolve refuses BEFORE dispatching
# backlogged's harvest-CLI delegation, so an ungated call never produces a
# queue/lesson side effect it would then need to unwind.
#
# `wont_do` ADDED 2026-08-05 (C2, break-class fix): the vendored schema's
# own allOf branch 4 has required disposition_detail for wont_do since
# before 1.3.0 (and forbidden disposition_ref alongside it), but this write
# path never enforced it — a `wont_do` resolve with no detail passed this
# gate clean and produced a row that was schema-invalid on the very next
# validation pass. The stale claim this comment used to make ("already
# enforced by the vendored schema at write time") was true of the SCHEMA,
# never of THIS write path, which is the one that actually decides whether
# resolve's write proceeds.
#
# Shape choice (spec backlink: DoE-claude:pln-plan-line-item-resolution-mode-16787c,
# Defect 2 dispatch brief): a synthesised detail (e.g. "routed to <ref>")
# would only restate disposition_ref, adding no information a reader doesn't
# already have — so this requires the caller to supply real prose rather
# than synthesizing a placeholder, matching the pm_approved gate's own
# refuse-with-an-offer voice above.
_PLAN_TASKS_DETAIL_REQUIRED_DISPOSITIONS = frozenset({'spun_off', 'backlogged', 'wont_do'})

_DISPOSITION_DETAIL_OFFER = (
    "pass disposition_detail naming the PM's reasoning "
    "(--verb resolve --id {task_id} --disposition {disposition} "
    "--disposition-detail \"<why>\"), then re-run resolve"
)

# `case_against` (leg 1, 2026-08-06, plan
# docs/plans/2026-08-06-deferrals-carry-both-sides.md): the SAME two
# scope-cut dispositions as `_PLAN_TASKS_DETAIL_REQUIRED_DISPOSITIONS`
# minus `spun_off` — nothing leaves the corpus on a spinoff, so there is
# no scope cut to argue against, and widening this trigger set would
# re-open a boundary the PM already ruled on 2026-08-05. Where
# `disposition_detail` carries the case FOR closing (the EM's own
# reasoning), `case_against` carries the case AGAINST — the strongest
# honest argument for doing the work now — so a deferral surfaced to the
# PM is a real decision, not an ID list the EM has already convinced
# itself of. The vendored schema (1.6.0) makes this field REQUIRED via
# an `allOf` conditional on the same trigger set, but is presence-only /
# non-hard-failing at the schema layer (it checks the key exists, not
# that its prose is non-vacuous) — this op is the hard-rejection
# enforcement leg asked of claude-klabauter by that plan's C8 memo.
_PLAN_TASKS_CASE_AGAINST_REQUIRED_DISPOSITIONS = frozenset({'backlogged', 'wont_do'})

_CASE_AGAINST_OFFER = (
    "pass case_against naming the strongest honest case for doing the "
    "work now (--verb resolve --id {task_id} --disposition {disposition} "
    "--case-against \"<why not cut>\"), then re-run resolve"
)

# ---------------------------------------------------------------------------
# resolve --backlogged delegation to coordinator-harvest-deferrals (C5)
# ---------------------------------------------------------------------------

# Fixed, Path(__file__)-relative script location — mirrors
# coordinator_core.workday_complete.apply._CLI_SCRIPT_ROOT's established
# in-process-CLI-load convention (never a brief/param-derived import target).
_HARVEST_CLI_PATH = (
    Path(__file__).resolve().parents[2] / "coordinator" / "bin" / "coordinator-harvest-deferrals.py"
)

_HARVEST_MODULE: Optional[ModuleType] = None


def _load_harvest_module() -> ModuleType:
    """Load `coordinator/bin/coordinator-harvest-deferrals` in-process
    (once, cached at module scope), via `importlib.util.spec_from_file_location`
    — the SAME in-process-CLI-load pattern
    `coordinator_core.workday_complete.apply._load_cli_module` (and its
    `workstream_complete`/`workweek_complete` siblings) already establish for
    dispatching a `coordinator/bin/*` script's functions without a subprocess
    spawn. Never re-implements the CLI's routing logic — this loads the real
    module and calls its own private functions (`_parse_plan_id`,
    `_harvest_key`, `_candidate_search_dirs`, `_already_harvested`,
    `_run_queue_append`, `_run_lesson_promote`,
    `_QUEUE_ELIGIBLE_CHANGE_KINDS`, `_LESSON_PROMOTE_CHANGE_KINDS`) directly,
    so the change_kind split and the `(plan_id, row id)` idempotency key
    cannot drift between the standalone CLI's own batch harvest and this
    verb's single-row delegation (C5's "do not copy-paste the mapping").

    Test seam: tests monkeypatch this module-level function itself (not the
    loaded module's internals) to inject a lightweight fake exposing the
    same attribute surface — see test_plan_tasks_mutate.py's
    `_make_fake_harvest_module`.
    """
    global _HARVEST_MODULE
    if _HARVEST_MODULE is not None:
        return _HARVEST_MODULE
    if not _HARVEST_CLI_PATH.is_file():
        raise MutateAbort(
            "resolve: could not locate coordinator-harvest-deferrals at "
            f"{_HARVEST_CLI_PATH} — cannot delegate backlogged row-routing"
        )
    module_name = "_plan_tasks_mutate_harvest_deferrals_cli"
    spec = importlib.util.spec_from_file_location(module_name, _HARVEST_CLI_PATH)
    if spec is None or spec.loader is None:
        raise MutateAbort(
            f"resolve: could not load coordinator-harvest-deferrals from {_HARVEST_CLI_PATH}"
        )
    module = importlib.util.module_from_spec(spec)
    # Register in sys.modules BEFORE exec — mirrors
    # workday_complete.apply._load_cli_module's identical fix (some
    # coordinator/bin scripts resolve sys.modules[cls.__module__] during
    # class-body execution; an unregistered module makes that lookup crash).
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    _HARVEST_MODULE = module
    return module


def _find_evidence_file(key: str, search_dirs: list) -> Optional[str]:
    """Return the path of the `*.yaml` file under `search_dirs` whose
    `evidence:` line contains `key`, or `None` if not found.

    Mirrors coordinator-harvest-deferrals' own `_already_harvested` scan
    (same evidence-line scoping) but returns the PATH instead of a bool.
    `_run_queue_append` / `_run_lesson_promote` report success/failure only
    — the standalone CLI itself only ECHOES the written path to stdout
    (never returns it) — so the written entry's path is recovered here by
    re-scanning for the row's own idempotency key immediately after
    dispatch, rather than threading a return-path through the harvest
    module's private dispatch functions (which would require editing that
    module — out of this chunk's write-scope). This is new locator logic,
    not a copy of the routing/change_kind-split mapping itself.
    """
    for directory in search_dirs:
        if not directory:
            continue
        for path in glob.glob(str(Path(directory) / "*.yaml")):
            try:
                with open(path, encoding="utf-8") as fh:
                    content = fh.read()
            except OSError:
                continue
            for line in content.splitlines():
                if line.strip().startswith("evidence:") and key in line:
                    return path
    return None


def _to_repo_relative(path: str, worktree: Path) -> str:
    """Best-effort repo-relative rendering of `path` for `disposition_ref`
    (DR-096: a single repo-relative path, enforced by
    `_cf_plan_tasks_disposition_shape`'s `_is_single_repo_relative_path`
    check). Falls back to `path` unchanged when it is not under `worktree`
    — a central-scope (claude-klabauter) or lessons-outbox (DoE) write can legitimately
    land in a different repo than the plan's own; an absolute cross-repo path
    is still a single, unambiguous referent, just not one relative to THIS
    plan's own worktree.
    """
    # A4 fix: `rel_id` (not `str(...relative_to(...))`) -- the latter
    # renders with `os.sep`, so a Windows session would write
    # `state\lessons\x.yaml` into the tracked plan-tasks spine.  DR-096's
    # `_is_single_repo_relative_path` validator and every downstream reader
    # key on the posix form.
    try:
        return rel_id(Path(path).resolve(), worktree.resolve())
    except ValueError:
        return path


def _dispatch_spun_off(task_id: str, disposition_ref: Optional[str], worktree: Path) -> str:
    """Compute a `disposition: spun_off` row's `disposition_ref` by verifying
    it against the spinoff artifact actually created, rather than recording a
    caller-supplied string verbatim (AC17's "computed producer" bar — the
    row must never point at a spinoff that does not exist).

    This does NOT create the spinoff artifact itself — that write lands
    separately, before `resolve` is ever called for this row (the `/spinoff`
    authoring surface; see `coordinator/bin/spinoff-deliverable-and-commit.py`
    in DoE-claude). What this function computes is the VERIFIED, canonical
    repo-relative form of the ref: it resolves the caller-supplied path
    against `worktree`, confirms a real file exists there, and re-derives the
    ref via `_to_repo_relative` rather than trusting the literal string —
    mirroring the principle `_dispatch_backlogged` establishes (a ref must be
    derived from a file that actually exists, not predicted or typed in
    advance), one step lighter because the artifact was already created by a
    prior write this op does not own.

    Raises MutateAbort (no write) when `disposition_ref` is missing/empty, or
    when it does not resolve to an existing file — a caller-supplied path
    that never landed on disk must abort the whole call, exactly as
    `_dispatch_backlogged` aborts before its disposition write on any harvest
    failure.
    """
    if not disposition_ref or not str(disposition_ref).strip():
        raise MutateAbort(
            f"resolve: disposition 'spun_off' for task {task_id!r} requires "
            "disposition_ref naming the spinoff artifact /spinoff already "
            "created (--disposition-ref <path>) — resolve does not create "
            "the spinoff itself, only verifies and records where it landed. "
            "No disposition was written."
        )

    candidate = Path(disposition_ref)
    resolved = candidate.resolve() if candidate.is_absolute() else (worktree / candidate).resolve()
    if not resolved.is_file():
        raise MutateAbort(
            f"resolve: task {task_id!r} disposition_ref {disposition_ref!r} does not "
            "point to a file that exists on disk — /spinoff must create the "
            "artifact before this row can be resolved to 'spun_off'. No "
            "disposition was written."
        )
    return _to_repo_relative(str(resolved), worktree)


def _dispatch_backlogged(row: dict, task_id: str, plan_text: str, worktree: Path) -> str:
    """Delegate a `disposition: backlogged` row to coordinator-harvest-
    deferrals' own row-routing FOR THE SINGLE ROW (AC5) — one operation, not
    two, from resolve's caller's point of view. Returns the (best-effort
    repo-relative) path of the queue/lesson entry the row routed to, for
    `disposition_ref`. Raises MutateAbort on any failure — missing plan_id,
    unroutable change_kind, a failed dispatch, or the entry not locatable
    afterward — so resolve aborts the WHOLE call (no disposition write is
    ever half-completed against a queue/lesson write that didn't land, or
    vice versa).

    Idempotency: reuses the harvest CLI's own `_harvest_key(plan_id, row id)`
    and `_already_harvested` dedup scan verbatim — a second `resolve
    --backlogged` call on an already-routed row skips the dispatch and
    relocates the SAME entry, so re-running resolve is a no-op on the
    queue/lesson side (idempotent) while still succeeding on the spine side.
    """
    harvest = _load_harvest_module()

    plan_id = harvest._parse_plan_id(plan_text)
    if not plan_id:
        raise MutateAbort(
            "resolve: plan frontmatter has no 'plan_id' field — cannot form "
            "the (plan_id, row id) idempotency key coordinator-harvest-"
            "deferrals requires for backlogged delegation. Add "
            "'plan_id: \"...\"' to the plan's frontmatter and retry."
        )

    key = harvest._harvest_key(plan_id, task_id)
    search_dirs = harvest._candidate_search_dirs(row)

    if not harvest._already_harvested(key, search_dirs):
        change_kind = row.get("change_kind")
        if change_kind in harvest._LESSON_PROMOTE_CHANGE_KINDS:
            ok = harvest._run_lesson_promote(row, key, dry_run=False)
        elif change_kind in harvest._QUEUE_ELIGIBLE_CHANGE_KINDS:
            ok = harvest._run_queue_append(row, key, dry_run=False)
        else:
            raise MutateAbort(
                f"resolve: task {task_id!r} has unroutable change_kind "
                f"{change_kind!r} — coordinator-harvest-deferrals has no "
                "queue/lesson route for it, so backlogged delegation cannot "
                "proceed. No disposition was written."
            )
        if not ok:
            raise MutateAbort(
                f"resolve: coordinator-harvest-deferrals row-routing failed "
                f"for task {task_id!r} (change_kind={change_kind!r}) — see "
                "stderr from the underlying coordinator-queue-append/"
                "coordinator-lesson-promote call. No disposition was written."
            )

    found = _find_evidence_file(key, search_dirs)
    if not found:
        raise MutateAbort(
            f"resolve: task {task_id!r} routed successfully (or was already "
            f"harvested) under key {key!r}, but the written entry could not "
            "be located afterward for disposition_ref — check "
            "coordinator-harvest-deferrals' search-dir resolution. No "
            "disposition was written."
        )
    return _to_repo_relative(found, worktree)


# ---------------------------------------------------------------------------
# D5 auto-repositioning (2026-08-06 ordering-deadlock fix)
# ---------------------------------------------------------------------------


def _plan_tasks_row_rank(row: dict) -> tuple:
    """D5 sort key for one task-spine row: `(grouping rank, do-suborder rank)`.

    Deliberately computed the SAME way `check_plan_tasks_ordering`
    (`coordinator_core.frontmatter.schema_validate`) computes rank for its
    own lint — same grouping table (`_PLAN_TASKS_GROUPING_BY_DISPOSITION`),
    same grouping order (`_PLAN_TASKS_GROUPING_ORDER`), same do-suborder
    table (`_PLAN_TASKS_SUBORDER_BY_DISPOSITION`), same `'open'`/`'do'`/`0`
    defaults for a row with no `disposition` or an unrecognized one — never
    an independent reimplementation. Two placement authorities computing
    rank differently is exactly the failure mode `check_plan_tasks_ordering`
    itself warns against (a spine each authority orders differently,
    surfacing as an unfixable plan): this function and that lint must never
    be able to disagree about what "correct order" means.

    The `open` default is resolved by calling
    `schema_validate._plan_tasks_row_disposition` directly (Review:
    code-reviewer — near-miss fix: this function used to default via
    `row.get("disposition") or "open"`, a falsy-check, while
    `_plan_tasks_row_disposition` defaults via an `isinstance(value, str)
    and value` type-check; the two agree on every value the vendored
    schema's enum permits, so this was not currently reachable, but
    "cannot drift" must not depend on that coincidence). Calling the same
    private helper the rest of this module already imports across the
    same boundary (see the module import block) makes the two literally
    the same rule rather than two hand-matched ones.
    """
    disposition = _plan_tasks_row_disposition(row)
    grouping = _PLAN_TASKS_GROUPING_BY_DISPOSITION.get(disposition, "do")
    return (
        _PLAN_TASKS_GROUPING_ORDER.index(grouping),
        _PLAN_TASKS_SUBORDER_BY_DISPOSITION.get(disposition, 0),
    )


def _reposition_rows_for_d5(rows: list) -> list:
    """Stable-sort `rows` into D5's required grouping order.

    A disposition change is exactly what determines which grouping a row
    belongs to — so once `resolve` has written a batch's new
    `disposition` fields onto `rows`, the row's POSITION must move to
    match in the same write; leaving position untouched and only checking
    it left some single-row transitions unsatisfiable in EITHER direction
    (see `_resolve`'s module-docstring section and the queue entry
    `state/bug-backlog/2026-08-06-plan-tasks-mutate-d5-ordering-deadlocks-
    c223a7208a5a.yaml` for the exact deadlock this fixes).

    Uses Python's `sorted()` — guaranteed STABLE — specifically so this
    stays narrow: rows that already share a rank (same grouping, same
    do-suborder) keep their existing relative order, untouched. A row not
    named in this batch keeps its existing disposition, hence its
    existing rank, so its position relative to every OTHER untouched row
    is unchanged no matter how the spine was ordered before this call —
    only rows whose rank this batch itself just changed can move. This
    holds regardless of whether the incoming spine already satisfied D5:
    unlike the retired pre-write check, this sort does not require the
    spine to already be valid — it MAKES it valid, which is what lets
    `resolve` no longer refuse a call just because some other, unrelated
    part of the spine was already out of order.

    LOG SIGNAL (Review: code-reviewer P3, EM-dispositioned as fix — the
    retired pre-write check used to refuse loudly on a spine whose order
    was ALREADY invalid for reasons unrelated to this call (hand-edit,
    merge-mangling, corruption); this sort now normalises that silently,
    with nothing recording that a repair happened at all. Repair is still
    the right behaviour — the retired check is what deadlocked ordinary
    forward progress — but a fired log line restores the signal without
    reintroducing the refusal: an `_LOG.info` line fires ONLY when the
    sort actually changes row order (i.e. an id at some position in the
    input differs from the id at that position in the output — the
    overwhelmingly common no-op case, where the batch's own rows already
    sat in D5 order, logs nothing), naming the id(s) whose position moved.
    A fired line means the spine arrived at this call ALREADY out of
    order for reasons this call's own disposition writes do not explain —
    worth investigating, not routine.
    """
    repositioned = sorted(rows, key=_plan_tasks_row_rank)
    moved_ids = [
        before.get("id")
        for before, after in zip(rows, repositioned)
        if before is not after
    ]
    if moved_ids:
        _LOG.info(
            "plan.tasks.mutate resolve: D5 repositioning changed row order — "
            "spine arrived already out of order; moved id(s): %r",
            moved_ids,
        )
    return repositioned


def _resolve(
    plan_path: str,
    resolutions: list,
    worktree: Path,
    repo_root: Path,
) -> dict:
    """Apply the resolve verb: write N rows' disposition + ref + detail
    atomically under ONE locked_rmw call (C13, 2026-07-30 batch-resolve).

    `resolutions` is a non-empty list of `{"id": ..., "disposition": ...,
    "disposition_ref": ..., "disposition_detail": ...}` dicts — see
    `_handler` for how a single-row `id`/`disposition` call and a
    multi-row `resolves` call both normalize to this shape before reaching
    here. A batch of one behaves identically to the pre-batch verb (same
    checks, same error text, same message shape for len==1).

    Refuses (MutateAbort, no write, WHOLE BATCH aborts) when ANY row in the
    batch names a CLOSED disposition (spun_off / backlogged / wont_do)
    whose authorization signal does not clear — mirrors
    handoff_carry_gate's refuse-on-ungated-state pattern (D4). `coded` is
    not closed and needs no authorization (D3). On a GOVERNED plan, every
    grouping touched by the batch is checked once, against a prospective
    membership computed with the ENTIRE batch applied — not per row (see
    the module docstring's resolve-verb section for why per-row checking
    against a set-granularity approval is exactly the defect this exists
    to fix). On a LEGACY plan each row's own `pm_approved` field is
    checked independently (legacy plans have no groupings to batch over).

    Also refuses (MutateAbort, no write) when any `spun_off`/`backlogged`
    row in the batch lacks a non-empty `disposition_detail` — D4's "the
    verbatim PM reasoning ... goes in disposition_detail" requires real
    prose, not a synthesised placeholder that would only restate
    `disposition_ref` (Defect 2 fix). `wont_do` needs no separate check
    here — it has no `disposition_ref` to pair a detail with, and the
    vendored schema already hard-requires its detail at write time. Every
    refusal is worded as an offer naming the concrete next step, not a
    bare denial (D4) — this is the EM-facing surface that makes the PM
    gate legible at the moment of the cut.

    Every gate above runs for the WHOLE batch before any row is mutated or
    any backlogged/spun_off dispatch side effect fires — see the module
    docstring's "ATOMICITY" paragraph.
    """
    try:
        path = _resolve_path(plan_path, worktree)
    except _PathNotContained as exc:
        return _err(f"resolve: {exc}")

    if not isinstance(resolutions, list) or not resolutions:
        return _err("resolve: 'id'/'disposition' (or a non-empty 'resolves' batch) is required")

    for r in resolutions:
        if not isinstance(r, dict) or not r.get("id"):
            return _err("resolve: 'id' is required")
        if not r.get("disposition"):
            return _err("resolve: 'disposition' is required")

    # Fail-loud on a duplicate id within one batch (mirrors stamp's F2
    # discipline) — checked before the lock is even taken, same as the
    # per-entry shape checks above.
    seen_ids: set = set()
    for r in resolutions:
        rid = r["id"]
        if rid in seen_ids:
            return _err(f"resolve: duplicate task id in batch: {rid!r}")
        seen_ids.add(rid)

    _state: dict = {"applied": False, "message": "", "all_resolved": False, "warnings": []}

    def mutate(old_text: str) -> str:
        result = locate_fenced_block(old_text)

        if result.status is LocateStatus.MALFORMED:
            raise MutateAbort(
                "resolve: task spine is malformed (multiple 'yaml plan-tasks' fences, "
                "or a fence not directly under the '## Tasks' heading)"
            )
        if result.status is LocateStatus.ABSENT:
            raise MutateAbort("resolve: task spine is absent — nothing to resolve")

        # RETIRED 2026-08-06 (D5 ordering-deadlock fix, queue
        # state/bug-backlog/2026-08-06-plan-tasks-mutate-d5-ordering-
        # deadlocks-c223a7208a5a.yaml) — a PRE-write refusal used to live
        # here: "refuse before writing a new disposition onto a spine whose
        # EXISTING row order already violates D5", checked against
        # `old_text` as a precondition on the spine's on-disk state.
        #
        # That precondition is what made the deadlock this fix exists for:
        # a spine reaches "earlier rows coded, one open row trailing them"
        # by ordinary forward progress (code C1, then C2, ... leaving the
        # last row open) — but the do-suborder rule (open must sort above
        # coded) calls that same, ordinary spine ALREADY invalid, so this
        # precondition refused every subsequent resolve call on it
        # regardless of what the call was trying to do. There was no
        # un-resolve verb and no reorder verb, so no edit could satisfy the
        # precondition before making the write it was meant to gate.
        #
        # It is also no longer NEEDED: `_reposition_rows_for_d5` below now
        # runs a full stable sort of the batch's post-mutation `rows` by
        # the identical rank tuple this precondition checked, on every
        # resolve call — so the write this precondition used to guard
        # against ("compounding an already-invalid ordering") cannot
        # happen anymore; the write always ends in a D5-valid order
        # regardless of what order the spine started in. Removing this
        # precondition does not relax D5's invariant on the RESULTING
        # spine — that invariant is still enforced (the post-mutation
        # check below), just no longer ALSO demanded of the spine as it
        # stood before this call, which is the half of the old contract
        # that was unsatisfiable.
        rows = _parse_rows_or_abort(result.body, "resolve")

        rows_by_id = {row.get("id"): row for row in rows if isinstance(row, dict)}

        # Every id in the batch must exist BEFORE any check runs against a
        # row's fields — an unknown id aborts the whole call.
        for r in resolutions:
            if rows_by_id.get(r["id"]) is None:
                raise MutateAbort(f"resolve: task id not found: {r['id']!r}")

        # Authorization gate: a CLOSED disposition is a scope decision and
        # needs the PM's recorded assent. resolve never grants that itself —
        # it only checks — and refuses without offering any way to satisfy
        # the check from inside the session (see the retired
        # `_PM_APPROVAL_OFFER` banner above for why).
        #
        # Which signal carries the assent depends on the plan:
        #   - GOVERNED (frontmatter carries the `grouping_approvals` key at
        #     all — bare presence, no schema_version conjunct; see
        #     is_governed_plan's own docstring for why the version conjunct
        #     was dropped 2026-07-29): each grouping touched by the batch
        #     must read status: approved, and its digest must cover the
        #     membership the WHOLE BATCH is about to produce.
        #   - LEGACY (no `grouping_approvals` key): the per-row pm_approved
        #     boolean, checked per row (no grouping to batch over).
        plan_fm = parse_frontmatter(old_text).get("frontmatter")
        governed = is_governed_plan(plan_fm) if isinstance(plan_fm, dict) else False
        # PLAN-scoped context forwarded to the writes-declared cross-field
        # rule (2026-08-19 fix) — see _add_task's identical resolution.
        plan_created = plan_fm.get("created") if isinstance(plan_fm, dict) else None

        # id -> prospective (about-to-be-written) disposition for the WHOLE
        # batch — this is what makes the digest check below cover the
        # batch's full membership rather than one row at a time (C13's
        # entire point: a set-granularity approval needs a set-granularity
        # write to check against).
        new_disposition_by_id = {r["id"]: r["disposition"] for r in resolutions}

        def _prospective_rows() -> list:
            return [
                {**row, "disposition": new_disposition_by_id[row["id"]]}
                if isinstance(row, dict) and row.get("id") in new_disposition_by_id
                else row
                for row in rows
                if isinstance(row, dict)
            ]

        # Group the batch's CLOSED-disposition entries by grouping so a
        # governed plan's per-grouping approval is checked exactly once per
        # grouping touched, even when several rows in the batch land in the
        # same grouping (or the batch spans more than one grouping).
        #
        # TWO SETS, picked by mode — never one set for both legs.
        #
        # GOVERNED reads `_PLAN_TASKS_GOVERNED_PM_APPROVAL_GATED_DISPOSITIONS`
        # ({backlogged, wont_do, spun_off}); LEGACY reads
        # `_PLAN_TASKS_PM_APPROVAL_GATED_DISPOSITIONS` ({backlogged,
        # wont_do}). Both legs keyed on the LEGACY set until 2026-09-04,
        # which left this write gate one member narrower than the lint that
        # reads the same records: `check_plan_tasks_grouping_approval`
        # (schema_validate.py) widened for `spun_off` on 2026-08-30 when
        # plan.schema.json 2.13.0 was vendored carrying
        # `grouping_approvals.spun_off`, and this gate did not follow. A
        # governed `spun_off` close therefore SUCCEEDED here and the record
        # it produced then failed the lint — a write path minting
        # lint-invalid plans, with the author left holding a warning and no
        # sanctioned repair. Reported from example-cockpit-repo via DoE-claude.
        #
        # The two-set split is the same one the constants themselves carry
        # (see their own banners): widening the LEGACY set in place would
        # retroactively invalidate 16 `spun_off` rows across 10 legacy plans
        # written correctly under DoE's 2026-08-05 relaxation, repairable
        # only by forging `pm_approved` assent no PM gave. A governed plan
        # opted into the block contract by carrying the key and can author a
        # `spun_off` block; a legacy plan cannot.
        #
        # D5's ordering lint and its closed-section concept consult no
        # disposition set at all — they run entirely off
        # `_PLAN_TASKS_GROUPING_BY_DISPOSITION` (schema_validate.py), which
        # already maps `spun_off` to its own grouping. Neither set reaches
        # them.
        _gated_dispositions = (
            _PLAN_TASKS_GOVERNED_PM_APPROVAL_GATED_DISPOSITIONS
            if governed
            else _PLAN_TASKS_PM_APPROVAL_GATED_DISPOSITIONS
        )
        closed_by_grouping: dict = {}
        for r in resolutions:
            disposition = r["disposition"]
            if disposition in _gated_dispositions:
                grouping = _PLAN_TASKS_GROUPING_BY_DISPOSITION[disposition]
                closed_by_grouping.setdefault(grouping, []).append((r["id"], disposition))

        for grouping, entries in closed_by_grouping.items():
            ids = [tid for tid, _ in entries]
            if governed:
                blocks = plan_fm.get("grouping_approvals")
                block = blocks.get(grouping) if isinstance(blocks, dict) else None

                if not isinstance(block, dict) or block.get("status") != "approved":
                    status = block.get("status", "pending") if isinstance(block, dict) else "absent"
                    raise MutateAbort(
                        f"resolve: closing task(s) {ids!r} puts them in the {grouping!r} "
                        f"grouping, which reads status {status!r}. {_GROUPING_APPROVAL_HINT}"
                    )

                # The digest must cover the membership AFTER the WHOLE batch
                # writes, not before it: the PM approves a cut-set, and
                # every row in this grouping this batch is closing is part
                # of the set they were shown. Checking a narrower (e.g.
                # single-row) membership would refuse the very first
                # application of a freshly approved multi-row cut-set.
                fresh = compute_grouping_digest(_prospective_rows(), grouping)
                if block.get("digest") != fresh:
                    raise MutateAbort(
                        f"resolve: the {grouping!r} grouping is approved, but over a "
                        f"different cut-set than this write would produce (approved "
                        f"{block.get('digest')!r}, this write {fresh!r}). Closing "
                        f"{ids!r} is not covered by that approval. {_GROUPING_APPROVAL_HINT}"
                    )

            else:
                for tid, disposition in entries:
                    row = rows_by_id[tid]
                    if row.get("pm_approved") is not True:
                        raise MutateAbort(
                            f"resolve: disposition {disposition!r} for task {tid!r} is a scope "
                            f"decision and needs the PM's ratification (D3/D4). "
                            f"{_LEGACY_PM_APPROVAL_HINT}"
                        )

        # Defect 2 fix (see _PLAN_TASKS_DETAIL_REQUIRED_DISPOSITIONS docstring
        # above): spun_off/backlogged/wont_do (C2, 2026-08-05) all require an
        # explicit caller-supplied disposition_detail, refused with the same
        # offer-shaped voice as the pm_approved gate. Runs BEFORE any
        # backlogged/spun_off dispatch below so an ungated call never
        # produces a queue/lesson write.
        for r in resolutions:
            disposition = r["disposition"]
            task_id = r["id"]
            disposition_detail = r.get("disposition_detail")
            if disposition in _PLAN_TASKS_DETAIL_REQUIRED_DISPOSITIONS and (
                not disposition_detail or not str(disposition_detail).strip()
            ):
                offer = _DISPOSITION_DETAIL_OFFER.format(task_id=task_id, disposition=disposition)
                raise MutateAbort(
                    f"resolve: disposition {disposition!r} for task {task_id!r} requires "
                    f"disposition_detail naming the PM's reasoning (D2/D4) — {offer}. "
                    "Refusing rather than recording a closed disposition with no rationale."
                )

        # case_against gate (leg 1, see _PLAN_TASKS_CASE_AGAINST_REQUIRED_
        # DISPOSITIONS docstring above): backlogged/wont_do also require an
        # explicit caller-supplied case_against, refused in this op's own
        # voice ahead of the vendored schema's presence-only check — a raw
        # "required field missing" names the missing key, not what it is
        # FOR. Runs alongside the disposition_detail gate, before any
        # backlogged dispatch below, so an ungated call never produces a
        # queue/lesson write.
        for r in resolutions:
            disposition = r["disposition"]
            task_id = r["id"]
            case_against = r.get("case_against")
            if disposition in _PLAN_TASKS_CASE_AGAINST_REQUIRED_DISPOSITIONS and (
                not case_against or not str(case_against).strip()
            ):
                offer = _CASE_AGAINST_OFFER.format(task_id=task_id, disposition=disposition)
                raise MutateAbort(
                    f"resolve: disposition {disposition!r} for task {task_id!r} requires "
                    f"case_against naming the strongest honest case for doing the work "
                    f"now, plus the EM's recommendation, confidence, and what would "
                    f"change it — not merely that the work is being cut — {offer}. "
                    "Refusing rather than recording a scope-cut with only the case for "
                    "cutting on the row."
                )

        # Every gate above has cleared for the WHOLE batch — only now does
        # any row mutate or any dispatch side effect (backlogged/spun_off)
        # fire.
        #
        # Phase 1: write every `disposition` field in the batch onto the
        # REAL `rows` (not a synthetic copy) — neither `_dispatch_backlogged`
        # nor `_dispatch_spun_off` reads a row's `disposition` field, so
        # this is safe to do ahead of dispatch. `case_against` rides along
        # in the same pass (rather than Phase 2, where `disposition_detail`
        # lands) so the D5 gate below and the schema check after dispatch
        # both see the finished row — neither dispatch function reads
        # `case_against` either.
        for r in resolutions:
            rows_by_id[r["id"]]["disposition"] = r["disposition"]
            case_against = r.get("case_against")
            if case_against is not None:
                rows_by_id[r["id"]]["case_against"] = case_against

        # Phase 1b (2026-08-06, D5 ordering-deadlock fix): reposition the
        # WHOLE spine into D5's required grouping order now that every
        # batch row's new `disposition` has landed — see
        # `_reposition_rows_for_d5`'s own docstring for why this must be
        # part of the same write rather than a separate "reorder the rows"
        # instruction (queue: state/bug-backlog/2026-08-06-plan-tasks-
        # mutate-d5-ordering-deadlocks-c223a7208a5a.yaml). `rows_by_id`
        # still keys the SAME row objects afterward — only the list's
        # order changes, so every lookup below by id is unaffected.
        rows = _reposition_rows_for_d5(rows)

        # Post-mutation D5 gate (C13, 2026-07-30): checked against the
        # spine as ACTUALLY mutated+repositioned above, before any
        # dispatch runs — never on the rendered `new_text` afterward.
        # `locked_rmw` covers the spine write only, so a D5 refusal raised
        # after `_dispatch_backlogged` had already appended a queue/lesson
        # entry would leave that entry on disk describing a deferral the
        # spine never recorded (the defect
        # `test_resolve_d5_refusal_fires_before_any_harvest_dispatch`
        # exists to prevent). Retained as a DEFENSIVE invariant assertion,
        # not the correctness mechanism itself: `_reposition_rows_for_d5`
        # is a stable sort keyed by the same rank tuple this lint checks,
        # so a batch that reaches this point cannot actually fail it — the
        # check exists to catch a future regression in that repositioning
        # logic, not a case any caller of `resolve` can currently trigger.
        mutated_start, mutated_end = result.span
        mutated_text = old_text[:mutated_start] + _dump_rows(rows) + old_text[mutated_end:]
        mutated_ordering_error = check_plan_tasks_ordering(mutated_text)
        if mutated_ordering_error is not None:
            raise MutateAbort(
                "resolve: refusing to apply this batch — the resulting spine would "
                f"violate D5 ({format_validation_errors([mutated_ordering_error])}) "
                "even after automatic repositioning. This should be unreachable — "
                "please report it as a bug in plan_tasks_mutate.py's "
                "_reposition_rows_for_d5. No disposition was written."
            )

        # Phase 2: the D5 gate above has cleared against the real mutated
        # spine — only now does any dispatch side effect fire.
        resolved_ids: list = []
        for r in resolutions:
            task_id = r["id"]
            disposition = r["disposition"]
            disposition_ref = r.get("disposition_ref")
            disposition_detail = r.get("disposition_detail")
            row = rows_by_id[task_id]

            # C5 (AC5): backlogged delegates row-routing to coordinator-
            # harvest-deferrals for THIS row and computes disposition_ref
            # from the result — any caller-supplied disposition_ref is
            # ignored for this disposition (see _dispatch_backlogged
            # docstring).
            #
            # C12 (AC17): spun_off gets its own computed producer, one step
            # lighter than backlogged's — the spinoff artifact is already
            # created by a prior write this op does not own (`/spinoff`), so
            # `_dispatch_spun_off` VERIFIES the caller-supplied
            # disposition_ref resolves to a real file and re-derives the
            # canonical repo-relative form, rather than recording the
            # caller's literal string unverified. See `_dispatch_spun_off`
            # docstring.
            effective_ref = disposition_ref
            if disposition == "backlogged":
                effective_ref = _dispatch_backlogged(row, task_id, old_text, worktree)
            elif disposition == "spun_off":
                effective_ref = _dispatch_spun_off(task_id, disposition_ref, worktree)

            if effective_ref is not None:
                row["disposition_ref"] = effective_ref
            if disposition_detail is not None:
                row["disposition_detail"] = disposition_detail
            resolved_ids.append(task_id)

        # `touched_ids=set(resolved_ids)` (2026-08-16, untouched-invalid-row
        # deadlock fix — the live repro this fixes: two rows in the SAME
        # spine each schema-invalid for reasons this batch did not touch
        # deadlocked each other's repair, because this call used to
        # validate and veto on every row in the spine. A row this batch
        # did not resolve could not have been made worse by it, so it must
        # not be able to block the write — see _validate_all's own
        # docstring.
        #
        # Review: code-reviewer — if `resolved_ids` is incomplete relative to
        # what Phase 1 already wrote in-memory (Phase 2 raised partway
        # through a `_dispatch_backlogged`/`_dispatch_spun_off` call), that
        # mismatch never reaches this line: `locked_rmw` discards `rows` and
        # writes nothing on ANY exception, so the mutate() closure never
        # returns and `_validate_all` is never called with the partial set.
        # `touched_ids` precision here is NOT what protects invariant (1)
        # ("never write a newly-invalid row") under partial failure —
        # `locked_rmw`'s all-or-nothing exception boundary is. A future
        # change there that lets a partial write through would silently
        # reintroduce that risk without this file changing at all.
        try:
            untouched_invalid = _validate_all(
                rows, governed=governed, touched_ids=set(resolved_ids), plan_created=plan_created,
            )
        except MutateAbort as exc:
            raise MutateAbort(f"resolve: {exc.args[0] if exc.args else exc}") from exc
        _state["warnings"] = _untouched_invalid_warnings(untouched_invalid)

        body_yaml = _dump_rows(rows)
        start, end = result.span
        new_text = old_text[:start] + body_yaml + old_text[end:]

        # Spine-resolution derivation (C1, 2026-08-14): "no row left open"
        # is knowable ONLY here — the sole site where a row's `disposition`
        # can move off `open` — so this is where the derived-landed check
        # is computed, against the SAME `rows` just validated and about to
        # be written, not a re-read of the (stale, pre-write) `old_text`.
        _state["all_resolved"] = all(
            _plan_tasks_row_disposition(row) != "open"
            for row in rows
            if isinstance(row, dict)
        )

        _state["applied"] = True
        if len(resolved_ids) == 1:
            _state["message"] = (
                f"resolve: {resolved_ids[0]!r} resolved to {resolutions[0]['disposition']!r}"
            )
        else:
            _state["message"] = f"resolve: resolved {len(resolved_ids)} task(s): {resolved_ids}"
        return new_text

    try:
        locked_rmw(path, mutate, repo_root=repo_root)
    except FileNotFoundError:
        return _err(f"resolve: plan not found: {plan_path}")
    except LockTimeout as exc:
        return _err(f"resolve: timed out waiting for file lock on {plan_path}: {exc}")
    except MutateAbort as exc:
        return _err(exc.args[0] if exc.args else "resolve: mutation aborted")

    result = _ok(_state["applied"], _state["message"], warnings=_state["warnings"])

    # C1 (2026-08-14, "landed fires at spine resolution"): the resolve
    # transaction above just committed. If it left no row `open`, derive
    # `status: landed` via the EXISTING sole writer
    # (`execute_plan_assemble.close_out_and_stamp._stamp_plan_landed`) —
    # this call site never writes `status:` itself and never reimplements
    # that function's terminal-status/idempotency guards (see the plan's
    # § Key decision: one writer, two callers). Imported lazily to avoid
    # loading `execute_plan_assemble`'s module graph on every resolve call
    # that doesn't need it.
    #
    # A stamp failure must not fail the row resolution the caller asked
    # for (AC3-adjacent: the caller's resolve already applied) — reported
    # in the result dict, never raised.
    #
    # Review: code-reviewer (P3 #3) -- `_stamp_plan_landed` is a leading-
    # underscore "private" symbol imported across a module boundary. That
    # is deliberate, not an oversight: the plan's one-writer decision (§
    # Key decision above) requires calling this EXACT existing primitive
    # rather than adding a public wrapper or a second implementation, so
    # the cross-module privacy violation is knowingly accepted here.
    if _state["applied"] and _state["all_resolved"]:
        try:
            from coordinator_core.execute_plan_assemble.close_out_and_stamp import (
                _stamp_plan_landed,
            )

            stamp_rc = _stamp_plan_landed(str(path))
            result["landed_stamp"] = "ok" if stamp_rc == 0 else "error"
        except Exception as exc:  # noqa: BLE001 — a derived side effect must never fail resolve
            _LOG.warning(
                "plan.tasks.mutate resolve: landed-stamp attempt failed for %s: %s",
                path,
                exc,
            )
            result["landed_stamp"] = f"error: {exc}"

    return result


# ---------------------------------------------------------------------------
# JSON-RPC handler
# ---------------------------------------------------------------------------


@register_op("plan.tasks.mutate")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC 'plan.tasks.mutate' handler — plan '## Tasks' task-spine mutations.

    MUTATING: writes to a docs/plans/*.md file's '## Tasks' fenced body block
    in-place. Does NOT git-commit.

    Required params:
        verb      (str) — one of: add-task | stamp | resolve.
        plan_path (str) — absolute or repo-relative path to the plan file
                           (must resolve under <worktree>/docs/plans/).

    Verb-specific required params:
        add-task : task    (dict) — the new row; must carry a non-empty 'id'.
        stamp    : updates (list[dict]) — [{"id": <id>, ...field-updates}, ...].
                   Refuses the WHOLE batch if any entry carries
                   disposition/disposition_ref/disposition_detail — use
                   resolve for those (D4).
        resolve  : EITHER id (str), disposition (str, required) — one of
                   open|coded|spun_off|backlogged|wont_do; disposition_ref
                   (str, optional); disposition_detail (str, required for
                   spun_off/backlogged — see below; optional otherwise);
                   case_against (str, required for backlogged/wont_do —
                   see below; optional otherwise) — a single-row resolve
                   (unchanged shape/behaviour), OR resolves (list[dict]) —
                   [{"id": ..., "disposition": ..., "disposition_ref": ...,
                   "disposition_detail": ..., "case_against": ...}, ...]
                   for an ATOMIC BATCH of N rows in one write (C13,
                   2026-07-30). `resolves` wins if both shapes are present.
                   A single-row resolve is a batch of one internally — no
                   separate code path, no behaviour change for existing
                   callers.

                   Refuses (the WHOLE call/batch; no partial writes) a
                   closed disposition (spun_off/backlogged/wont_do) on any
                   row unless its authorization signal clears — on a
                   LEGACY plan, the row's own pm_approved: true (checked
                   per row); on a GOVERNED plan, its grouping's
                   status: approved PLUS a digest match against the
                   membership the WHOLE BATCH is about to produce, checked
                   once per grouping touched by the batch even when the
                   batch spans multiple groupings (D3/D4). Also refuses
                   spun_off/backlogged lacking a non-empty
                   disposition_detail on any row — D4 requires the PM's
                   verbatim reasoning there, not a synthesised placeholder
                   (Defect 2 fix). Also refuses backlogged/wont_do lacking
                   a non-empty case_against on any row — the both-sides ask
                   (leg 1, 2026-08-06): disposition_detail carries the case
                   FOR closing, case_against carries the strongest honest
                   case for doing the work now, so a deferral surfaced to
                   the PM is a real decision rather than an ID list.
                   disposition == "backlogged" IGNORES a
                   caller-supplied disposition_ref and instead delegates
                   row-routing to coordinator-harvest-deferrals for that
                   row, recording the resulting queue/lesson entry path as
                   disposition_ref (C5, AC5) — each backlogged row in a
                   batch delegates independently.

    Returns:
        {"exit_code": 0, "applied": bool,  "message": str, "warnings": list} on
            success or no-op — "warnings" is present only when non-empty (see
            `_ok`'s own docstring): it names any pre-existing schema-invalid
            row this call did not touch (2026-08-16, untouched-invalid-row
            deadlock fix).
        {"exit_code": 1, "applied": False, "error":   str} on error.

    P9 WORKTREE DERIVATION: repo_root arrives as the git common dir
    (<worktree>/.git). main_worktree_root(repo_root) derives the worktree root
    used to resolve relative plan_path values and to containment-check under
    docs/plans/.
    """
    verb = (params.get("verb") or "").strip()
    plan_path = (params.get("plan_path") or "").strip()

    if not verb:
        return _err("plan.tasks.mutate: 'verb' is required (add-task | stamp | resolve)")
    if not plan_path:
        return _err("plan.tasks.mutate: 'plan_path' is required")

    if repo_root is None:
        return _err(
            "plan.tasks.mutate: repo_root is required "
            "(no founding root available — handler called without socket-authoritative common_dir)"
        )

    worktree = main_worktree_root(repo_root)

    if verb == "add-task":
        task = params.get("task")
        return await asyncio.to_thread(_add_task, plan_path, task, worktree, repo_root)

    if verb == "stamp":
        updates = params.get("updates")
        return await asyncio.to_thread(_stamp, plan_path, updates, worktree, repo_root)

    if verb == "resolve":
        # C13 (2026-07-30, batch resolve): a caller supplies EITHER the
        # single-row id/disposition/disposition_ref/disposition_detail
        # params (unchanged shape, unchanged behaviour — a batch of one),
        # OR a `resolves` list for a multi-row atomic batch. The two shapes
        # are mutually exclusive on the wire; `resolves` wins if both are
        # present (a caller sending both is almost certainly a mistake, but
        # there is exactly one sane reading: the explicit batch).
        resolves_param = params.get("resolves")
        if resolves_param is not None:
            if not isinstance(resolves_param, list) or not resolves_param:
                return _err(
                    "plan.tasks.mutate: 'resolves' must be a non-empty list of "
                    "{id, disposition, disposition_ref, disposition_detail, "
                    "case_against} dicts"
                )
            resolutions = []
            for entry in resolves_param:
                if not isinstance(entry, dict):
                    return _err(
                        "plan.tasks.mutate: every 'resolves' entry must be a dict "
                        "with a non-empty 'id'"
                    )
                resolutions.append(
                    {
                        "id": (entry.get("id") or "").strip(),
                        "disposition": entry.get("disposition"),
                        "disposition_ref": entry.get("disposition_ref"),
                        "disposition_detail": entry.get("disposition_detail"),
                        "case_against": entry.get("case_against"),
                    }
                )
        else:
            resolutions = [
                {
                    "id": (params.get("id") or "").strip(),
                    "disposition": params.get("disposition"),
                    "disposition_ref": params.get("disposition_ref"),
                    "disposition_detail": params.get("disposition_detail"),
                    "case_against": params.get("case_against"),
                }
            ]
        return await asyncio.to_thread(_resolve, plan_path, resolutions, worktree, repo_root)

    return _err(
        f"plan.tasks.mutate: unknown verb {verb!r} — supported: add-task, stamp, resolve"
    )
