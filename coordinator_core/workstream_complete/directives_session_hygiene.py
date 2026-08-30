"""
coordinator_core.workstream_complete.directives_session_hygiene — the
session-hygiene and completeness-checklist builders for the
`workstream-complete-assemble` computed-skill engine.

Purpose: computes Step 2.8 (orientation-pinboard append), Step 2.95's
machine-local regeneratability sub-check, and Step 2.96 (the
completeness-checklist advisory WARN gate) for
`coordinator_core.workstream_complete`'s `brief()` assembly seam (C3) —
two real `directives[]` entries plus one read-only `gates.*` fact, per
each sub-step's own mutating-vs-read-only shape (see § Design note below).

This is the other half of the original single "directives_scratch_memo.py"
row split at review (F6/eng-director) — `directives_memo_lifecycle.py`
(C2c) keeps the memo-lifecycle + scratch-self-clean half; this module
keeps session hygiene: the orientation pinboard (Step 2.8), the
machine-local regeneratability sub-check (Step 2.95), and the
completeness-checklist WARN gate (Step 2.96). Both halves were split out
of one grab-bag row that bundled four unrelated concerns under
deleted-ordinal-spine step-adjacency, not domain cohesion
(D-4/F6, docs/plans/2026-07-26-workstream-complete-computed-frontage.md,
chunk C2i).

This module is one of seven siblings (directives_lessons_plan.py,
directives_completion.py, directives_memo_lifecycle.py,
directives_review.py, directives_commit_tail.py, judgments.py) built
under the new intra-claude-klabauter multi-module-assembler convention this plan
sets: `__init__.py` is retained as the assembly + CLI seam ONLY, and
every submodule exposes pure, `__init__`-independent builder functions —
this is the first multi-module assembler in the tree (D-4; the
convention is registered centrally at C10,
coordinator/docs/wiki/computed-skills-conversion-checklist.md, so the
next converter inherits it deliberately rather than by imitation).

Step 2.96 is the LIVE completeness-checklist advisory WARN gate
(DoE-claude coordinator/skills/workstream-complete/SKILL.md, Step 2.96,
~lines 602-646) and is this module's primary acceptance condition, not a
peripheral inclusion: a live gate needs a directive/gate home before the
SKILL body is rewritten (C5), or it silently drops out of the ceremony
instead of converting — the coverage-checker's actual catch that
motivated splitting this row out of C2c in the first place
(G1/plan-coverage-checker).

Design note — directives[] vs gates.*: `directives[].cli` values must
each name a real, on-disk, mutating CLI never invoked in-process (see
this package's own `test_workstream_complete.py`
`test_directives_only_name_known_real_clis_and_never_invoke_them`, and
AC2's phantom-verb consumes-manifest guard). Step 2.8's pinboard append
and Step 2.95's regeneratability sub-check each have exactly that — a
real CLI this module never runs, only names. Step 2.96 has NO such CLI
on disk (grepped: no `wsc-*completeness*`/`*checklist*` entry exists
alongside `archive-session-scope.py`/`wsc-tail.py`/`wsc-coverage-gate-runner.py`) —
its three census sub-steps (locate+parse, cross-reference+count, emit
the fixed WARN template) are ALL pure reads: parse already-on-disk
handoff frontmatter, cross-reference against caller-supplied task/waiver
state, and render a fixed string. Modeling that as a phantom
`directives[].cli` value would violate AC2's own guard the moment C1's
contract test runs it against `CONSUMES_MANIFEST`. It is exposed instead
as `compute_completeness_checklist_gate(...)`, a read-only computed fact
mirroring the existing `gates.session_shape` shape
(`coordinator_core/workstream_complete/__init__.py`'s
`compute_session_shape_gate`) — the same "compute a fact, don't invent a
directive" disposition `workday_complete/brief.py`'s own negative-spec
already documents for its Step 2/Step 5 non-directive facts. This is a
decision this chunk made that the plan body left open (it names the
directive ids `d-parse-completeness-checklist` /
`d-count-unverified-checklist-items` / `d-emit-completeness-warn` per the
census's DR-090 extraction-unit classification, not per envelope-key
placement) — flagged explicitly for C3's assembly seam to wire under
whichever `gates.*` key it settles on.

Consumes (orchestrates, reimplements none):
    coordinator/bin/regenerate-orientation-cache (--pinboard-only mode)
        -> d-append-orientation-pinboard's directives[].cli.
    coordinator/bin/check-machine-local-regeneratability.py
        -> d-check-machine-local-regeneratability's directives[].cli.
    coordinator_core.frontmatter.schema_validate.parse_frontmatter
        -> reads the consumed handoff's `completeness_checklist:` YAML
        sequence off disk (Step 2.96 condition 2 / sub-step 1-2).
    coordinator_core.ops.parse_completeness_item.parse_completeness_item
        -> per-item grammar parse (`<class>: <assertion> [probe: ...]`,
        class in {live, restart-gated}) — single source of parse+classify
        already shipped for the pickup skill and its own co-located test
        suite; called in-process here rather than re-derived a second
        time.

Negative-spec:
    - Does NOT take the `SessionShapeGate` NamedTuple `__init__.py`
      defines as a parameter anywhere in this module. `__init__.py`
      imports this module (assembly direction); this module importing
      back from `__init__.py` for a type would be circular. Callers pass
      the plain fields the gate already carries (`disposition: str`,
      `consumed_handoff_text: Optional[str]`), not the gate object
      itself.
    - Does NOT decide pinboard note content, which orientation-doc rows
      to touch, or whether an inline waiver was actually given this
      session (`pinboard-note-content`, `orientation-doc-row-updates`,
      `inline-waiver-recognition` judgment_points, D-4) — those are
      C2f's (`judgments.py`). This module only turns an ALREADY-DECIDED
      pinboard note / waiver set into directive/gate shape.
    - Does NOT emit anything for Step 2.95's open-ended cross-cutting
      self-check (`cross-cutting-check` judgment_point, also C2f's) —
      that question has no corresponding atomic CLI or computable fact
      in the consumes-manifest; only its structured machine-local
      regeneratability sub-check does.
    - Does NOT execute a checklist item's `probe:` shell command.
      Parsing an item's grammar is not running it — probe execution (if
      any) is the EM's own verification act, never this read-only
      assembler's.
    - Does NOT mutate the consumed handoff, the orientation cache, or
      Tasks-API state, and never calls `git fetch`. Every mutating
      action this module names is an existing CLI for the apply half to
      invoke, never invoked in-process here.

Idempotence-mechanism classification (C3,
docs/plans/2026-08-08-wsc-judgment-directive-boundary.md AC5/AC6):
`already_satisfied` (`apply.py::execute_directives`) has NO PRODUCER
anywhere in this package outside a test fixture (`test_apply.py`) — every
`_directive()` helper across the six `directives_*.py` siblings defaults
it to `False` and no production call site overrides it. Four DISTINCT
mechanisms are in play; recorded here (not split across the five
`directives_*.py` siblings this chunk does not own) so the classification
stays internally consistent and reviewable in one read:

  M1 envelope `already_satisfied` short-circuit (`apply.py`) — computed
     from disk, mirroring `baton_assemble/__init__.py`'s `d1_already_
     satisfied` + `already_satisfied_reason` shape. THIS MODULE'S TWO
     directives now use it (see below) — no other builder in the six
     siblings does (grepped: zero `already_satisfied=True` sites outside
     `test_apply.py`).
  M2 CLI-level re-entrancy — no envelope short-circuit; the DISPATCHED
     CLI itself is safe to re-fire. VERIFIED (read the invoked CLI/op,
     not accepted from a docstring) for:
       - `d-claim-plan-execution-lock` (directives_lessons_plan.py
         build_plan_claim_and_stamp_directives) — `wsc-coverage-gate-
         runner.py:189` own contract: "claim-plan — 0 (claimed/
         re-entrant/stale-takeover)".
       - `d-complete-entry` (directives_completion.py
         build_complete_entry_directive) — `coordinator/bin/coordinator-
         complete-entry.py:43`: "0 — entry written, OR idempotent no-op
         (entry already exists for this chain slug)".
       - `d-harvest-deferrals-<n>` (directives_lessons_plan.py
         build_deferral_harvest_directives, one per governing plan, so
         this id carries an ordinal suffix and never matches an
         exact-id lookup) — `coordinator/bin/
         coordinator-harvest-deferrals:658-980`: dedup key `harvest-key:
         <plan_id>:<row_id>`, `_already_harvested` skip-if-present.
       - `d-release-plan-claim` (directives_commit_tail.py
         build_release_plan_claim_directive) —
         `coordinator_core/session/claims.py::release_artifact:594`:
         "ALWAYS returns True... the no-op paths are successes, not
         errors" (not-the-holder / claim-already-absent -> no-op).
     UNVERIFIED beyond a bare docstring assertion — recorded as
     `unverified-re-entrancy`, settled by reading the named CLI's own
     dedup/detection logic:
       - `d-stamp-plan-implemented` (directives_lessons_plan.py, same
         builder as d-claim-plan-execution-lock above, but the re-
         entrancy prose covers ONLY the claim directive, not this one) —
         settled by reading `archive-stamp-cli stamp-plan-implemented`'s
         own write path for an existing-stamp guard.
       - `d-reconcile-completion-commits` (directives_completion.py
         build_reconcile_completion_commits_directive) — docstring says
         "Detection (which commits are new)... [is] the CLI's own job"
         but never asserts idempotence — settled by reading
         `reconcile-completion-commits.py`'s Steps 1-2 client-side
         validation for a re-run-safe append guard.
  M3 hardcoded `already_satisfied: False` dict literal, emitted
     unconditionally once the builder fires — re-fires on every pass,
     unverified whether the dispatched CLI itself tolerates that. This is
     every OTHER directive across the five sibling modules this chunk
     does not own: `d-close-tail-args`, `d-run-wsc-tail`, `d-emit-
     cadence` (directives_commit_tail.py); `d-fold-execution-
     observations` (directives_completion.py); lesson-capture entries
     (directives_lessons_plan.py build_lesson_capture_directives);
     `d-flip-memo-status` entries
     (directives_memo_lifecycle.py); `d-run-review-brightline-gate`,
     per-slice `d-freeze-and-
     dispatch-review-partition-*`, `d-freeze-and-dispatch-review-
     partition-integrator`, `d-run-chain-coverage-gate`, `d-write-
     review-trail`, `d-run-ubt-pending-check` (when emitted; the table
     previously named this `d-check-ubt-pending`, a spelling the actual
     builder — `build_ubt_pending_check_directive`, directives_review.py —
     never emits; caught by this chunk's own table-vs-reality contract
     test, `test_apply.py`
     `test_idempotence_table_directive_ids_are_still_emitted_by_their_builders`),
     `d-classify-
     dispatch-shape` (when emitted) (directives_review.py). `d-run-wsc-
     tail` is the highest-risk member of this set — it is the ceremony's
     own commit — and is flagged, not silently left, for a follow-up
     chunk: closing it needs either a real disk check (has this session's
     commit already landed on this branch tip) or a verified re-
     entrancy read of `wsc-tail.py` itself, neither of which is this
     chunk's in-scope surface.
  M4 structural non-emission — the builder returns `None`/`[]` when its
     governing condition is false, so the directive never enters the
     envelope at all; distinct from M3 because there is nothing to
     re-fire. Applies to the SKIP branch of: `build_pinboard_directive`
     (this module, below), `build_release_plan_claim_directive` (no
     governing_plan_slug), `build_fold_execution_observations_directive`
     (no plan_path), `build_plan_claim_and_stamp_directives` (no
     governing plan — `governing_plan_predicate` False), `build_ubt_
     pending_check_directive` (`applies=False`), `build_classify_
     dispatch_shape_directive` (no plan_file). The EMIT branch of each of
     these falls under M2 or M3 per the entries above/below — M4 only
     covers the "never entered the envelope" case, not what happens once
     it does.

Snapshot addendum (ceremony.wsc_tail / completion.reconcile_commits kills,
2026-08-23): `d-release-plan-claim`, `d-close-tail-args`, `d-run-wsc-tail`,
`d-emit-cadence` (directives_commit_tail.py) and `d-reconcile-completion-
commits` (directives_completion.py) — cited above under M2, M3, and M4 as
of this classification's original authoring — no longer exist; their
builders were removed. The classification above is left as the historical
record it was written as, not corrected line-by-line.

This module's own two directives, BEFORE this chunk, were both M3
(dict-literal `"already_satisfied": False`, `:156`/`:178` below — cited
by the plan's own residual-defect section). After this chunk:
  - `d-append-orientation-pinboard` gains an M1 check, NOT YET WIRED to
    fire in production (see the unwired-caveat two sentences below —
    do not read this bullet's headline alone as "closed"): `build_pinboard_
    directive` gained an optional `existing_pinboard_line` parameter
    (the caller's already-read current `## Pinboard` line, matching this
    module's existing no-disk-I/O convention — `compute_completeness_
    checklist_gate`'s `consumed_handoff_text` param is the same shape)
    and computes `already_satisfied` by comparing it to `pinboard_note`
    AFTER applying the same first-line/400-char truncation
    `patch_pinboard_only` applies at write time (`_written_pinboard_note`
    — a raw comparison against the untruncated note would report
    `already_satisfied=False` for a note the write path would in fact
    land byte-identically; the reviewer's P3 finding on this asymmetry,
    coordinator:code-reviewer, is fixed here).
    VERIFIED against `coordinator_core/orientation/regenerate_cache.py::
    patch_pinboard_only`: the write is a whole-section REGEX REPLACE
    (`_PINBOARD_SECTION_RE.sub(..., count=1)`), never an append, so
    re-firing with a note whose truncated form is unchanged would write
    byte-identical Pinboard bytes — the real value of the M1 check here
    is skipping the Housekeeping re-derive + `clear_failures_log` side
    effect `patch_pinboard_only` performs on every non-check write, not
    preventing a corruption `patch_pinboard_only` could not already
    survive on its own. `__init__.py` (out of this chunk's scope; see
    brief) does NOT YET thread `existing_pinboard_line` through its
    `build_pinboard_directive` call site — the check is real and tested
    (`test_apply.py`) but currently DEAD CODE on the shipped/dispatched
    path, still M3 (`already_satisfied` always `False`) there until a
    follow-up chunk wires it. That follow-up is tracked at
    `state/bug-backlog/2026-08-08-the-pinboard-directive-s-new-satisfactio-84428eba910c.yaml`,
    not left untracked for the next reader to rediscover.
  - `d-check-machine-local-regeneratability` stays M3 in dict shape but
    is now DELIBERATELY, PERMANENTLY False rather than an oversight: the
    invoked CLI (`coordinator/bin/check-machine-local-
    regeneratability.py`, confirmed by reading it) is READ-ONLY —
    "reads the [regeneratability] TOML table... Findings go to stderr;
    silent on a clean registry" and never opens a file for writing. There
    is no disk artifact whose presence would mean "already done", so no
    real `already_satisfied` check can exist for it; re-firing is
    unconditionally safe because the directive performs no mutation at
    all, which is a stronger property than idempotence, not a gap in it.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, NamedTuple, Optional

from coordinator_core.frontmatter.schema_validate import parse_frontmatter
from coordinator_core.ops.ceremony.wsc_disposition import PREDECESSOR_CONSUMED, canonicalize
from coordinator_core.ops.parse_completeness_item import parse_completeness_item

# ---------------------------------------------------------------------------
# Step 2.8 — orientation-pinboard append (real CLI, real directive)
# ---------------------------------------------------------------------------

_PINBOARD_CLI = "regenerate-orientation-cache"

#: Mirrors `coordinator_core.orientation.regenerate_cache.patch_pinboard_only`'s
#: own write-time transform (`_first_line(pinboard)[:400]`) exactly, so the
#: `already_satisfied` comparison below never compares a raw multi-line/
#: >400-char `pinboard_note` against a first-line/400-char-truncated
#: `existing_pinboard_line` — that mismatch would only make the check report
#: `False` (unnecessary re-write) for a note the write path would actually
#: land byte-identically, never the unsafe direction (Review: coordinator:
#: code-reviewer flagged the untruncated comparison, P3).
_PINBOARD_WRITE_TRUNCATE_CHARS = 400


def _written_pinboard_note(pinboard_note: Optional[str]) -> str:
    if not pinboard_note:
        return ""
    first_line = pinboard_note.splitlines()[0] if pinboard_note.splitlines() else ""
    return first_line[:_PINBOARD_WRITE_TRUNCATE_CHARS]


def build_pinboard_directive(
    orientation_cache_exists: bool,
    pinboard_note: Optional[str],
    existing_pinboard_line: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Step 2.8's single mutation: append one pinboard line via
    `regenerate-orientation-cache --pinboard-only`.

    `pinboard_note` is the ALREADY-DECIDED one-line note (the
    `pinboard-note-content` judgment_point's resolved text, C2f/EM's own
    call) — this function only wires it into directive shape, it never
    decides content. Returns `None` (skip silently, per the SKILL's own
    "If nothing pinboard-worthy, do nothing... If the cache file doesn't
    exist, skip" text) when there is no note to write or the cache file
    the `--pinboard-only` fast path requires is absent — a structural
    (M4) non-emission, not the `already_satisfied` mechanism below.

    `existing_pinboard_line` (C3, AC5/AC6 — see module docstring's
    idempotence-mechanism classification) is the caller's ALREADY-READ
    current `## Pinboard` line (e.g.
    `coordinator_core.orientation.regenerate_cache.read_existing_
    pinboard`'s return) — this function stays disk-I/O-free per its own
    module convention (mirrors `compute_completeness_checklist_gate`'s
    `consumed_handoff_text` param shape) and never reads the cache file
    itself. When supplied and equal to `pinboard_note` AFTER applying the
    SAME first-line/400-char truncation `patch_pinboard_only` applies at
    write time (`_written_pinboard_note` below — mirrors
    `_first_line(pinboard)[:400]`, not a raw comparison), the directive is
    `already_satisfied` (M1): `patch_pinboard_only` replaces the whole
    Pinboard section on every write, so a re-fire whose truncated note
    matches would write byte-identical bytes there — skipping instead
    avoids the Housekeeping re-derive + failures-log clear side effect
    that write also performs. `None` (the default, and every call site
    until a caller threads this parameter through) means "not verified"
    and `already_satisfied` stays `False` — never inferred as satisfied
    from absence.
    """
    if not pinboard_note:
        return None
    if not orientation_cache_exists:
        return None
    already_satisfied = existing_pinboard_line is not None and existing_pinboard_line == _written_pinboard_note(pinboard_note)
    directive: dict[str, Any] = {
        "id": "d-append-orientation-pinboard",
        "cli": _PINBOARD_CLI,
        "args": ["--invoker", "workstream-complete", "--pinboard-only", pinboard_note],
        "depends_on": None,
        "already_satisfied": already_satisfied,
    }
    if already_satisfied:
        directive["already_satisfied_reason"] = (
            f"the cache's current ## Pinboard line already reads {existing_pinboard_line!r}, "
            f"matching what patch_pinboard_only would write for this note ({_written_pinboard_note(pinboard_note)!r}) — "
            "patch_pinboard_only replaces the whole section on every write, so re-firing "
            "would write identical bytes; skipping avoids its Housekeeping re-derive + "
            "failures-log clear side effect"
        )
    return directive


