"""
coordinator_core.plan_assemble.predicates.composed — Layer 2: pure
composition over already-computed Layer 0 / Layer 1 predicate outputs, the
ten `:44` / `:57` / `:59` / `:90(7)` / `:91` / `:105(1)` / `:105(2)` /
`:134` / `:139` / `:195-198` contract rows.

Purpose: every function in this module takes the *return values* of Layer 0
leaf readers (`triage.py`, `substrate_seven_dim.py`, `substrate_scans.py`,
`composition_lints.py`) and Layer 1 shared booleans (`shared_booleans.py`)
and recombines them. **This module performs zero disk reads.** If a row
here needed to open a file, that is a Layer 0 gap, not something to patch
here — the fix is a new/extended Layer 0 row, reported BLOCKED rather than
reached for.

Three-valued composition is the house convention throughout this module:
`_ternary_and`/`_ternary_or` implement AND/OR over inputs that may
individually be a plain boolean or the `undetermined` sentinel. An
`undetermined` arm never silently reads as `False` — it only surfaces as
`False`/`True` in the aggregate when a decisive arm on the other side
(`False` for AND, `True` for OR) already settles the outcome regardless of
what the unresolved arm turns out to be; otherwise the aggregate itself is
`undetermined`. `:91`'s row states this rule explicitly ("an arm that is
undetermined makes the aggregate undetermined, not false") — this module
applies the identical rule to `:44`/`:57` for consistency, since the
contract's own three-valued framing generalizes cleanly to both AND and
OR.

Rows, and the Layer 0/1 output shapes each one composes:
  :44        `trivial_conjunction` — AND of `:105(3a)`
             (`shared_booleans.collapse_scope_file_count`'s
             `scope_file_count_le_2`) and `:105(3d)`
             (`shared_booleans.collapse_no_cross_repo_contract`'s
             `no_cross_repo_contract`).
  :57        `nontrivial_disjunction` — OR across those same two booleans
             plus `:118` (`substrate_scans.compute()["reverses_teardown"]`'s
             `candidate`), `:159` (`substrate_scans.compute()
             ["mutates_shared_symbol"]`'s `mutates_shared_symbol`), and
             `:166` (`substrate_scans.compute()["scaffold_checklist"]` —
             presence of a non-`undetermined` row IS the "plan scaffolds a
             skill/agent" signal, matching that producer's own convention:
             it emits `undetermined` exactly when no `## Scaffold
             Checklist` section exists, never a `False` guess).
  :59        `architectural_tier_judgment_point` — a real, answerable
             `judgment_points[]` entry (C9, 2026-08-16: rebuilt through
             `build_judgment_point`, replacing the hand-rolled
             `{candidate_criteria, disposition: None}` dict that shared
             zero keys with the contract shape and was structurally
             unanswerable). Presents three of the four architectural-tier
             criteria as candidate evidence, `multi-stakeholder` genuinely
             absent, `recommendation: None` (the engine presents evidence,
             the EM names which criterion fires). See AC4 in the
             negative-spec.
  :90(7)     `seven_dim_fix_locus` — the `:111`/`:112` fields
             (`substrate_scans.compute()["fix_locus"]`) recombined under
             `gates.substrate.seven_dim.fix_locus`, verbatim.
  :91        `seven_dim_all_green` — AND over the four typed arms this
             build has producers for (`:90(1)(2)(4)(5)` —
             `substrate_seven_dim.compute()["seven_dim"]`). See
             negative-spec for why `:90(3)(6)(7)` are excluded.
  :105(1)    `collapse_seven_dim_green` — `:91`'s own output, unchanged,
             consumed under the collapse namespace. No new logic.
  :105(2)    `collapse_premise_gate_green` — `:94`'s
             (`substrate_seven_dim.premise_gate`) fields consumed under
             collapse, INHERITING THE M-BAND EXCEPTION: `tshirt == "M"`
             always resolves `undetermined`, never a computed boolean.
  :134       `scope_mode_declared` — `:73`'s `scope_mode` row tested for
             non-null. No new producer.
  :139       `route_triggers_review` — `:33`/`:34`'s `route` field tested
             against the review-triggering set. That set is grounded in
             `coordinator/skills/plan/SKILL.md:211-214`'s terminal table:
             only the `plan` route reaches the full terminal that invokes
             `coordinator:plan`'s Branch (3) `coordinator:review` pipeline
             step; `spec-dispatch`'s light terminal explicitly runs "No
             Opus plan review" and substitutes a `code-reviewer` diff pass
             instead. `shape`/`roadmap`/`pm-decision` never reach Exit at
             all (Branch A diverted them). `_REVIEW_TRIGGERING_ROUTES` is
             therefore `{"plan"}`.
  :195-198   `terminal_table_result` — static route -> terminal lookup,
             grounded in the same `SKILL.md:211-214` table
             (`plan` -> `full_terminal`, `spec-dispatch` -> `light_terminal`).
             Any other route name resolves `undetermined` — that table has
             no third row, and every route it omits is documented as never
             reaching Exit by construction.

Negative-spec:
  - Does NOT open any file, run any grep, or call `git`. Every input this
    module reads is a parameter — a dict or scalar a Layer 0/1 producer
    already computed. A missing Layer 0 row is a BLOCKED report, never a
    disk read reached for here.
  - Does NOT fold `:90(3)`/`:90(6)` into `:91`'s AND — the contract's own
    row (`:91`, DoE spec `archive/specs/2026-08/2026-08-08-stop-the-rot-
    pickup-and-workstream-complete.md:632`) frames those two as "once an
    EM supplies them"; this build's Layer 0 fan-out has no producer for
    either, so they are absent from the conjunction rather than silently
    assumed `True`. Does NOT fold `:90(7)` (`seven_dim_fix_locus`) into the
    AND either — that row's own contract entry (`:90(7)`) is a dict of two
    named sub-fields (`citation_present`, `registry_has_gate_type`), not a
    single boolean the contract defines a green/red collapse rule for;
    inventing one here would be exactly the kind of un-spec'd disposition
    AC4 exists to prevent. This is a scoped interpretation, flagged for
    the EM at wave close alongside the `:90(3)(6)` gap.
  - Does NOT write a `.verdict`/`.recommended`/`.fires` field into `:59`'s
    judgment point. `candidate_criteria` (preserved verbatim as an extra
    top-level key on the built point, alongside the canonical
    `build_judgment_point` shape) carries exactly three `{criterion,
    computed}` pairs; `multi-stakeholder` has no list entry at all (not
    `false`, not `undetermined`); `recommendation` is the literal Python
    `None` — this is AC4, the one guardrail the whole contract has. (C9,
    2026-08-16: the hand-rolled `disposition: None` field this used to
    carry is gone -- `recommendation: None` is its contract-shaped
    successor, same meaning, "the engine presents evidence, the EM names
    the criterion.")
  - Does NOT invert or recompute any reused Layer 0/1 field's boolean
    sense. `:59`'s three reused criteria (`:105(3d)`, `:153`, `:112`) are
    surfaced exactly as their own producer computed them — presenting
    candidate evidence, not deciding what it means.
  - Does NOT treat `:105(2)`'s M-band case as a computed `False`. On
    `tshirt == "M"`, `collapse_premise_gate_green` returns `undetermined`
    unconditionally — the detent never fired, so there is no green/red
    verdict to report, and reporting `False` would misstate "evaluated,
    failed" for "never evaluated".
  - Does NOT invent an alternate review-triggering route set. The set
    used by `:139` is the two-row terminal table `SKILL.md:211-214`
    already carries — reused, not re-derived — and every route the table
    omits resolves `undetermined` rather than guessing membership.

Spec backlink: pln-plan-assemble-wave-2-the-predi-fad89b, chunk C12
"""
from __future__ import annotations

