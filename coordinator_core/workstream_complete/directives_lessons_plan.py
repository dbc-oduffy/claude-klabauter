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
computed-frontage.census-steps.md`, example-doctrine-repo): Step 1, Step 1.2 mechanical
tail, Step 2 locate + predicate, Step 2.4 governing-plan predicate + claim
+ stamp, Step 2.4b harvest sweep.

Consumes manifest (orchestrates, reimplements none):
    coordinator/bin/coordinator-lesson-add
        -> d-add-lesson-<n> (one per captured lesson)
    coordinator/bin/coordinator-queue-append
        -> d-queue-append-lesson-<n> (one per universal-scoped lesson)
    coordinator/bin/wsc-coverage-gate-runner.py claim-plan
        -> d-claim-plan-execution-lock
    coordinator/bin/archive-stamp-cli stamp-plan-implemented
        -> d-stamp-plan-implemented
    coordinator/bin/coordinator-harvest-deferrals
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
      for `d-claim-plan`) as the primary signal, falling back to a
      disk-only ordered search only when neither is supplied — it does
      NOT attempt to guess "the" governing plan among an unscoped
      `docs/plans/` directory, which the census's own "search fixed
      candidate paths in order" framing under-specifies for a repo
      carrying hundreds of plan docs.
    - No `## Deviations` audit-table writing, no plan-body mutation of
      any kind — Step 2's ALLOWLIST reconcile and Step 2/2.4b's content
      updates are judgment-authored Edits the EM performs directly
      (`plan-body-edits-never-route-to-executor`), never a directive
      this module emits.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, NamedTuple, Optional


# ---------------------------------------------------------------------------
# gates.governing_plan — Step 2 (locate) / Step 2.4 + 2.4b (predicate)
# ---------------------------------------------------------------------------

#: Disk-only fallback search order for Step 2's "actively search" list,
#: restricted to the legs that are actually repo_root-resolvable (see the
#: module Negative-spec above for why "session context (opened docs)" is
#: excluded). `docs/plans/` is checked before `tasks/plans/` because it is
#: this repo's canonical plan location (`CLAUDE.md § Key Files`).
_GOVERNING_PLAN_GLOB_DIRS = ("docs/plans", "tasks/plans")
_GOVERNING_PLAN_FIXED_FILES = ("tasks/todo.md", "tasks/plan.md")

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
) -> Optional[GoverningPlan]:
    """Step 2 — locate the governing plan doc (`d-locate-governing-plan`).
    Thin wrapper over `resolve_governing_plan_with_source` for callers that
    only need the resolved plan, not which source resolved it."""
    return resolve_governing_plan_with_source(
        repo_root, decisions, handoff_governing_plan_field, consumed_handoff_deliverable_id
    )[0]


