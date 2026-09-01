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

import hashlib
import os
import tempfile
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
    """A resolved governing plan: its slug, the file that was actually found,
    and that file's repo-relative form.

    Negative-spec:
        - Do NOT rebuild a plan path from `slug` at a consumer. `slug` names
          the plan; it does not locate it. A plan resolved from a consumed
          handoff's `governing_plan:` frontmatter, or from an explicit
          `decisions` path, legitimately lives outside `_GOVERNING_PLAN_GLOB_
          DIRS` -- `archive/specs/<month>/` for a distilled plan is the common
          case -- so `f"docs/plans/{slug}.md"` names a file the resolver never
          returned and which need not exist. `rel` is the field a directive's
          argv takes; it is populated at every construction site precisely so
          the wrong path is not expressible downstream.
    """

    slug: str
    path: Path
    rel: str


def _rel_to_repo(candidate: Path, repo_root: Path) -> str:
    """`candidate`'s repo-relative POSIX form, or its absolute POSIX form when
    it lies outside `repo_root` (an explicitly-supplied absolute path may).
    Never raises -- a path the CLI can open beats a path-shaped guess."""
    try:
        return candidate.relative_to(repo_root).as_posix()
    except ValueError:
        return candidate.as_posix()


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
                return GoverningPlan(slug=slug, path=candidate, rel=_rel_to_repo(candidate, repo_root)), "decisions_slug"
        return None, "decisions_slug_not_found"

    path_override = decisions.get(_KEY_GOVERNING_PLAN_PATH)
    if path_override:
        candidate = Path(path_override)
        if not candidate.is_absolute():
            candidate = repo_root / candidate
        if candidate.is_file():
            return GoverningPlan(slug=candidate.stem, path=candidate, rel=_rel_to_repo(candidate, repo_root)), "decisions_path"
        return None, "decisions_path_not_found"

    handoff_value = _normalize_handoff_governing_plan_field(handoff_governing_plan_field)
    if handoff_value:
        candidate = Path(handoff_value)
        if not candidate.is_absolute():
            candidate = repo_root / candidate
        if candidate.is_file():
            return GoverningPlan(slug=candidate.stem, path=candidate, rel=_rel_to_repo(candidate, repo_root)), "handoff_frontmatter"
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
    `build_emit_cadence_directive` used for their own
    `{d-run-wsc-tail.landed}` tokens (both removed in the ceremony.wsc_tail
    kill, 2026-08-23). `archive-stamp-cli`'s
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
    plan_rel = governing_plan.rel
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
    plan_rel = governing_plan.rel
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
                ["--plan", plan.rel],
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
                harvest_targets.append(GoverningPlan(slug=slug, path=candidate, rel=_rel_to_repo(candidate, repo_root)))
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


#: Keys every `decisions["lessons"]` entry must carry before a lesson-add
#: directive can be composed. Validated up front rather than indexed blind:
#: a missing key previously surfaced as a bare `KeyError` traceback out of
#: `build_lesson_capture_directives`, which reads as an engine crash mid-
#: ceremony rather than as the malformed-input error it actually is, and
#: gives the author no clue which entry or which key was at fault.
#: Negative-spec: do NOT default a missing value here. These are
#: author-composed prose; substituting a placeholder would put an
#: unauthored lesson on disk, which is worse than refusing.
_LESSON_REQUIRED_KEYS: tuple[str, ...] = ("title", "scope")

#: The body transport pair. Forwarded by `_lesson_body_args`, which picks
#: ONE of them per entry, never the generic loop below — an entry carrying
#: both flags is what `coordinator_core.argv_fidelity.resolve_body` refuses
#: outright. Listed here so the pair stays declared in one place alongside
#: the facets, and so the drift guard
#: (`test_assembler_covers_every_optional_flag_the_lesson_cli_accepts`)
#: reads them as forwarded: they are optional at the CLI's argparse layer
#: (exactly-one-of is enforced after parse), so the guard counts them.
_LESSON_BODY_FLAGS: tuple[tuple[str, str], ...] = (
    ("body", "--body"),
    ("body_file", "--body-file"),
)

_LESSON_BODY_KEYS: frozenset[str] = frozenset(key for key, _flag in _LESSON_BODY_FLAGS)


#: The title transport pair. `coordinator-lesson-add` requires EXACTLY ONE
#: of `--title`/`--title-file`, so these are not facets: running them through
#: the generic loop below would forward both halves of a mutually-exclusive
#: pair over a REQUIRED field. Forwarded by `_lesson_title_args`.
_LESSON_TITLE_FLAGS: tuple[tuple[str, str], ...] = (
    ("title", "--title"),
    ("title_file", "--title-file"),
)