from typing import Any

from coordinator_core.contract.decision_object.judgment import (
    build_disposition,
    build_judgment_point,
)
from coordinator_core.plan_assemble.predicates import undetermined

_ARCHITECTURAL_TIER_JUDGMENT_POINT_ID = "architectural-tier-criterion-classification"

_REVIEW_TRIGGERING_ROUTES: frozenset[str] = frozenset({"plan"})

_TERMINAL_TABLE: dict[str, str] = {
    "plan": "full_terminal",
    "spec-dispatch": "light_terminal",
}


def _is_undetermined(value: Any) -> bool:
    return isinstance(value, dict) and value.get("undetermined") is True


def _field(value: Any, key: str) -> Any:
    if _is_undetermined(value):
        return value
    if isinstance(value, dict):
        return value.get(key)
    return value


def _ternary_and(*values: Any) -> Any:
    saw_undetermined = False
    for value in values:
        if _is_undetermined(value):
            saw_undetermined = True
            continue
        if not value:
            return False
    if saw_undetermined:
        return undetermined("one or more input arms undetermined")
    return True


def _ternary_or(*values: Any) -> Any:
    saw_undetermined = False
    for value in values:
        if _is_undetermined(value):
            saw_undetermined = True
            continue
        if value:
            return True
    if saw_undetermined:
        return undetermined("one or more input arms undetermined")
    return False


