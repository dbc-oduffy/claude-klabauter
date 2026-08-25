"""
coordinator_core.workstream_complete.directives_lessons_plan — lesson-
capture (Step 1/1.2) and governing-plan reconcile (Step 2/2.4/2.4b)
directive submodule.

One of the seven submodules `workstream_complete`'s directive spine splits
into (D-4, `docs/plans/2026-07-26-workstream-complete-computed-frontage.md`
§ Key decisions D-4). **This plan is the first multi-module directive
assembler in the claude-klabauter tree** — `pickup_assemble` is one large module;
every sibling submodule in this split (`directives_completion.py`,
`directives_memo_lifecycle.py`, `directives_session_hygiene.py`,
`directives_review.py`, `directives_commit_tail.py`, `judgments.py`)
states this convention explicitly so the next converter inherits it
deliberately, not by imitation (C10 registers it centrally in
`coordinator/docs/wiki/computed-skills-conversion-checklist.md`).

Consumed only by `coordinator_core.workstream_complete.__init__`'s
assembly seam (C3) — never imported elsewhere, never run as a script.

Census rows covered (`state/plan-sidecars/2026-07-26-workstream-complete-
computed-frontage.census-steps.md`, DoE-claude): Step 1, Step 1.2 mechanical
tail, Step 2 locate + predicate, Step 2.4 governing-plan predicate + claim
+ stamp, Step 2.4b harvest sweep.

Consumes manifest (orchestrates, reimplements none):
    coordinator/bin/coordinator-lesson-add
        -> d-add-lesson-<n> (one per captured lesson)
    coordinator/bin/coordinator-queue-append
        -> d-queue-append-lesson-<n> (one per universal-scoped lesson)
    coordinator/bin/wsc-coverage-gate-runner.py claim-plan
        -> d-claim-plan-execution-lock
    coordinator/bin/archive-stamp-cli.py stamp-plan-implemented
        -> d-stamp-plan-implemented
    coordinator/bin/archive-stamp-cli.py stamp-review-verified
        -> d-attest-review-verified (C8, only when decisions["review"] is
           truthy this pass -- see `build_review_verified_directive`)
    coordinator/bin/coordinator-harvest-deferrals.py
        -> d-harvest-deferrals-<n> (one per governing plan in scope)

Negative-spec:
    - Do NOT invoke any of the above CLIs in-process. Every mutating
      action is a returned `directives[]` dict naming the CLI; this
      module only reads disk (governing-plan existence) and echoes
      caller-supplied `decisions`, exactly like the sibling `review`
      dict already does in `workstream_complete/__init__.py`'s existing
      `build_directives` for `d-write-trail`.
    - Do NOT re-derive the judgment calls this Step spans —
      "what qualifies as a lesson" (`lesson-worth-capturing`),
      "universal vs project-specific + change-kind" (`lesson-scope-
      classification`), the plan-doc content update
      (`plan-doc-content-update`), the ALLOWLIST forecast-vs-reality
      reconcile (`plan-vs-reality-reconcile`), and the enablement-vs-
      opportunistic deferral classification
      (`enablement-vs-opportunistic-deferral`) are all
      `judgment_points[]`, authored in C2f's `judgments.py`. This module
      assumes those judgments already resolved by the time `decisions`
      reaches it (see `resolve_lesson_capture_directives`'s own
      docstring for the exact shape it expects) — mirroring how the
      existing `build_directives` already treats its `review` dict as
      pre-resolved caller input, never something this module computes.
    - Step 0's `d-resolve-session-disposition` is explicitly OUT of this
      module's scope. D-7's disposition table names it as the existing,
      untouched, anti-scoped `compute_session_shape_gate` — not one of
      this plan's new directives, and it never appears in this module's
      manifest.
    - Step 2's "session context (opened docs)" search leg is not disk-
      computable from a bare `repo_root` — no filesystem fact records
      which docs the EM's own conversation has open. `resolve_governing_
      plan` therefore takes an explicit caller-supplied slug/path
      (`decisions["governing_plan_slug"]` / `["governing_plan_path"]`,
      the same key `build_directives` in `__init__.py` already consumes
      for `d-claim-plan`) as the primary signal, falling back only to the
      baton's own stamped `governing_plan` path (R5/C5) when neither is
      supplied — it does NOT attempt to guess "the" governing plan among
      an unscoped `docs/plans/` directory, and NO join/scan/fixed-file
      fallback is attempted at any price once both the explicit override
      and the stamp are absent (C10, R1): absence is surfaced as a WARN
      and the ceremony stops there, it does not search.
    - No `## Deviations` audit-table writing, no plan-body mutation of
      any kind — Step 2's ALLOWLIST reconcile and Step 2/2.4b's content
      updates are judgment-authored Edits the EM performs directly
      (`plan-body-edits-never-route-to-executor`), never a directive
      this module emits.
"""
from __future__ import annotations