# ---------------------------------------------------------------------------
# Step 2.95 sub-check — machine-local regeneratability (real CLI, real directive)
# ---------------------------------------------------------------------------

_MACHINE_LOCAL_REGEN_CLI = "check-machine-local-regeneratability"


def build_machine_local_regeneratability_directive() -> dict[str, Any]:
    """Step 2.95's structured sub-check: an unconditional, offer-shaped
    peer slot alongside the open-ended cross-cutting question (which is a
    judgment_point, not this module's concern). `exit 0` always; the
    named CLI writes findings to stderr only and is silent on a clean
    registry — this directive always fires, never gated.

    `already_satisfied` stays hardcoded `False` PERMANENTLY, by design,
    not oversight (C3, AC5 — see module docstring's idempotence-mechanism
    classification): `check-machine-local-regeneratability.py` is
    READ-ONLY (confirmed by reading it — it opens the machine-local
    registry, prints findings to stderr, never opens anything for
    writing). There is no disk artifact whose presence would mean
    "already done", so no real satisfaction check can exist for this
    directive — re-firing it every pass is unconditionally safe because
    it performs no mutation at all, a stronger guarantee than idempotence
    tracking would give it.
    """
    return {
        "id": "d-check-machine-local-regeneratability",
        "cli": _MACHINE_LOCAL_REGEN_CLI,
        "args": [],
        "depends_on": None,
        "already_satisfied": False,
    }