def trivial_conjunction(
    scope_file_count_row: dict[str, Any],
    no_cross_repo_contract_row: dict[str, Any],
) -> Any:
    a = _field(scope_file_count_row, "scope_file_count_le_2")
    b = _field(no_cross_repo_contract_row, "no_cross_repo_contract")
    return _ternary_and(a, b)


def nontrivial_disjunction(
    scope_file_count_row: dict[str, Any],
    no_cross_repo_contract_row: dict[str, Any],
    reverses_teardown_row: dict[str, Any],
    mutates_shared_symbol_row: dict[str, Any],
    scaffold_checklist_row: dict[str, Any],
) -> Any:
    a = _field(scope_file_count_row, "scope_file_count_le_2")
    b = _field(no_cross_repo_contract_row, "no_cross_repo_contract")
    c = _field(reverses_teardown_row, "candidate")
    d = _field(mutates_shared_symbol_row, "mutates_shared_symbol")
    return _ternary_or(a, b, c, d, scaffold_checklist_row)


def architectural_tier_judgment_point(
    no_cross_repo_contract_row: dict[str, Any],
    concurrency_shared_state_row: dict[str, Any],
    fix_locus_row: dict[str, Any],
) -> dict[str, Any]:
    candidate_criteria = [
        {
            "criterion": "cross-system-irreversible",
            "computed": _field(no_cross_repo_contract_row, "no_cross_repo_contract"),
        },
        {
            "criterion": "security-privacy-boundary",
            "computed": _field(concurrency_shared_state_row, "candidate"),
        },
        {
            "criterion": "naming-collision-with-product-policy",
            "computed": _field(fix_locus_row, "registry_has_gate_type"),
        },
    ]
    evidence = (
        "candidate architectural-tier criteria (`multi-stakeholder` has no "
        "producer, genuinely absent): "
        + "; ".join(f"{c['criterion']}={c['computed']!r}" for c in candidate_criteria)
    )
    point = build_judgment_point(
        None,
        id=_ARCHITECTURAL_TIER_JUDGMENT_POINT_ID,
        question=(
            "Does this plan trigger an architectural-tier criterion "
            "(cross-system-irreversible, security-privacy-boundary, or "
            "naming-collision-with-product-policy), and if so, which?"
        ),
        dispositions=[
            build_disposition("cross-system-irreversible", resolves=[]),
            build_disposition("security-privacy-boundary", resolves=[]),
            build_disposition("naming-collision-with-product-policy", resolves=[]),
            build_disposition("none-fire", resolves=[]),
        ],
        evidence=evidence,
        reason=(
            "the engine presents candidate evidence for three of the four "
            "architectural-tier criteria (`multi-stakeholder` has no "
            "producer); it does not itself decide which one fires -- that "
            "is a qualitative call this module's negative-spec reserves "
            "for the EM, so `recommendation` stays `None` rather than "
            "manufacturing a verdict"
        ),
        revalidate_at_dispatch=True,
        reportable=False,
    )
    point["candidate_criteria"] = candidate_criteria
    return point


def seven_dim_fix_locus(fix_locus_row: dict[str, Any]) -> Any:
    if _is_undetermined(fix_locus_row):
        return fix_locus_row
    return {
        "citation_present": fix_locus_row.get("citation_present"),
        "registry_has_gate_type": fix_locus_row.get("registry_has_gate_type"),
    }


