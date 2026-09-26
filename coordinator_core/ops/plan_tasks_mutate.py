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

from coordinator_core.bin_lib_binding import ensure_bin_lib_bound
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
from coordinator_core.module_load_lock import held_during_load
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.wire_paths import rel_id


def _ok(applied: bool, message: str, *, warnings: Optional[list] = None) -> dict:
    reply = {"exit_code": 0, "applied": applied, "message": message}
    if warnings:
        reply["warnings"] = warnings
    return reply


def _err(message: str) -> dict:
    return {"exit_code": 1, "applied": False, "error": message}


class _PathNotContained(Exception):
    pass


def _resolve_path(plan_path: str, worktree: Path) -> Path:
    p = Path(plan_path)
    if not p.is_absolute():
        p = worktree / p
    allowed_roots = [worktree / "docs" / "plans"]
    resolved = contained_path(p, allowed_roots)
    if resolved is None:
        raise _PathNotContained(f"plan_path escapes docs/plans/: {plan_path!r}")
    return resolved


_TASKS_HEADING_LINE = "## Tasks"
_FENCE_OPEN = "```yaml plan-tasks\n"
_FENCE_CLOSE = "\n```"


def _has_tasks_heading(source: str) -> bool:
    return _compile_heading_re("Tasks").search(source) is not None


def _synthesize_fence_under_heading(source: str, body_yaml: str) -> str:
    heading_re = _compile_heading_re("Tasks")
    match = heading_re.search(source)
    assert match is not None
    insert_at = match.end()
    fenced = f"\n\n{_FENCE_OPEN}{body_yaml}{_FENCE_CLOSE}\n"
    return source[:insert_at] + fenced + source[insert_at:]