from pathlib import Path
from collections.abc import Mapping
from typing import Any, NamedTuple, Optional


# ---------------------------------------------------------------------------
# gates.governing_plan — Step 2 (locate) / Step 2.4 + 2.4b (predicate)
# ---------------------------------------------------------------------------

#: `docs/plans/<slug>.md` then `tasks/plans/<slug>.md` — checked for the two
#: explicit EM-supplied overrides only (`decisions["governing_plan_slug"]`),
#: never for a guess. `docs/plans/` first because it is this repo's
#: canonical plan location (`CLAUDE.md § Key Files`). Also read directly by
#: `__init__.py`'s own additional-governing-plan-slugs sweep — same-package
#: sibling constant, do not rename without checking that call site (C10,
#: docs/plans/2026-08-21-rebuild-the-three-ceremony-assemblers.md).
_GOVERNING_PLAN_GLOB_DIRS = ("docs/plans", "tasks/plans")

#: The `decisions` keys this module's functions read — declared once so a
#: caller (`__init__.py`'s `preflight.decisions_template` composition) can
#: import and union this tuple rather than hand-copying the key list. See
#: AC3 (docs/plans/2026-07-29-workstream-complete-the-envelope-names-t.md):
#: the arg-builder and the template read this SAME constant.
_KEY_GOVERNING_PLAN_SLUG = "governing_plan_slug"
_KEY_GOVERNING_PLAN_PATH = "governing_plan_path"
_KEY_ADDITIONAL_GOVERNING_PLAN_SLUGS = "additional_governing_plan_slugs"
_KEY_LESSONS = "lessons"

FREE_VALUE_KEYS: tuple[str, ...] = (
    _KEY_GOVERNING_PLAN_SLUG,
    _KEY_GOVERNING_PLAN_PATH,
    _KEY_ADDITIONAL_GOVERNING_PLAN_SLUGS,
    _KEY_LESSONS,
)


class GoverningPlan(NamedTuple):
    slug: str
    path: Path


def _normalize_handoff_governing_plan_field(raw: Optional[Any]) -> Optional[str]:
    """A handoff's `governing_plan:` frontmatter value is producer-written,
    not schema-enforced — a known hazard in this area (fixed for comment-
    stripping in `a571e6d3`) is an FK-typed field carrying the *string*
    `'null'` (or `'none'`, or an all-whitespace scalar) rather than a real
    `None` for "no governing plan on record". Collapse all three to
    absent so callers never treat the literal text as a path."""
    if raw is None:
        return None
    value = str(raw).strip()
    if value.lower() in ("", "null", "none"):
        return None
    return value


def resolve_governing_plan(
    repo_root: Path,
    decisions: dict[str, Any],
    handoff_governing_plan_field: Optional[Any] = None,
    consumed_handoff_deliverable_id: Optional[Any] = None,
    session_id: Optional[str] = None,
) -> Optional[GoverningPlan]:
    """Step 2 — locate the governing plan doc (`d-locate-governing-plan`).
    Thin wrapper over `resolve_governing_plan_with_source` for callers that
    only need the resolved plan, not which source resolved it."""
    return resolve_governing_plan_with_source(
        repo_root, decisions, handoff_governing_plan_field, consumed_handoff_deliverable_id, session_id
    )[0]