def seven_dim_all_green(seven_dim_row: dict[str, Any]) -> Any:
    """`:91` -> `gates.substrate.seven_dim.all_green` (bool).

    AND over the four typed arms this build has producers for —
    `:90(1)` `no_duplicate`, `:90(2)` `no_fabrication`, `:90(4)`
    `official_docs_read`, `:90(5)` `reference_impl_seen` — from
    `substrate_seven_dim.compute()["seven_dim"]`. `:90(3)(6)(7)` are
    excluded; see this module's negative-spec. An arm that is
    `undetermined` makes the aggregate `undetermined`, NEVER `False`.

    ARM-SHAPE ASYMMETRY, CONFIRMED DELIBERATE (code-reviewer flagged this
    extraction as suspiciously asymmetric; verified against
    `substrate_seven_dim.py` on disk, not assumed). Three of the four arms
    (`seven_dim_no_duplicate`, `seven_dim_official_docs_read`,
    `seven_dim_reference_impl_seen`) return a BARE value on success — either
    `True` or the `undetermined(...)` sentinel dict directly, never wrapped
    in a named sub-key. `seven_dim_no_fabrication` is the sole genuine
    exception: on success it returns
    `{"no_fabrication": bool, "absent_citations": [...]}`, a dict carrying an
    extra field (`absent_citations`) no other arm has, so it cannot be
    collapsed to the same bare shape without losing that evidence at its own
    producer. The asymmetric extraction below — three bare `.get(...)` reads
    plus one `_field(...)` unwrap — is therefore correct, not a bug:
    `_field`'s own `undetermined`-passthrough means the unwrap is also safe
    when `no_fabrication` itself resolves `undetermined` (the whole sentinel
    propagates unchanged rather than being indexed into)."""
    no_duplicate = seven_dim_row.get("no_duplicate")
    no_fabrication = _field(seven_dim_row.get("no_fabrication"), "no_fabrication")
    official_docs_read = seven_dim_row.get("official_docs_read")
    reference_impl_seen = seven_dim_row.get("reference_impl_seen")
    return _ternary_and(no_duplicate, no_fabrication, official_docs_read, reference_impl_seen)


def collapse_seven_dim_green(all_green_value: Any) -> Any:
    return all_green_value


def collapse_premise_gate_green(premise_gate_row: dict[str, Any]) -> Any:
    """`:105(2)` -> `gates.substrate.collapse.premise_gate_green`.

    `:94`'s (`substrate_seven_dim.premise_gate`) fields consumed under
    collapse. INHERITS THE M-BAND EXCEPTION: on `tshirt == "M"` the
    premise-gate detent never fires, so this always resolves
    `undetermined` — never a computed `True`/`False` — regardless of what
    `m_band_uncovered` itself says."""
    m_band_uncovered = _field(premise_gate_row, "m_band_uncovered")
    if _is_undetermined(m_band_uncovered):
        return m_band_uncovered
    tshirt = _field(premise_gate_row, "tshirt")
    if _is_undetermined(tshirt):
        return tshirt
    if tshirt == "M":
        return undetermined(
            "M-band premise-gate detent (_LARGE_TSHIRTS) never fires for "
            "tshirt='M'; :94's m_band_uncovered evidence names the gap, "
            "it does not evaluate the gate"
        )
    return not m_band_uncovered


def scope_mode_declared(scope_mode_row: dict[str, Any]) -> Any:
    value = _field(scope_mode_row, "value")
    if _is_undetermined(value):
        return value
    return value is not None


def route_triggers_review(route_row: dict[str, Any]) -> Any:
    """`:139` -> reuses `gates.triage.route` tested against the
    review-triggering set (`_REVIEW_TRIGGERING_ROUTES`, grounded in
    `SKILL.md:211-214`'s terminal table — only `plan` invokes
    `coordinator:review`)."""
    route = _field(route_row, "route")
    if _is_undetermined(route):
        return route
    return route in _REVIEW_TRIGGERING_ROUTES


def terminal_table_result(route_row: dict[str, Any]) -> dict[str, Any]:
    """`:195-198` -> `gates.exit.terminal_table.result` (enum).

    Static route -> terminal lookup (`_TERMINAL_TABLE`), keyed on
    `gates.triage.route`. A route the table has no entry for (every route
    other than `plan`/`spec-dispatch`, all of which are documented as
    never reaching Exit) resolves `undetermined`, not a guessed default."""
    route = _field(route_row, "route")
    if _is_undetermined(route):
        return {"result": route}
    result = _TERMINAL_TABLE.get(route)
    if result is None:
        return {
            "result": undetermined(
                f"no terminal-table entry for route {route!r}; "
                "SKILL.md:211-214 maps only plan/spec-dispatch, and every "
                "other route is documented as never reaching Exit"
            )
        }
    return {"result": result}


__all__ = [
    "trivial_conjunction",
    "nontrivial_disjunction",
    "architectural_tier_judgment_point",
    "seven_dim_fix_locus",
    "seven_dim_all_green",
    "collapse_seven_dim_green",
    "collapse_premise_gate_green",
    "scope_mode_declared",
    "route_triggers_review",
    "terminal_table_result",
]