# ---------------------------------------------------------------------------
# Step 2.96 — completeness-checklist advisory WARN (read-only computed gate)
# ---------------------------------------------------------------------------

_WAIVED_ITEM_KEY = "waived_items"
_DONE_TASK_KEY = "completed_checklist_task_ids"

#: The `decisions` keys `compute_completeness_checklist_gate` reads —
#: declared once so a caller (`__init__.py`'s `preflight.decisions_template`
#: composition) can import and union this tuple rather than hand-copying
#: the key list. See AC3
#: (docs/plans/2026-07-29-workstream-complete-the-envelope-names-t.md):
#: the arg-builder and the template read this SAME constant.
FREE_VALUE_KEYS: tuple[str, ...] = (
    _WAIVED_ITEM_KEY,
    _DONE_TASK_KEY,
)


class CompletenessItem(NamedTuple):
    item_class: str
    assertion: str
    probe: str
    verified: bool


class CompletenessChecklistGate(NamedTuple):
    applies: bool
    items: tuple[CompletenessItem, ...]
    unverified_count: int
    warn_text: Optional[str]
    summary_line: str
    #: Four-way split over what `applies=False` was standing in for,
    #: matching `directives_spine_worklist.OpenSpineRowGate.verdict` /
    #: `__init__.LandedReconciliationGate.verdict`. `applies` alone
    #: collapses three distinct cases into one payload shape: the close
    #: is not chain-terminal (nothing to check), the chain-terminal
    #: handoff carries no `completeness_checklist:` field (nothing to
    #: check), and the close IS chain-terminal but the consumed handoff
    #: text never arrived (should have checked, could not). The last of
    #: those is not theoretical: `__init__._read_consumed_handoff_text`
    #: degrades an unreadable/missing/archived-away handoff to `None`,
    #: and the cadence sweeps archive handoffs routinely — a
    #: chain-terminal close over an archived-away handoff would
    #: otherwise read as "all verified / not applicable" with no trace
    #: that the gate never looked.
    #:
    #:   "indeterminate"  — chain-terminal disposition, but no consumed
    #:                      handoff text to check (unreadable, missing,
    #:                      or archived away)
    #:   "not-applicable" — not chain-terminal, or the consumed handoff
    #:                      carries no `completeness_checklist:` field
    #:   "clean"          — items parsed, all verified
    #:   "open"           — items parsed, at least one unverified
    #:
    #: Advisory only, exactly like `applies`: `verdict` adds no judgment
    #: point, no dependency edge, and no exit code.
    verdict: str = "not-applicable"