def resolve_governing_plan_with_source(
    repo_root: Path,
    decisions: dict[str, Any],
    handoff_governing_plan_field: Optional[Any] = None,
    consumed_handoff_deliverable_id: Optional[Any] = None,
    session_id: Optional[str] = None,
) -> tuple[Optional[GoverningPlan], str]:
    """Step 2 — locate the governing plan doc (`d-locate-governing-plan`),
    also returning which source resolved it (or why none did) so a caller
    can make that absence legible rather than silently losing the four
    plan-gated directives with no trace (see `build_directives`'s
    `preflight["governing_plan_resolution"]`).

    REBUILT (C10, docs/plans/2026-08-21-rebuild-the-three-ceremony-
    assemblers.md): the former precedence ladder (leg 1, leg 2, leg 2.5,
    leg 3.5) cost 680-943ms — leg 2.5 spawned git for the session's own
    commit-trailer `deliverable_id`s then joined them against
    `docs/plans/*.md`, and leg 3.5 joined the same way off the consumed
    handoff's `deliverable_id`. Both are DELETED outright, not tuned: an
    expensive fallback that can return a confident wrong answer is R1's
    textbook violation, whatever it costs. So is the old leg 4 fixed-file
    fallback (`tasks/todo.md` / `tasks/plan.md`) — a guess with no
    explicit signal behind it.

    Precedence, highest first, no join, no scan, at any price:
      1. Caller-supplied `decisions["governing_plan_slug"]` (checked
         against `docs/plans/<slug>.md` then `tasks/plans/<slug>.md`) — an
         explicit, O(1) override, never a guess.
      2. Caller-supplied `decisions["governing_plan_path"]` (used
         verbatim, resolved against `repo_root` if relative) — same
         posture as leg 1.
      3. THE BATON'S OWN STAMPED PLAN PATH (R5, C5): `governing_plan` is
         now stamped at baton mint and carried across every successor hop
         (`baton_assemble/apply.py :: _dispatch_handoff_carry_gate` /
         `_dispatch_handoff_supersede_predecessor`), so the SAME
         frontmatter field this leg has always read — pre-extracted and
         passed in as `handoff_governing_plan_field` by `__init__.py`'s
         `_governing_plan_field_from_consumed_handoff` — is now a reliable
         write-time fact rather than the dead field a fleet-wide sweep
         found on 0 of 276 live handoffs pre-C5. This is now the SOLE
         disk-resolvable source; there is nothing below it to fall
         through to. Source strings (`"handoff_frontmatter"` /
         `"handoff_frontmatter_not_found"` / `"none"`) are UNCHANGED from
         the pre-C10 leg-3 names — `test_workstream_complete.py` (out of
         this chunk's `writes:` scope) asserts them verbatim; renaming
         them would be the same caller-first hazard as a signature change.

    Absent this leg's stamp with no explicit override either: return
    `(None, "none")` — the plan did not travel to this continuation. The
    caller surfaces that as a WARN (via `preflight[
    "governing_plan_resolution"]["source"]`, already legible there) and
    STOPS: no ladder, no join, no scan is attempted at any price, exactly
    like every leg above.

    Each leg is a terminal override once present: if the caller (or the
    baton stamp) names a plan and it does not exist on disk, this returns
    `None` immediately rather than falling through to a lower-precedence
    source — Step 2.4's own text: "Do NOT invent a plan to reconcile
    against," and an explicit-but-wrong override should not silently
    resolve to some other plan the caller didn't name.

    `consumed_handoff_deliverable_id` and `session_id` are accepted but no
    longer consulted here — the deliverable_id-join legs they fed (2.5,
    3.5) are gone. Both parameters are RETAINED, unused, purely so
    `__init__.py`'s existing five-positional-arg call site (out of this
    chunk's `writes:` scope — landing a signature change against an
    out-of-scope caller is the caller-first hazard this plan's own
    Anti-scope names) keeps working unchanged.

    MISMATCH HANDLING (C6's reverse edge — "a plan records which baton
    currently owns it"; docs/plans/2026-08-21-rebuild-the-three-ceremony-
    assemblers.md § C6/C10): NOT implemented here. C6 (the plan-side
    owner stamp this warn-and-continue check reads) is undelivered as of
    this chunk — no write site, no field name, nothing on disk to compare
    against. Wiring a comparison against a fabricated key would be silent
    dead code at best and a wrong-answer warn at worst, which is exactly
    what this rebuild exists to remove. Land once C6 specifies its own
    stamp; do not re-derive the field name here.
    """
    slug = decisions.get(_KEY_GOVERNING_PLAN_SLUG)
    if slug:
        for dirname in _GOVERNING_PLAN_GLOB_DIRS:
            candidate = repo_root / dirname / f"{slug}.md"
            if candidate.is_file():
                return GoverningPlan(slug=slug, path=candidate), "decisions_slug"
        return None, "decisions_slug_not_found"

    path_override = decisions.get(_KEY_GOVERNING_PLAN_PATH)
    if path_override:
        candidate = Path(path_override)
        if not candidate.is_absolute():
            candidate = repo_root / candidate
        if candidate.is_file():
            return GoverningPlan(slug=candidate.stem, path=candidate), "decisions_path"
        return None, "decisions_path_not_found"

    handoff_value = _normalize_handoff_governing_plan_field(handoff_governing_plan_field)
    if handoff_value:
        candidate = Path(handoff_value)
        if not candidate.is_absolute():
            candidate = repo_root / candidate
        if candidate.is_file():
            return GoverningPlan(slug=candidate.stem, path=candidate), "handoff_frontmatter"
        return None, "handoff_frontmatter_not_found"

    return None, "none"