_LESSON_TITLE_KEYS: frozenset[str] = frozenset(key for key, _flag in _LESSON_TITLE_FLAGS)

#: The `why` transport pair. `--why-file` is a second body-shaped pair over an
#: OPTIONAL facet, not a seventh facet: the CLI resolves the two through
#: `resolve_optional_prose`, which refuses both at once.
_LESSON_WHY_FLAGS: tuple[tuple[str, str], ...] = (
    ("why", "--why"),
    ("why_file", "--why-file"),
)

_LESSON_WHY_KEYS: frozenset[str] = frozenset(key for key, _flag in _LESSON_WHY_FLAGS)

#: Optional `decisions["lessons"][n]` keys → the `coordinator-lesson-add` flag
#: that carries them, in the order they are appended to the directive's argv.
#: Every optional flag the CLI accepts appears here: a facet the EM composes but
#: the assembler has no flag for is a facet silently dropped on the way to disk,
#: which is what forces an author to bypass the directive and hand-run the CLI.
#: The body pair leads (its two keys are handled by `_lesson_body_args`, and
#: the generic loop skips them via `_LESSON_BODY_KEYS`); the facets follow.
_LESSON_OPTIONAL_FLAGS: tuple[tuple[str, str], ...] = _LESSON_BODY_FLAGS + _LESSON_TITLE_FLAGS + _LESSON_WHY_FLAGS + (
    ("trigger", "--trigger"),
    ("how_to_apply", "--how-to-apply"),
    ("target_wiki", "--target-wiki"),
    ("proposed_target", "--proposed-target"),
    ("evidence", "--evidence"),
)

#: Where a multi-paragraph `body` is materialized so it can travel as
#: `--body-file`. Content-addressed and therefore idempotent: the same body
#: re-composed on a re-run resolves to the same path with the same bytes,
#: so a repeated `apply` pass adds no file and mutates none.
_LESSON_BODY_SPOOL_RELDIR = "state/ceremony/wsc-lesson-body"


#: `preflight.decisions_template`'s discoverable stand-in for the bare
#: `None` a free-value key gets by default (`__init__.py::build_decisions_
#: template`). `_LESSON_REQUIRED_KEYS`/`_LESSON_BODY_KEYS`/
#: `_LESSON_OPTIONAL_FLAGS` plus the queue-append facet keys
#: `_iter_capturable_lessons` reads for `wants_queue` are ALL represented
#: here, keyed to `None`, so a caller can discover every key this module's
#: builders read by looking at the template's OWN output rather than
#: reverse-engineering this module's source — the exact gap
#: `build_lesson_capture_directives`'s `ValueError`s used to surface only a
#: round trip later, after a malformed `--decisions` was already composed
#: and rejected. A single-entry list, not an empty one: an empty `[]`
#: reads as "the shape is a list of lessons" with no clue what a lesson
#: dict looks like, which is the same discoverability gap in a different
#: costume. Never resolved to anything else at runtime — this is a static
#: hint, not a computed fact, so it carries no `resolved_free_values`
#: entry the way `governing_plan_slug` does.
LESSONS_TEMPLATE_DEFAULT: list[dict[str, Any]] = [
    {
        "title": None,
        "body": None,
        "body_file": None,
        "scope": None,
        "trigger": None,
        "why": None,
        "how_to_apply": None,
        "target_wiki": None,
        "proposed_target": None,
        "evidence": None,
        "queue_title": None,
        "queue_body": None,
        "surface": None,
        "proposed_action": None,
        "change_kind": None,
    }
]