_WARN_TEMPLATE = """WARN [completeness-checklist]: {count} completeness item(s) unverified on consumed baton {handoff_basename}.
Validate or explicitly waive each before this counts as shipped.

Unverified items:
{item_lines}

Reference: docs/wiki/install-surface-completeness.md § Running-in-Claude-Code
To waive: note each item explicitly ("waiving: <item> — <rationale>") and re-run /workstream-complete,
or mark the corresponding Tasks-API task done after verifying."""


def _parse_checklist_items(raw_items: Iterable[str]) -> list[tuple[str, str, str]]:
    """Parses every raw `completeness_checklist:` line via the shared
    single-source grammar. A malformed item is skipped (surfaced to the
    caller's diagnostics is out of this gate's scope — the frontmatter
    schema validator is the load-bearing malformed-input gate, not this
    advisory WARN), not fatal to the rest of the checklist."""
    parsed: list[tuple[str, str, str]] = []
    for raw in raw_items:
        try:
            item_class, assertion, probe = parse_completeness_item(str(raw))
        except Exception:  # noqa: BLE001 - malformed items are the schema gate's concern, not this advisory's
            continue
        parsed.append((item_class, assertion, probe))
    return parsed


def _item_is_verified(assertion: str, waived_items: frozenset[str], done_task_ids: frozenset[str], item_index: int) -> bool:
    if assertion in waived_items:
        return True
    return str(item_index) in done_task_ids