def governing_plan_predicate(governing_plan: Optional[GoverningPlan]) -> bool:
    """Step 2.4 / Step 2.4b's shared negative-spec gate
    (`d-governing-plan-predicate`) — fires only when a governing plan was
    resolved; both plan-gated directive builders below skip entirely
    (return no directives) when this is `False`. Pure existence check —
    no CLI, no disk write, nothing to invoke."""
    return governing_plan is not None


# ---------------------------------------------------------------------------
# directives[] — Step 2.4 claim + stamp, Step 2.4b harvest
# ---------------------------------------------------------------------------


def _directive(
    id_: str,
    cli: str,
    args: list[str],
    depends_on: Any = None,
    already_satisfied: bool = False,
) -> dict[str, Any]:
    return {"id": id_, "cli": cli, "args": args, "depends_on": depends_on, "already_satisfied": already_satisfied}


def build_plan_claim_and_stamp_directives(governing_plan: Optional[GoverningPlan]) -> list[dict[str, Any]]:
    """Step 2.4's plan-claim guard + governing-plan stamp, both gated on
    `governing_plan_predicate` — zero tax on plan-less sessions.

    `d-claim-plan-execution-lock` (spec backlink:
    `docs/plans/2026-06-26-cs-claim-plan-execution-lock.md` § C4) is
    re-entrant at the CLI level (D2) — no `already_satisfied` short-circuit
    needed here. `d-stamp-plan-implemented` DEPENDS ON that claim landing.
    The `depends_on="d-claim-plan-execution-lock"` field documents that
    intent and is read by other tooling, but — like every `depends_on`
    naming a SIBLING DIRECTIVE ID rather than a judgment-point id —
    `apply_halt.py::_directive_gate_open` does NOT gate on it (it hits a
    bare `continue` for non-judgment-point ids); by itself it is inert.
    The actual enforcement is the trailing `{d-claim-plan-execution-lock.
    landed}` arg token below, resolved by `apply.py::_resolve_arg_tokens`:
    it substitutes the empty string when the claim landed this pass, and
    fails `d-stamp-plan-implemented` loud into `report["failed"]` when the
    claim never landed (denied, blocked, or failed) — the same idiom
    `directives_commit_tail.py`'s `build_release_plan_claim_directive` /
    `build_emit_cadence_directive` already use for their own
    `{d-run-wsc-tail.landed}` tokens. `archive-stamp-cli`'s
    `stamp-plan-implemented` verb reads only `rest[0]` (the plan path) and
    silently ignores any trailing argv, so appending the token as an extra
    positional is safe — it never reaches `cs_stamp_plan_implemented` as a
    real argument, it only participates in `_resolve_arg_tokens`'
    pre-dispatch substitution/failure check.

    This closes a defence-in-depth gap surfaced by a live incident — a
    session-shape misdetection caused `plan-status-transition` to stamp a
    LIVE PEER's `approved` plan `implemented` and commit it (cross-repo
    memo `2026-08-10-example-retrieval-repo-em-wsc-misdetection-wrote-to-a-live-peers-
    plan.md`) — because the stamp directive fired independent of whether
    the claim actually succeeded. `d-claim-plan-execution-lock` itself
    still carries `depends_on=None` — nothing gates the claim attempt
    itself; only the stamp that follows it is now conditioned on it
    landing. The predicate gate (no governing plan -> the list is empty)
    is unchanged and still expressed structurally, matching how the
    existing `d-claim-plan` in `__init__.py`'s `build_directives` already
    gates on `decisions.get("governing_plan_slug")` via a plain `if`, not
    a `depends_on` edge.
    """
    if not governing_plan_predicate(governing_plan):
        return []
    assert governing_plan is not None  # narrows for the type checker; predicate already proved it
    slug = governing_plan.slug
    plan_rel = f"docs/plans/{slug}.md"
    return [
        _directive("d-claim-plan-execution-lock", "wsc-coverage-gate-runner", ["claim-plan", slug]),
        _directive(
            "d-stamp-plan-implemented",
            "archive-stamp-cli",
            ["stamp-plan-implemented", plan_rel, "{d-claim-plan-execution-lock.landed}"],
            depends_on="d-claim-plan-execution-lock",
        ),
    ]