def resolve_governing_plan_with_source(
    repo_root: Path,
    decisions: dict[str, Any],
    handoff_governing_plan_field: Optional[Any] = None,
    consumed_handoff_deliverable_id: Optional[Any] = None,
) -> tuple[Optional[GoverningPlan], str]:
    """Step 2 — locate the governing plan doc (`d-locate-governing-plan`),
    also returning which source resolved it (or why none did) so a caller
    can make that absence legible rather than silently losing the four
    plan-gated directives with no trace (see `build_directives`'s
    `preflight["governing_plan_resolution"]`).

    Precedence, highest first:
      1. Caller-supplied `decisions["governing_plan_slug"]` (checked
         against `docs/plans/<slug>.md` then `tasks/plans/<slug>.md`) —
         see the module Negative-spec for why this explicit override is
         the primary signal, not a guess.
      2. Caller-supplied `decisions["governing_plan_path"]` (used
         verbatim, resolved against `repo_root` if relative).
      3. The chain-terminal session's own consumed handoff's
         `governing_plan:` frontmatter field, pre-extracted and passed in
         as `handoff_governing_plan_field` by `__init__.py` (this module
         has no handoff-reading concern of its own — see
         `_read_consumed_handoff_text` / `_governing_plan_field_from_
         consumed_handoff` there, including their archived-handoff
         fallback). This is a genuinely disk-resolvable source, unlike
         the "session context (opened docs)" leg the Negative-spec
         excludes — it is the ceremony's own prior-session provenance
         record, not a guess among an unscoped `docs/plans/` directory.
      3.5. The chain-terminal session's own consumed handoff's
         `deliverable_id` frontmatter field, pre-extracted and passed in
         as `consumed_handoff_deliverable_id`, joined against the single
         `docs/plans/*.md` carrying the SAME `deliverable_id` — composed,
         never re-derived, from `__init__.py`'s own
         `_resolve_session_handoff_plan_by_deliverable_id` (the identical
         join `_evaluate_session_handoff_leg_a` already performs for leg
         A's acceptance-criteria gate, imported here function-locally to
         avoid a circular import against `__init__.py`, which imports this
         module at module scope). This leg exists because leg 3 above is,
         in practice, dead: a fleet-wide sweep found the `governing_plan:`
         frontmatter field on 0 of 276 live handoffs, while `deliverable_id`
         resolves for nearly all of them (see `_evaluate_session_handoff_
         leg_a`'s own docstring, PM ruling R2, docs/plans/2026-08-04-
         terminal-state-propagation-join-keys.md § C12) — the ceremony was
         stamping "no governing plan" for almost every session, silently
         dropping the plan-claim/stamp/harvest directives and leaving the
         plan's baton-advancing `deliverable.cascade_terminal` cascade
         unreachable. Terminal like every leg above it: an ambiguous join
         (`_resolve_session_handoff_plan_by_deliverable_id` returns `None`
         on zero OR more than one match — it does not guess) or a missing
         `deliverable_id` falls through to leg 4, it never raises and
         never fabricates a plan.
      4. The fixed `tasks/todo.md` / `tasks/plan.md` candidates.

    Each of 1-3.5 is a terminal override once present: if the caller (or
    the handoff) names a plan and it does not exist on disk, this returns
    `None` immediately rather than falling through to a lower-precedence
    source or a fixed fallback — Step 2.4's own text: "Do NOT invent a
    plan to reconcile against," and an explicit-but-wrong override should
    not silently resolve to some other plan the caller didn't name. Leg
    3.5 is the one exception to "terminal once present": an unresolved
    join (no `deliverable_id`, or one matching zero/more-than-one plan)
    falls through rather than terminating, because — unlike legs 1-3 —
    there is no explicit plan name here to have gotten wrong; the absence
    of a join is simply silent, exactly like the absence of any signal at
    all.
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

    deliverable_id_value = _normalize_handoff_governing_plan_field(consumed_handoff_deliverable_id)
    if deliverable_id_value:
        # Function-local import: `__init__.py` imports this module at module
        # scope (`from coordinator_core.workstream_complete import
        # directives_lessons_plan`), so a module-scope import in the other
        # direction would be circular — same idiom `deliverable_cascade.
        # _predicate_refusal` already uses for its own reverse-membership
        # import of `ops.handoff_children`.
        from coordinator_core.workstream_complete import (
            _resolve_session_handoff_plan_by_deliverable_id,
        )

        joined_plan_path = _resolve_session_handoff_plan_by_deliverable_id(repo_root, deliverable_id_value)
        if joined_plan_path is not None:
            return GoverningPlan(slug=joined_plan_path.stem, path=joined_plan_path), "deliverable_id_join"

    for fname in _GOVERNING_PLAN_FIXED_FILES:
        candidate = repo_root / fname
        if candidate.is_file():
            return GoverningPlan(slug=candidate.stem, path=candidate), "fixed_fallback"
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
    moved on" — example-doctrine-repo-em could not establish from it whether the new
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
    only Step 2.4b's harvest fans out. `handoff_governing_plan_field` and
    `consumed_handoff_deliverable_id` forward unchanged to
    `resolve_governing_plan` — see that function's docstring for the
    precedence they slot into.
    """
    governing_plan = resolve_governing_plan(
        repo_root, decisions, handoff_governing_plan_field, consumed_handoff_deliverable_id
    )
    directives = build_plan_claim_and_stamp_directives(governing_plan)

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