def compute_completeness_checklist_gate(
    disposition: str,
    consumed_handoff_text: Optional[str],
    consumed_handoff_basename: str = "",
    decisions: Optional[Mapping[str, Any]] = None,
) -> CompletenessChecklistGate:
    """Step 2.96, all three census sub-steps in one read-only computation:

    1. Opt-in gate + locate + parse (`d-parse-completeness-checklist`):
       fires only when `disposition == "chain-terminal"` AND the consumed
       handoff's frontmatter carries a non-empty `completeness_checklist:`
       sequence — an ordinary continuation handoff (no such field) is a
       silent no-op, matching the SKILL's own three-condition gate.
    2. Cross-reference + count (`d-count-unverified-checklist-items`): an
       item is verified if its assertion text appears in the caller's
       `decisions["waived_items"]` set (an explicit inline waiver this
       session) or its ordinal position appears in
       `decisions["completed_checklist_task_ids"]` (the Tasks-API done
       set the pickup step instantiated) — absence of either is
       unverified, including the cross-conversation case where no Task
       record is visible (absence of a Task is NOT proof of completion,
       per the SKILL's own cross-conversation note).
    3. Emit the fixed WARN template (`d-emit-completeness-warn`): only
       when at least one item is unverified; `warn_text` is `None`
       otherwise.

    Returns `CompletenessChecklistGate(applies=False, ...)` with an empty
    item tuple and `warn_text=None` when the opt-in gate does not fire —
    `summary_line` is still populated with the "not applicable" Step 4
    one-liner in that case. `verdict` splits that `applies=False` shape
    four ways (see `CompletenessChecklistGate.verdict`): `not-applicable`
    when the close is not chain-terminal or the consumed handoff carries
    no `completeness_checklist:` field; `indeterminate` when the close IS
    chain-terminal but no consumed handoff text arrived to check (an
    unreadable, missing, or archived-away handoff); `clean` when every
    parsed item verified; `open` when at least one did not.
    """
    decisions = decisions or {}
    waived_items = frozenset(str(x) for x in decisions.get(_WAIVED_ITEM_KEY, ()))
    done_task_ids = frozenset(str(x) for x in decisions.get(_DONE_TASK_KEY, ()))

    if canonicalize(disposition) != PREDECESSOR_CONSUMED:
        return CompletenessChecklistGate(
            applies=False, items=(), unverified_count=0, warn_text=None,
            summary_line="Completeness checklist: all verified / not applicable",
            verdict="not-applicable",
        )

    if not consumed_handoff_text:
        return CompletenessChecklistGate(
            applies=False, items=(), unverified_count=0, warn_text=None,
            summary_line="Completeness checklist: all verified / not applicable",
            verdict="indeterminate",
        )

    parsed = parse_frontmatter(consumed_handoff_text)
    frontmatter = parsed.get("frontmatter") or {}
    raw_items = frontmatter.get("completeness_checklist")
    if not raw_items:
        return CompletenessChecklistGate(
            applies=False, items=(), unverified_count=0, warn_text=None,
            summary_line="Completeness checklist: all verified / not applicable",
            verdict="not-applicable",
        )

    checklist_items: list[CompletenessItem] = []
    for idx, (item_class, assertion, probe) in enumerate(_parse_checklist_items(raw_items)):
        verified = _item_is_verified(assertion, waived_items, done_task_ids, idx)
        checklist_items.append(CompletenessItem(item_class=item_class, assertion=assertion, probe=probe, verified=verified))

    unverified = [it for it in checklist_items if not it.verified]
    if not unverified:
        return CompletenessChecklistGate(
            applies=True, items=tuple(checklist_items), unverified_count=0, warn_text=None,
            summary_line="Completeness checklist: all verified / not applicable",
            verdict="clean",
        )

    item_lines = "\n".join(f"  - {it.item_class}: {it.assertion}" for it in unverified)
    warn_text = _WARN_TEMPLATE.format(
        count=len(unverified),
        handoff_basename=consumed_handoff_basename,
        item_lines=item_lines,
    )
    summary_line = f"Completeness checklist: {len(unverified)} items unverified — WARN emitted"
    return CompletenessChecklistGate(
        applies=True, items=tuple(checklist_items), unverified_count=len(unverified),
        warn_text=warn_text, summary_line=summary_line,
        verdict="open",
    )