def build_review_verified_directive(
    governing_plan: Optional[GoverningPlan], decisions: dict[str, Any]
) -> list[dict[str, Any]]:
    """Step 2.4's sibling attest write (`d-attest-review-verified`, C8,
    AC17 -- docs/plans/2026-08-20-the-rungs-get-writers.md). Emitted
    alongside `d-stamp-plan-implemented` off the SAME `governing_plan_
    predicate` gate, with the SAME `depends_on="d-claim-plan-execution-
    lock"` edge (inert by itself, matching `build_plan_claim_and_stamp_
    directives`'s own -- see that function's docstring for why the real
    ordering enforcement lives in the trailing arg token, not this field)
    and the SAME two `jp-open-spine-rows-block-stamp` / `jp-landed-
    reconciliation-block-stamp` block conditions `__init__.py`'s assembly
    layer wires onto `d-stamp-plan-implemented` by directive id -- this
    module only emits the directive itself; the block-condition `depends_
    on` edges are appended by that caller, matching every other judgment
    point in this family's own division of labour (module computes/
    emits, `__init__.py` decides which facts gate a directive).

    Fires only when `decisions["review"]` is truthy -- the session's work
    went through a code review this pass. Mirrors `__init__.py`'s
    `build_write_trail_directives`'s own non-empty-`review` gate, but
    does NOT re-derive that function's five-required-fields shape check:
    a review that is present but shape-incomplete still attests here
    (`d-write-trail` itself may simply not fire for it), because this
    attest is a courtesy record of "a review happened", never a
    completeness judgment of its own.

    Writes `coordinator_core.ops.plan_status_transition.
    _stamp_review_verified`'s three attest fields (C6a) via the SAME
    already-admitted `archive-stamp-cli` CLI `d-stamp-plan-implemented`
    uses, plan path positional first -- identical argv shape to that
    sibling directive.

    `--findings` carries the REVIEW-TRAIL PATH (C6a's own docstring: "a
    count drifts from the record already on disk... and cannot
    distinguish a P1 from a nitpick"), which is only known once
    `d-write-trail` actually lands THIS pass -- so, exactly like
    `d-stamp-plan-implemented`'s own `{d-claim-plan-execution-lock.
    landed}` token, this threads a `{d-write-trail.entry_path}` inter-
    directive token (`apply.py::_resolve_arg_tokens`) rather than
    guessing the path at build time.
    """
    if not governing_plan_predicate(governing_plan):
        return []
    if not decisions.get("review"):
        return []
    assert governing_plan is not None  # narrows for the type checker; predicate already proved it
    plan_rel = f"docs/plans/{governing_plan.slug}.md"
    return [
        _directive(
            "d-attest-review-verified",
            "archive-stamp-cli",
            ["stamp-review-verified", plan_rel, "--findings", "{d-write-trail.entry_path}"],
            depends_on="d-claim-plan-execution-lock",
        ),
    ]