#: Union source for `build_decisions_template`'s static-shape overrides
#: (mirrors `FREE_VALUE_KEYS`'s own per-submodule union pattern) — a free-
#: value key that wants a discoverable non-`None` template default rather
#: than the generic `None` every other free-value key gets.
FREE_VALUE_KEY_STATIC_DEFAULTS: dict[str, Any] = {
    _KEY_LESSONS: LESSONS_TEMPLATE_DEFAULT,
}


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

    Derived from the SAME validated walk the directive builder uses
    (`_iter_capturable_lessons`) rather than re-deriving the id set from
    `decisions`: the queue-append directive is CONDITIONAL (universal scope
    + five populated queue fields), so an index-range reconstruction would
    name `d-queue-append-lesson-<n>` ids that were never built — phantom
    `resolves` entries, which the fleet-wide sweep guard
    (`ceremony_common.test_phantom_resolves_id_sweep`) correctly rejects.
    Sharing the walk makes the two exact by construction.

    Walks, rather than calls the builder: composing argv can MATERIALIZE a
    multi-paragraph body to disk (`_lesson_body_args`), and an id list has
    no business writing a spool file — nor does it hold the `repo_root`
    that write needs."""
    ids: list[str] = []
    for idx, _lesson, wants_queue in _iter_capturable_lessons(decisions):
        ids.append(lesson_add_directive_id(idx))
        if wants_queue:
            ids.append(lesson_queue_directive_id(idx))
    return ids


def _lesson_body_spool_path(repo_root: Path, body: str) -> Path:
    """Content-addressed spool path for `body` under `repo_root`."""
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:24]
    return Path(repo_root) / _LESSON_BODY_SPOOL_RELDIR / f"{digest}.md"


def _spool_body_to_file(repo_root: Path, body: str) -> str:
    """Materialize `body` at its content-addressed spool path and return the
    path as a string. Shared by `_lesson_body_args` (the `--body`/`--body-file`
    leg) and the queue-append `--body` leg below — same idempotent
    write-if-absent behaviour both need: a repeated `apply` pass composing the
    same body resolves to the same path with the same bytes, so it adds no
    file and mutates none on a re-run."""
    path = _lesson_body_spool_path(repo_root, body)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.stem}.tmp.", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)
        os.replace(tmp_str, str(path))
    except BaseException:
        try:
            os.unlink(tmp_str)
        except OSError:
            pass
        raise
    return str(path)


def _lesson_title_args(idx: int, lesson: Mapping[str, Any]) -> list[str]:
    """The two-element title transport for one lesson's argv — `--title` or
    `--title-file`, never both, mirroring `_lesson_body_args`.

    An explicit `title_file` wins and is forwarded verbatim. A title is one
    line by contract, so a newline-bearing `title` refuses here with the
    remedy named rather than composing argv `coordinator-lesson-add` is
    certain to reject after the launcher has already truncated it."""
    title_file = str(lesson.get("title_file") or "").strip()
    if title_file:
        return ["--title-file", title_file]
    title = str(lesson["title"])
    if "\n" in title:
        raise ValueError(
            f"decisions[{_KEY_LESSONS!r}][{idx}]['title'] carries a newline; a"
            " title is one line. Supply 'title_file' instead."
        )
    return ["--title", title]


def _lesson_why_args(lesson: Mapping[str, Any]) -> list[str]:
    """`--why` or `--why-file`, never both — the CLI's `resolve_optional_
    prose` refuses the pair. Absent on both keys, the facet is omitted."""
    why_file = str(lesson.get("why_file") or "").strip()
    if why_file:
        return ["--why-file", why_file]
    why = str(lesson.get("why") or "")
    return ["--why", why] if why.strip() else []


def _lesson_body_args(idx: int, lesson: Mapping[str, Any], repo_root: Optional[Path]) -> list[str]:
    """The two-element body transport for one lesson's `coordinator-lesson-add`
    argv — `--body` or `--body-file`, never both.

    An explicit `body_file` wins and is forwarded verbatim (the CLI resolves
    it, and its `-` stdin sentinel is not this assembler's to interpret).
    Otherwise a SINGLE-LINE body travels as `--body`; a multi-paragraph one
    is spooled under `_LESSON_BODY_SPOOL_RELDIR` and forwarded as
    `--body-file`, because `coordinator-lesson-add` refuses a newline-bearing
    `--body` outright (`coordinator_core.argv_fidelity.refuse_newline_argv`
    — cmd.exe truncates an argv at its first LF). Without the spool the
    author's only route for a real lesson body was to abandon the directive
    and hand-run the CLI.

    `repo_root` is required to spool. `None` is legal only while every body
    in play is single-line — a multi-paragraph body then refuses here rather
    than composing argv the CLI is certain to reject mid-apply."""
    body_file = str(lesson.get("body_file") or "").strip()
    if body_file:
        return ["--body-file", body_file]
    body = str(lesson["body"])
    if "\n" not in body:
        return ["--body", body]
    if repo_root is None:
        raise ValueError(
            f"decisions[{_KEY_LESSONS!r}][{idx}]['body'] is multi-paragraph and no "
            "repo_root was supplied to spool it; pass repo_root, or supply "
            "'body_file' instead."
        )
    return ["--body-file", _spool_body_to_file(repo_root, body)]


def _queue_body_args(idx: int, lesson: Mapping[str, Any], repo_root: Optional[Path]) -> list[str]:
    """The two-element body transport for one lesson's `coordinator-queue-
    append` argv — `--body` for a single-line `queue_body`, `--body-file`
    for a multi-paragraph one, mirroring `_lesson_body_args` exactly.

    `coordinator-queue-append` does not refuse a newline-bearing `--body`
    the way `coordinator-lesson-add` does (it silently `str.replace`s a
    literal `\\n` escape, which is not the same thing as a REAL embedded
    newline surviving a `.cmd` launcher's argv — see that CLI's own
    `--body-file` docstring: "cmd.exe truncates its argv at the first LF").
    A real multi-paragraph `queue_body` composed as `--body` therefore loses
    every line after the first the same way a lesson body did before this
    fix, just without the CLI raising to say so — so this leg spools
    exactly like the lesson-add leg rather than trusting the silent
    single-line success.

    `repo_root` is required to spool. `None` is legal only while
    `queue_body` is single-line — a multi-paragraph one then refuses here
    rather than composing argv the CLI would silently truncate."""
    body = str(lesson["queue_body"])
    if "\n" not in body:
        return ["--body", body]
    if repo_root is None:
        raise ValueError(
            f"decisions[{_KEY_LESSONS!r}][{idx}]['queue_body'] is multi-paragraph and "
            "no repo_root was supplied to spool it; pass repo_root."
        )
    return ["--body-file", _spool_body_to_file(repo_root, body)]


#: The scope values `coordinator-lesson-add` will accept. Authority is
#: DOWNSTREAM of this module -- lesson-add forwards `--scope` verbatim to the
#: record-writing CLI, whose enum is the real gate, and the same three values
#: are hard-coded at `coordinator/bin/coordinator-queue-append.py`'s own
#: `_VALID_LESSON_SCOPES`. Duplicated here rather than imported.
#:
#: Review: overengineering-reviewer (finding #4) — the prior comment here
#: claimed this could not be imported because a bin script is "not an
#: importable module". That claim is false and this session's own diff
#: falsifies it twice over: `coordinator/bin/tests/test_cc_invoke_
#: indeterminate.py` and `test_cross_repo_memo_indeterminate_reconcile.py`
#: both import across this exact boundary (one via `sys.path.insert` onto
#: `bin/lib`, the other via `SourceFileLoader` on the hyphenated `.py`), and
#: `ceremony_common.cli_dispatch.load_cli_module` (this package's own
#: `apply.py` sibling) does the same at PRODUCTION runtime to invoke bin
#: CLIs from `workstream_complete`/`workday_complete`/`workweek_complete`.
#: The honest reason for duplicating anyway: `load_cli_module` is scoped to
#: directive DISPATCH time and its own docstring disclaims isolating a
#: loaded script's top-level side effects/argv/env -- reaching for it here,
#: at directive-BUILD time (this module runs well before any directive
#: executes), to read one three-value constant would import
#: `coordinator-queue-append.py`'s full top-level (argparse setup and all)
#: on a path that has nothing to do with dispatching it, for a cost this
#: three-value enum does not justify. If a fourth value is ever added, this
#: set is one of three places that must move together; the alternative --
#: discovering the mismatch at dispatch -- costs a PARTIAL_MUTATION after
#: the commit tail has landed.
_VALID_LESSON_SCOPES = frozenset({"universal", "project", "wiki-only"})