# ---------------------------------------------------------------------------
# AC3 — section-scoped acceptance-criteria checkbox parser (pure text parse)
# ---------------------------------------------------------------------------

#: Reused verbatim from ops/emit/sections/handoffs.py's own module-level
#: constants (see Anti-scope: do NOT reuse `_acceptance_criteria` itself,
#: which counts every checkbox in the whole body regardless of heading and
#: does its own file read — only these two line-matcher patterns are
#: shared).
_AC_DONE_RE = re.compile(r"^[ \t\r\n\f\v]*- \[[xX]\]")
_AC_OPEN_RE = re.compile(r"^[ \t\r\n\f\v]*- \[ \]")

#: ATX heading line: 1-6 leading `#` characters, a space, then the heading
#: text. Group 1's length is the heading's nesting level (fewer `#` ==
#: higher/shallower in the document tree).
_ATX_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")

_ACCEPTANCE_CRITERIA_PREFIX = "acceptance criteria"


def _locate_acceptance_criteria_section(lines: list[str]) -> Optional[tuple[int, int]]:
    """Shared by BOTH AC parsers (checkbox and table): finds the heading whose
    case-folded text starts with `_ACCEPTANCE_CRITERIA_PREFIX` and returns
    `(start_index, section_level)` — the line index immediately after the
    heading, and the heading's ATX nesting level (used by the caller to know
    where the section ends). `None` when no such heading exists.

    Extracted so the two parsers' "same boundary rule" claim is enforced by
    one call site each, not asserted twice in prose (Review: coordinator:
    code-reviewer Finding 3 — duplicated verbatim risked silent divergence)."""
    for index, line in enumerate(lines):
        match = _ATX_HEADING_RE.match(line)
        if not match:
            continue
        if match.group(2).strip().casefold().startswith(_ACCEPTANCE_CRITERIA_PREFIX):
            return index + 1, len(match.group(1))
    return None