def build_deferral_harvest_directives(governing_plans: list[GoverningPlan]) -> list[dict[str, Any]]:
    """Step 2.4b's belt-and-suspenders deferral harvest sweep, run once
    per governing plan in scope (ordinarily one; a chain-terminal session
    may carry more than one predecessor plan — spec backlink:
    `docs/plans/2026-07-09-plan-full-coverage-and-deferred-harvest.md`
    § C6). Dedup is `coordinator-harvest-deferrals`' own job (idempotent
    on `harvest-key: <plan_id>:<row id>`), never re-derived here.

    Non-zero exit REACHES THE CALLER — it is not swallowed. "Advisory"
    here means the sweep does not abort the ceremony, NOT that its failure
    is cosmetic. Since 2026-07-29 the harvest exits 1 when it skips a
    `pm_approved` row it cannot route, and that code propagates: the
    apply-half's dispatch loop (`workstream_complete.apply._execute_
    directives`) puts any non-zero directive in `report["failed"]` and
    skips `landed`, so `apply()` returns `DIRECTIVE_FAILED` — or
    `PARTIAL_MUTATION` when other directives did land. Both are non-zero
    ceremony exits. Regression cover:
    `test_apply.py::test_execute_directives_nonzero_exit_is_failed_not_landed`.

    What "advisory" DOES mean: the halt is per-directive, so a failed
    harvest does not stop sibling directives from dispatching. That is the
    whole of it.

    This paragraph is spelled out because the prior wording ("surfaced in
    the Step 4 one-liner, never a ceremony halt") read as "rendered and
    moved on" — doe-claude-em could not establish from it whether the new
    fail-loud survived WSC at all, which is the one path that matters for
    a row that flipped to deferred after Phase 1.6 already harvested.
    """
    directives: list[dict[str, Any]] = []
    for idx, plan in enumerate(governing_plans):
        directives.append(
            _directive(
                f"d-harvest-deferrals-{idx + 1}",
                "coordinator-harvest-deferrals",
                ["--plan", f"docs/plans/{plan.slug}.md"],
            )
        )
    return directives