def _iter_capturable_lessons(
    decisions: dict[str, Any],
) -> "list[tuple[int, Mapping[str, Any], bool]]":
    """Every `decisions["lessons"]` entry, validated, as
    `(idx, lesson, wants_queue_append)`.

    The one place an entry's shape is checked, so `lesson_capture_resolves_ids`
    and `build_lesson_capture_directives` cannot disagree about which entries
    are capturable. Validated up front rather than indexed blind: a missing
    key previously surfaced as a bare `KeyError` traceback, which reads as an
    engine crash mid-ceremony rather than the malformed-input error it is."""
    out: list[tuple[int, Mapping[str, Any], bool]] = []
    for idx, lesson in enumerate(decisions.get(_KEY_LESSONS, []) or []):
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
        # `title` is satisfied by either half of its transport pair: the CLI
        # requires exactly one of `--title`/`--title-file`, so an entry
        # carrying only `title_file` is complete, not missing a key.
        missing = [
            k
            for k in _LESSON_REQUIRED_KEYS
            if not str(lesson.get(k) or '').strip()
            and not (k == 'title' and str(lesson.get('title_file') or '').strip())
        ]
        if missing:
            raise ValueError(
                f"decisions[{_KEY_LESSONS!r}][{idx}] is missing required "
                f"key(s) {missing!r} (required: {list(_LESSON_REQUIRED_KEYS)!r}). "
                "Supply them and re-run apply — this is a malformed decisions "
                "map, not a ceremony failure, and nothing has been written."
            )
        scope = str(lesson.get("scope") or "").strip()
        if scope not in _VALID_LESSON_SCOPES:
            # VALIDATED HERE, BEFORE THE COMMIT TAIL, because the cost of
            # validating it downstream was measured: example-market-data-repo-em
            # supplied `scope: "local"`, the assembler forwarded it unchecked,
            # `coordinator-lesson-add` rejected it, BOTH lesson directives
            # returned exit 1 -- and the commit tail had ALREADY SUCCEEDED, so
            # apply returned exit 4 (PARTIAL_MUTATION) on a ceremony that
            # looked done while both lessons were silently lost. They found it
            # by grepping state/lessons/ afterwards (cross-repo/archive/
            # 2026-08-11-example-market-data-repo-em-workstream-complete-engine-
            # defects.md, defect 2). A caller-supplied value the engine can
            # check must not be checked by a subprocess that runs after the
            # irreversible half.
            raise ValueError(
                f"decisions[{_KEY_LESSONS!r}][{idx}] has scope {scope!r}, which "
                f"is not one of {sorted(_VALID_LESSON_SCOPES)!r}. Supply a valid "
                "scope and re-run apply — this is a malformed decisions map, not "
                "a ceremony failure, and nothing has been written."
            )
        supplied = [k for k in ("body", "body_file") if str(lesson.get(k) or "").strip()]
        if len(supplied) != 1:
            raise ValueError(
                f"decisions[{_KEY_LESSONS!r}][{idx}] supplied {supplied!r} for its "
                "body; exactly one of 'body' or 'body_file' is required. Supply "
                "one and re-run apply — this is a malformed decisions map, not a "
                "ceremony failure, and nothing has been written."
            )
        wants_queue = lesson.get("scope") == "universal" and all(
            lesson.get(k) not in (None, "")
            for k in ("queue_title", "queue_body", "surface", "proposed_action", "change_kind")
        )
        out.append((idx, lesson, wants_queue))
    return out