def _iter_acceptance_criteria_section_lines(lines: list[str], start_index: int, section_level: int) -> Iterable[str]:
    """Yields the body lines of an already-located AC section (see
    `_locate_acceptance_criteria_section`), stopping at the next ATX heading
    whose level is <= `section_level`, and skipping any line inside a
    ``` fenced code block — a fenced example table/checkbox row must not be
    counted by either parser (Review: coordinator:code-reviewer Finding 6)."""
    in_fence = False
    for line in lines[start_index:]:
        heading_match = _ATX_HEADING_RE.match(line)
        if heading_match and len(heading_match.group(1)) <= section_level:
            break
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        yield line


def parse_consumed_handoff_acceptance_criteria(text: str) -> Optional[dict[str, int]]:
    """Parses `- [ ]`/`- [x]` checkboxes under a consumed handoff's own
    `## Acceptance criteria` heading ONLY — a pure text parse over
    already-read body text, matching AC3's parser half.

    This is a SEPARATE mechanism from the shipped `wsc_resolve` STEP_2_96
    D-node (`coordinator_core/ops/ceremony/branch_resolution.py`), by
    design, not oversight: STEP_2_96 reads the SESSION'S OWN todo-mirror at
    `state/tasks/<sid>/completeness-checklist.yaml`, a file that is
    structurally absent in single-session runs (no prior session ever
    wrote it). This parser reads the PREDECESSOR handoff's own
    `## Acceptance criteria` checkboxes instead — a different signal that
    exists precisely when STEP_2_96's todo-mirror does not, because it
    lives in the predecessor's own body rather than this session's task
    state.

    Heading match: the heading text is case-folded and checked with
    `str.startswith("acceptance criteria")`, which — being a prefix test,
    not an equality test — already tolerates both a trailing colon
    (`## Acceptance criteria:`) and a trailing parenthetical
    (`## Acceptance criteria (batch)`) with no extra stripping: the
    corpus carries both spellings (87 occurrences of the former, 1 of the
    latter), and an equality-only match would silently no-op on the
    `(batch)` spelling, the multi-AC case this gate most needs.

    Section boundary: scanning starts on the line AFTER the matched
    heading and stops at the next ATX heading whose level is <= the
    matched heading's own level (same-or-higher in the document tree) — a
    deeper/nested subheading (e.g. a `###` under a `##` AC heading) does
    NOT terminate the section; checkboxes under it still count.

    Return contract (three distinct values a caller must not conflate):
        - No heading matches at all -> `None`.
        - Heading matches but no `- [ ]`/`- [x]` boxes appear before the
          section boundary -> `{"done": 0, "total": 0, "open": 0}`
          (`total == 0`).
        - Otherwise -> the real `done`/`total`/`open` counts.

    This function never opens a file and never constructs `gates.*`
    evidence — it is a pure parser over text the caller has already read
    off disk; wiring its `None`/`total=0` returns into indeterminate
    `gates.*` evidence is the caller's job (see AC3b).
    """
    lines = text.splitlines()
    located = _locate_acceptance_criteria_section(lines)
    if located is None:
        return None
    start_index, section_level = located

    done = 0
    total = 0
    for line in _iter_acceptance_criteria_section_lines(lines, start_index, section_level):
        if _AC_DONE_RE.match(line):
            done += 1
            total += 1
        elif _AC_OPEN_RE.match(line):
            total += 1

    return {"done": done, "total": total, "open": total - done}


# The status column is found by NAME, never by position. Measured over
# docs/plans/ on 2026-08-26: 265 of 313 AC tables carry a column literally
# headed "status", 7 head it "state" -- a true synonym, added to this set --
# and the remaining 68 head their third column "verified by", "discharged
# by", "oracle", "evidence", or "instrument" -- none of which is a status,
# and several of which hold a chunk id (C8, C2) or a prose instruction.
# Reading the last cell positionally misclassified every one of those, which
# is why this is a named lookup and why a table without the column is
# UNREADABLE rather than guessed at. The set stays closed at exactly these
# two names: "state" is genuinely the same concept as "status", but none of
# the five non-status headers above is, and widening this set to catch them
# would reintroduce the same misclassification the named lookup exists to
# prevent.
_AC_TABLE_STATUS_HEADERS = frozenset({"status", "state"})

_AC_TABLE_ROW_RE = re.compile(r"^\|\s*(AC[0-9][A-Za-z0-9]*)\s*\|")
# Leading tokens in a status cell recognised as OPEN or DONE. Measured over
# `docs/plans/*.md` on 2026-08-26: the leading token of every `| ACn |` row's
# last cell spans ~300 DISTINCT tokens (703 'open', 596 'met', 465 '☐', 269
# 'pending', 246 '☑', 103 '✅', 97 'done', then a long prose tail — 'the', 'a',
# 'not', ...). No allowlist closes an open set that long, and the Unicode
# checkbox glyphs are a MAJOR spelling, not an edge case — see the module's
# three-outcome contract on `parse_plan_acceptance_criteria_table`.
_AC_TABLE_OPEN_TOKENS = frozenset({"open", "partial", "pending", "blocked", "todo", "wip", "n/a", "☐"})
_AC_TABLE_DONE_TOKENS = frozenset({
    "met", "done", "closed", "complete", "completed", "shipped", "waived",
    "satisfied", "discharged", "green", "pass", "passed", "landed", "void",
    "☑", "✅",
})