def build_governing_plan_directives(
    repo_root: Path,
    decisions: dict[str, Any],
    handoff_governing_plan_field: Optional[Any] = None,
    consumed_handoff_deliverable_id: Optional[Any] = None,
    session_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Composes Step 2.4's claim+stamp pair with Step 2.4b's harvest sweep
    off one resolved governing-plan gate — the single entry point C3
    imports for this half of the module. A chain-terminal session with
    more than one predecessor plan supplies
    `decisions["additional_governing_plan_slugs"]` (each checked the same
    way `resolve_governing_plan` checks the primary slug) to extend the
    harvest sweep beyond the single claim+stamp target — Step 2.4's
    claim/stamp guard is deliberately single-plan (`docs/plans/2026-06-26-
    cs-claim-plan-execution-lock.md` § C4 names one lock per session), so
    only Step 2.4b's harvest fans out. `handoff_governing_plan_field`,
    `consumed_handoff_deliverable_id`, and `session_id` forward unchanged
    to `resolve_governing_plan` — see that function's docstring for the
    precedence they slot into (C6, AC4: `session_id` feeds the new leg-2.5
    commit-trailer join, above legs 3 and 3.5).
    """
    governing_plan = resolve_governing_plan(
        repo_root, decisions, handoff_governing_plan_field, consumed_handoff_deliverable_id, session_id
    )
    directives = build_plan_claim_and_stamp_directives(governing_plan)
    directives += build_review_verified_directive(governing_plan, decisions)

    harvest_targets: list[GoverningPlan] = [governing_plan] if governing_plan else []
    for slug in decisions.get(_KEY_ADDITIONAL_GOVERNING_PLAN_SLUGS, []) or []:
        for dirname in _GOVERNING_PLAN_GLOB_DIRS:
            candidate = repo_root / dirname / f"{slug}.md"
            if candidate.is_file():
                harvest_targets.append(GoverningPlan(slug=slug, path=candidate))
                break
    directives += build_deferral_harvest_directives(harvest_targets)
    return directives


# ---------------------------------------------------------------------------
# directives[] — Step 1 lesson capture, Step 1.2 mechanical queue-append tail
# ---------------------------------------------------------------------------

# The per-lesson id suffix exists in exactly ONE place (these two helpers) and
# is consumed by BOTH the directive builder below and the `lesson-worth-
# capturing` judgment point's `resolves` list (`judgments.py`, via
# `lesson_capture_resolves_ids`). Formatting it independently in the two places
# is the defect this centralization exists to prevent: `apply`'s gate matches a
# `resolves` entry against a directive id EXACTLY (never by prefix), so a
# `resolves` naming the unsuffixed base silently never opens the gate, and the
# captured lesson is never written while `apply` still reports success.


#: Optional `decisions["lessons"][n]` keys → the `coordinator-lesson-add` flag
#: that carries them, in the order they are appended to the directive's argv.
#: Every optional flag the CLI accepts appears here: a facet the EM composes but
#: the assembler has no flag for is a facet silently dropped on the way to disk,
#: which is what forces an author to bypass the directive and hand-run the CLI.
#: Keys every `decisions["lessons"]` entry must carry before a lesson-add
#: directive can be composed. Validated up front rather than indexed blind:
#: a missing key previously surfaced as a bare `KeyError` traceback out of
#: `build_lesson_capture_directives`, which reads as an engine crash mid-
#: ceremony rather than as the malformed-input error it actually is, and
#: gives the author no clue which entry or which key was at fault.
#: Negative-spec: do NOT default a missing value here. These are
#: author-composed prose; substituting a placeholder would put an
#: unauthored lesson on disk, which is worse than refusing.
_LESSON_REQUIRED_KEYS: tuple[str, ...] = ("title", "body", "scope")

_LESSON_OPTIONAL_FLAGS: tuple[tuple[str, str], ...] = (
    ("trigger", "--trigger"),
    ("why", "--why"),
    ("how_to_apply", "--how-to-apply"),
    ("target_wiki", "--target-wiki"),
    ("proposed_target", "--proposed-target"),
    ("evidence", "--evidence"),
)


def lesson_add_directive_id(idx: int) -> str:
    """`d-add-lesson-<n>` for the 0-based lesson index `idx`."""
    return f"d-add-lesson-{idx + 1}"


def lesson_queue_directive_id(idx: int) -> str:
    """`d-queue-append-lesson-<n>` for the 0-based lesson index `idx`."""
    return f"d-queue-append-lesson-{idx + 1}"


def lesson_capture_resolves_ids(decisions: dict[str, Any]) -> list[str]:
    """Every directive id `build_lesson_capture_directives` will emit for
    the same `decisions` — the exact list the `lesson-worth-capturing`
    judgment point's "capture" disposition must name in `resolves` for its
    directives to fire.

    Derived by asking the directive builder itself rather than re-deriving
    the id set from `decisions`: the queue-append directive is CONDITIONAL
    (universal scope + five populated queue fields), so an index-range
    reconstruction would name `d-queue-append-lesson-<n>` ids that were
    never built — phantom `resolves` entries, which the fleet-wide sweep
    guard (`ceremony_common.test_phantom_resolves_id_sweep`) correctly
    rejects. Delegating makes the two exact by construction."""
    return [d["id"] for d in build_lesson_capture_directives(decisions)]


def build_lesson_capture_directives(decisions: dict[str, Any]) -> list[dict[str, Any]]:
    """Step 1 / Step 1.2's mechanical directive tail.

    Expects `decisions["lessons"]`: a list of dicts, one per lesson this
    session has already decided (via the `lesson-worth-capturing`
    judgment, `judgments.py`) is worth capturing — a lesson the judgment
    resolved as "not worth it" simply never appears in this list, which is
    how that judgment's gate is expressed here (no runtime `depends_on`
    edge needed, matching this module's plan-claim/stamp pair above). Each
    entry:
        {"title": str, "body": str, "scope": "universal"|"project"|"wiki-only"}
    plus, optionally, any of the structured facets `coordinator-lesson-add`
    accepts — `trigger`, `why`, `how_to_apply`, `target_wiki`,
    `proposed_target`, `evidence`. Each is forwarded to its matching flag
    when populated and omitted when absent or empty
    (`_LESSON_OPTIONAL_FLAGS`). The facets are author-composed prose, never
    derived here: a lesson that reaches disk carrying only title/body/scope
    loses the why-it-matters and how-to-apply halves that make it actionable
    four weeks later, and an author who notices mid-ceremony works around it
    by abandoning the directive and hand-running the CLI.

    Additionally, only for entries the `lesson-scope-classification` judgment
    (`judgments.py`) resolved `universal`, the additional queue-append
    fields:
        {"queue_title": str, "queue_body": str, "surface": str,
         "proposed_action": str,
         "change_kind": "skill-edit"|"hook-edit"|"wiki-append"|"wiki-new"|
                         "agent-prompt-edit"}

    `d-queue-append-lesson-<n>` is only ever built alongside its matching
    `d-add-lesson-<n>` and `depends_on`s it directly (the queue entry's
    `--surface` cites the lesson YAML the add-lesson call writes) — this
    is a concrete disk dependency, not a re-statement of the
    classification judgment's own gate.
    """
    directives: list[dict[str, Any]] = []
    for idx, lesson in enumerate(decisions.get(_KEY_LESSONS, []) or []):
        add_id = lesson_add_directive_id(idx)
        if not isinstance(lesson, Mapping):
            # A bare string is the natural first guess at this shape, and it
            # used to reach `.get` and die on AttributeError -- a traceback
            # where the block below is deliberately designed to name the
            # offending entry and its missing keys. Same refusal, same
            # nothing-written guarantee, just legible.
            raise ValueError(
                f"decisions[{_KEY_LESSONS!r}][{idx}] is a "
                f"{type(lesson).__name__}, not a mapping (required keys: "
                f"{list(_LESSON_REQUIRED_KEYS)!r}). Supply them and re-run "
                "apply -- this is a malformed decisions map, not a ceremony "
                "failure, and nothing has been written."
            )
        missing = [k for k in _LESSON_REQUIRED_KEYS if not str(lesson.get(k) or "").strip()]
        if missing:
            raise ValueError(
                f"decisions[{_KEY_LESSONS!r}][{idx}] is missing required "
                f"key(s) {missing!r} (required: {list(_LESSON_REQUIRED_KEYS)!r}). "
                "Supply them and re-run apply — this is a malformed decisions "
                "map, not a ceremony failure, and nothing has been written."
            )
        add_args = [
            "--title", str(lesson["title"]),
            "--body", str(lesson["body"]),
            "--scope", str(lesson["scope"]),
        ]
        for key, flag in _LESSON_OPTIONAL_FLAGS:
            value = lesson.get(key)
            if value in (None, ""):
                continue
            add_args += [flag, str(value)]
        directives.append(_directive(add_id, "coordinator-lesson-add", add_args))
        if lesson.get("scope") == "universal" and all(
            lesson.get(k) not in (None, "")
            for k in ("queue_title", "queue_body", "surface", "proposed_action", "change_kind")
        ):
            directives.append(
                _directive(
                    lesson_queue_directive_id(idx),
                    "coordinator-queue-append",
                    [
                        "--schema", "improvement-queue",
                        "--queue-scope", "central",
                        "--title", str(lesson["queue_title"]),
                        "--body", str(lesson["queue_body"]),
                        "--surface", str(lesson["surface"]),
                        "--proposed-action", str(lesson["proposed_action"]),
                        "--change-kind", str(lesson["change_kind"]),
                        "--status", "open",
                    ],
                    depends_on=add_id,
                )
            )
    return directives