def build_lesson_capture_directives(
    decisions: dict[str, Any], repo_root: Optional[Path] = None
) -> list[dict[str, Any]]:
    """Step 1 / Step 1.2's mechanical directive tail.

    Expects `decisions["lessons"]`: a list of dicts, one per lesson this
    session has already decided (via the `lesson-worth-capturing`
    judgment, `judgments.py`) is worth capturing — a lesson the judgment
    resolved as "not worth it" simply never appears in this list, which is
    how that judgment's gate is expressed here (no runtime `depends_on`
    edge needed, matching this module's plan-claim/stamp pair above). Each
    entry:
        {"title": str, "body": str, "scope": "universal"|"project"|"wiki-only"}
    where `body` may be replaced by `body_file` (a path the CLI reads) —
    exactly one of the two, matching `coordinator-lesson-add`'s own
    exactly-one-of contract. A multi-paragraph `body` needs no such
    substitution: `_lesson_body_args` spools it and forwards `--body-file`
    itself, which is what `repo_root` is for. Supply `repo_root` whenever a
    body may run past one line; omitting it is legal only for single-line
    bodies (see `_lesson_body_args`).

    Plus, optionally, any of the structured facets `coordinator-lesson-add`
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
    for idx, lesson, wants_queue in _iter_capturable_lessons(decisions):
        add_id = lesson_add_directive_id(idx)
        add_args = _lesson_title_args(idx, lesson)
        add_args += _lesson_body_args(idx, lesson, repo_root)
        add_args += ["--scope", str(lesson["scope"])]
        for key, flag in _LESSON_OPTIONAL_FLAGS:
            if key in _LESSON_BODY_KEYS | _LESSON_TITLE_KEYS | _LESSON_WHY_KEYS:
                continue
            value = lesson.get(key)
            if value in (None, ""):
                continue
            add_args += [flag, str(value)]
        add_args += _lesson_why_args(lesson)
        directives.append(_directive(add_id, "coordinator-lesson-add", add_args))
        if wants_queue:
            queue_args = ["--schema", "improvement-queue", "--queue-scope", "central"]
            queue_args += ["--title", str(lesson["queue_title"])]
            queue_args += _queue_body_args(idx, lesson, repo_root)
            queue_args += ["--surface", str(lesson["surface"])]
            queue_args += ["--proposed-action", str(lesson["proposed_action"])]
            queue_args += ["--change-kind", str(lesson["change_kind"])]
            queue_args += ["--status", "open"]
            directives.append(
                _directive(
                    lesson_queue_directive_id(idx),
                    "coordinator-queue-append",
                    queue_args,
                    depends_on=add_id,
                )
            )
    return directives