def parse_plan_acceptance_criteria_table(text: str) -> Optional[dict[str, int]]:
    """Parses a PLAN's `| ACn | criterion | status |` table under its
    `## Acceptance Criteria` heading — the plan-side counterpart to
    `parse_consumed_handoff_acceptance_criteria`'s checkbox parse.

    WHY BOTH EXIST, so neither is mistaken for a duplicate of the other.
    Handoffs carry acceptance criteria as `- [ ]` checkboxes; PLANS carry them
    as a three-column table. Measured over this repo's `docs/plans/` on
    2026-08-26: 226 recent plans use the table row, 21 use checkboxes. The
    checkbox parser is therefore correct for handoffs and structurally blind to
    the format ~91% of plans actually use, which is how
    `compute_landed_reconciliation_gate` came to report `indeterminate` — "is
    status: landed but its Acceptance Criteria heading has no checkboxes" — on
    a plan whose 16 criteria were all visibly met. A gate that cannot read the
    dominant format is not a conservative gate; it is a gate that abstains
    exactly when asked to work.

    The shared checkbox parser is deliberately NOT widened to cover both: it is
    also leg A of `consumed_handoff_completeness`, where the checkbox contract
    is correct, and teaching it a second grammar would change handoff semantics
    to fix a plan-reading bug.

    Heading match and section boundary are identical to the checkbox parser's
    (both call `_locate_acceptance_criteria_section`/
    `_iter_acceptance_criteria_section_lines`), so the two agree on WHERE the
    section is — including fenced-code-block skipping (Finding 6) — and
    differ only in WHAT they count inside it.

    THREE-OUTCOME STATUS CONTRACT (not two). A corpus scan of every `| ACn |`
    row's last cell's leading token across `docs/plans/*.md` (2026-08-26)
    found ~300 distinct tokens: the two Unicode checkbox glyphs `☐`/`☑`/`✅`
    are major spellings (hundreds of occurrences each), and the tail is
    unbounded prose ('the', 'a', 'not', 'every', 'test', ...). No allowlist
    closes a 300-token open set, so a status cell is now one of three
    outcomes, not two:
        - a token in `_AC_TABLE_OPEN_TOKENS`         -> open
        - a token in `_AC_TABLE_DONE_TOKENS`         -> done
        - anything else, OR a row with fewer than 3
          cells (no identifiable status column, the
          `c1`/`c2`/`c3`/`c4` shape a differently-
          columned table produces)                   -> UNREADABLE
    An earlier version of this parser counted an unrecognised token as OPEN,
    "deliberately, the safe direction" — but the safe direction fabricates a
    number: a plan spelling its met criteria `satisfied` or `☑` would report
    a false open-AC count with full confidence, which is worse than an
    honest abstention (this gate already has one: `indeterminate`). Unreadable
    rows are surfaced to the caller instead of silently folded into either
    bucket, so the caller can refuse to conclude `not-applicable`/`applicable`
    from a party-readable table (see `compute_landed_reconciliation_gate`).

    Return contract:
        - No `## Acceptance Criteria` heading at all -> `None`.
        - Heading present, no `| ACn |` rows before the boundary ->
          `{"done": 0, "total": 0, "open": 0, "unreadable": 0}`.
        - Otherwise -> `{"done", "total", "open", "unreadable"}`, where
          `total == done + open + unreadable`.

    KNOWN GAP (Finding 5, not fixed here — no corpus evidence of the shape):
    a literal `\\|` escape or a `` `a|b` `` inline-code pipe inside the last
    cell is not defended against; either would shift what `cells[-1]` actually
    is. Left unfixed pending a real corpus example.

    Pure parser over already-read text: never opens a file, never builds
    `gates.*` evidence.
    """
    lines = text.splitlines()
    located = _locate_acceptance_criteria_section(lines)
    if located is None:
        return None
    start_index, section_level = located

    done = 0
    total = 0
    unreadable = 0
    status_column: Optional[int] = None
    for line in _iter_acceptance_criteria_section_lines(lines, start_index, section_level):
        if not line.lstrip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if not _AC_TABLE_ROW_RE.match(line):
            # Not an AC row. It may be the header that names the status column.
            # Header detection is the whole ballgame -- see the docstring: the
            # LAST cell is a status in only some plans, and is a chunk id, a
            # verification method, or an evidence pointer in the rest.
            if status_column is None and any(
                cell.casefold() in _AC_TABLE_STATUS_HEADERS for cell in cells
            ):
                status_column = next(
                    i for i, cell in enumerate(cells)
                    if cell.casefold() in _AC_TABLE_STATUS_HEADERS
                )
            continue
        total += 1
        if status_column is None or status_column >= len(cells):
            # No column is NAMED status (or this row is too short to carry it).
            # There is nothing here to read, and guessing at a positional cell
            # is what produced wrong answers before. Unreadable, by design.
            unreadable += 1
            continue
        cell = cells[status_column]
        token = cell.lstrip("*_ ").split()[0].strip("*_:.,").casefold() if cell.strip() else ""
        if token in _AC_TABLE_DONE_TOKENS:
            done += 1
        elif token in _AC_TABLE_OPEN_TOKENS:
            pass
        else:
            unreadable += 1

    return {"done": done, "total": total, "open": total - done - unreadable, "unreadable": unreadable}