def _synthesize_tasks_section(source: str, body_yaml: str) -> str:
    separator = "" if source.endswith("\n\n") else "\n\n"
    section = f"{_TASKS_HEADING_LINE}\n\n{_FENCE_OPEN}{body_yaml}{_FENCE_CLOSE}\n"
    return source + separator + section


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
    not here. That module's `check_plan_tasks_source` is genuinely THREE
    independent copies away from this one — it hardcodes claude-klabauter's own
    vendored schema, while the write guards deliberately resolve DoE's
    vendored corpus copy (which its own docstring notes has drifted from
    claude-klabauter's), and it short-circuits on the first error where the guards
    need every row's errors. As of P084-C1/C2, `check_plan_tasks_source`
    and both write guards share ONE statement of which spine-level legs run
    and in what order (`PLAN_TASKS_SPINE_SEQUENCE` /
    `plan_tasks_spine_errors`, in `schema_validate.py`) — but that sharing
    covers the WHOLE-SPINE legs (integrity, ordering, grouping-approval),
    not this function's per-row shape-then-cross-field sequence, which
    remains duplicated here and (independently) inside each write guard's
    own per-row loop. What IS shared, and does keep the per-row copies from
    disagreeing about MEANING, are the low-level primitives each copy
    calls: `_plan_tasks_schema_without_pm_approved_required` (the governed
    schema derivation), `is_governed_plan`, and `_apply_cross_field_rules`
    — a row cannot be "governed" in one copy and "legacy" in another. But
    the per-row validation SEQUENCE itself — which schema to pick, when to
    run cross-field rules, how to merge the two error lists — stays
    duplicated across this function and the write guards' per-row loops,
    and nothing enforces those copies stay in lockstep if one of them
    changes. This function's own precondition call to
    `check_plan_tasks_ordering` (in `_resolve`, below) stays a direct
    single-leg call rather than routing through the accumulating driver —
    it is a precondition on existing on-disk order, not a spine validation
    pass, and routing it through the driver would make a mutation refuse on
    defects it does not own.
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
    return yaml.dump(
        rows,
        Dumper=_PlanTasksDumper,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=4096,
    )


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

        plan_fm = parse_frontmatter(old_text).get("frontmatter")
        plan_created = plan_fm.get("created") if isinstance(plan_fm, dict) else None
        governed = is_governed_plan(plan_fm) if isinstance(plan_fm, dict) else False

        if result.status is LocateStatus.ABSENT:
            rows: list = []
            new_rows = rows + [task]
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

        plan_fm = parse_frontmatter(old_text).get("frontmatter")
        plan_created = plan_fm.get("created") if isinstance(plan_fm, dict) else None
        governed = is_governed_plan(plan_fm) if isinstance(plan_fm, dict) else False

        rows = _parse_rows_or_abort(result.body, "stamp")

        rows_by_id = {row.get("id"): row for row in rows if isinstance(row, dict)}

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


# RETIRED 2026-07-29 — `_PM_APPROVAL_OFFER` lived here and is deliberately
# `_GROUPING_APPROVAL_HINT` from schema_validate, which is written as an

# `pm_utterance` field anywhere in their schema — `_GROUPING_APPROVAL_HINT`
# REWRITTEN 2026-08-12 (DoE ruling, exit 1 —
# tripwire A-REFUSAL-MAY-NOT-CLAIM-IMPOSSIBILITY-IT-CANNOT-ENFORCE). The
# `_PM_APPROVAL_OFFER` banner below correctly killed, because it teaches a
# well-meaning EM to satisfy the field. `_GROUPING_APPROVAL_HINT` above is
_LEGACY_PM_APPROVAL_HINT = (
    "Recording pm_approved: true on this row asserts that the PM ratified "
    "this specific cut. Nothing in this session can verify that, so stamping "
    "it without their word puts a false statement in the record. "
    "plan-tasks-stamp sets the field once they have ruled."
)

_PLAN_TASKS_DETAIL_REQUIRED_DISPOSITIONS = frozenset({'spun_off', 'backlogged', 'wont_do'})

_DISPOSITION_DETAIL_OFFER = (
    "pass disposition_detail naming the PM's reasoning "
    "(--verb resolve --id {task_id} --disposition {disposition} "
    "--disposition-detail \"<why>\"), then re-run resolve"
)

# scope-cut dispositions as `_PLAN_TASKS_DETAIL_REQUIRED_DISPOSITIONS`
# itself of. The vendored schema (1.6.0) makes this field REQUIRED via
_PLAN_TASKS_CASE_AGAINST_REQUIRED_DISPOSITIONS = frozenset({'backlogged', 'wont_do'})

_CASE_AGAINST_OFFER = (
    "pass case_against naming the strongest honest case for doing the "
    "work now (--verb resolve --id {task_id} --disposition {disposition} "
    "--case-against \"<why not cut>\"), then re-run resolve"
)


# coordinator_core.workday_complete.apply._CLI_SCRIPT_ROOT's established
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

    The check-cache/register/exec sequence runs under `module_load_lock.
    held_during_load(module_name)` so a second concurrent caller (warm
    engine, shared threads) blocks on the first's `exec_module` rather than
    racing it over the same `sys.modules[module_name]` slot — see that
    module's docstring for the half-executed-module hazard this closes.
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
    with held_during_load(module_name):
        if _HARVEST_MODULE is not None:
            return _HARVEST_MODULE
        ensure_bin_lib_bound(str(_HARVEST_CLI_PATH.parent))
        spec = importlib.util.spec_from_file_location(module_name, _HARVEST_CLI_PATH)
        if spec is None or spec.loader is None:
            raise MutateAbort(
                f"resolve: could not load coordinator-harvest-deferrals from {_HARVEST_CLI_PATH}"
            )
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(module_name, None)
            raise
        _HARVEST_MODULE = module
        return module


def _find_evidence_file(key: str, search_dirs: list) -> Optional[str]:
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

        # EXISTING row order already violates D5", checked against
        # precondition does not relax D5's invariant on the RESULTING
        rows = _parse_rows_or_abort(result.body, "resolve")

        rows_by_id = {row.get("id"): row for row in rows if isinstance(row, dict)}

        for r in resolutions:
            if rows_by_id.get(r["id"]) is None:
                raise MutateAbort(f"resolve: task id not found: {r['id']!r}")

        # `_PM_APPROVAL_OFFER` banner above for why).
        #   - GOVERNED (frontmatter carries the `grouping_approvals` key at
        plan_fm = parse_frontmatter(old_text).get("frontmatter")
        governed = is_governed_plan(plan_fm) if isinstance(plan_fm, dict) else False
        plan_created = plan_fm.get("created") if isinstance(plan_fm, dict) else None

        new_disposition_by_id = {r["id"]: r["disposition"] for r in resolutions}

        def _prospective_rows() -> list:
            return [
                {**row, "disposition": new_disposition_by_id[row["id"]]}
                if isinstance(row, dict) and row.get("id") in new_disposition_by_id
                else row
                for row in rows
                if isinstance(row, dict)
            ]

        # GOVERNED reads `_PLAN_TASKS_GOVERNED_PM_APPROVAL_GATED_DISPOSITIONS`
        # `_PLAN_TASKS_PM_APPROVAL_GATED_DISPOSITIONS` ({backlogged,
        # governed `spun_off` close therefore SUCCEEDED here and the record
        # `_PLAN_TASKS_GROUPING_BY_DISPOSITION` (schema_validate.py), which
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

        for r in resolutions:
            rows_by_id[r["id"]]["disposition"] = r["disposition"]
            case_against = r.get("case_against")
            if case_against is not None:
                rows_by_id[r["id"]]["case_against"] = case_against

        rows = _reposition_rows_for_d5(rows)

        # spine as ACTUALLY mutated+repositioned above, before any
        # exists to prevent). Retained as a DEFENSIVE invariant assertion,
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

        resolved_ids: list = []
        for r in resolutions:
            task_id = r["id"]
            disposition = r["disposition"]
            disposition_ref = r.get("disposition_ref")
            disposition_detail = r.get("disposition_detail")
            row = rows_by_id[task_id]

            # `_dispatch_spun_off` VERIFIES the caller-supplied
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

    # `status: landed` via the EXISTING sole writer
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


# JSON-RPC handler


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
