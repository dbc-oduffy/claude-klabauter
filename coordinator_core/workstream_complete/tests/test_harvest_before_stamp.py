"""
coordinator_core.workstream_complete.tests.test_harvest_before_stamp — Item 10
(IBMDT-C13, docs/plans/2026-09-26-inbox-blitz-claude-klabauter-fixes-doe-thread.md § C13).

Purpose: pin that Step 2.4b's deferral-harvest sweep (`d-harvest-deferrals-N`)
runs BEFORE `d-stamp-plan-implemented` can archive the governing plan, and
that this ordering is not merely cosmetic list position but an enforced
dispatch-time gate — `d-stamp-plan-implemented`'s own args carry one
`{d-harvest-deferrals-N.landed}` token per harvest directive, mirroring the
existing `{d-claim-plan-execution-lock.landed}` idiom this builder already
used. `apply._resolve_arg_tokens` refuses to dispatch a directive whose
token producer "did not land before this directive this pass (failed,
blocked, or not yet dispatched)" — so the token both requires
list-order-precedence AND fails the stamp loud if the harvest it names
never landed.

Negative-spec: does NOT re-exercise `apply._execute_directives`'s own
per-directive-halt/sibling-isolation behaviour — that is pinned directly in
`test_apply.py::test_harvest_deferrals_failure_does_not_abort_sibling_
directives` and `test_harvest_deferrals_nonzero_exit_reaches_the_ceremony_
exit_code`, unchanged by this row. This file only pins `build_directives`'s
own emission shape: ordering and the token wiring, at the pure-builder level.
"""

from __future__ import annotations

from pathlib import Path

import coordinator_core.workstream_complete as wsc
from coordinator_core.workstream_complete import directives_lessons_plan
from coordinator_core.ops.ceremony.wsc_disposition import SINGLE_SESSION


def _gate(sid: str = "testsid-harvest-before-stamp") -> wsc.SessionShapeGate:
    return wsc.SessionShapeGate(
        sid=sid,
        disposition=SINGLE_SESSION,
        consumed_handoff="",
        diagnostics=[],
        consumed_handoff_paths=(),
    )


def _governing_plan(tmp_path: Path, slug: str = "2026-09-26-a-governing-plan") -> directives_lessons_plan.GoverningPlan:
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    plan_path = plans_dir / f"{slug}.md"
    plan_path.write_text(f"# {slug}\n", encoding="utf-8")
    return directives_lessons_plan.GoverningPlan(
        slug=slug,
        path=plan_path,
        rel=directives_lessons_plan._rel_to_repo(plan_path, tmp_path),  # noqa: SLF001 - same-package sibling helper, test fixture
    )


def test_harvest_directive_precedes_the_stamp_in_emission_order(tmp_path: Path) -> None:
    """The single-plan case: `d-harvest-deferrals-1` must appear in
    `directives[]` at an earlier index than `d-stamp-plan-implemented` —
    `apply._resolve_arg_tokens`'s `.landed` token requires its producer to
    already be a key of `stdout_by_id`, which is populated strictly in
    `directives[]` dispatch order."""
    governing_plan = _governing_plan(tmp_path)
    directives = wsc.build_directives(_gate(), {}, tmp_path, governing_plan=governing_plan)

    ids = [d["id"] for d in directives]
    assert "d-harvest-deferrals-1" in ids
    assert "d-stamp-plan-implemented" in ids
    assert ids.index("d-harvest-deferrals-1") < ids.index("d-stamp-plan-implemented")


def test_stamp_args_carry_a_landed_token_for_the_harvest_directive(tmp_path: Path) -> None:
    """`d-stamp-plan-implemented`'s args must name `d-harvest-deferrals-1`
    via a `{d-harvest-deferrals-1.landed}` token — the actual dispatch-time
    enforcement, not just favorable list position. Mirrors the existing
    `{d-claim-plan-execution-lock.landed}` token already on this directive."""
    governing_plan = _governing_plan(tmp_path)
    directives = wsc.build_directives(_gate(), {}, tmp_path, governing_plan=governing_plan)

    by_id = {d["id"]: d for d in directives}
    stamp_args = by_id["d-stamp-plan-implemented"]["args"]
    assert "{d-claim-plan-execution-lock.landed}" in stamp_args
    assert "{d-harvest-deferrals-1.landed}" in stamp_args


def test_no_governing_plan_emits_neither_harvest_nor_stamp(tmp_path: Path) -> None:
    """Zero tax on plan-less sessions: no `governing_plan` means no harvest
    sweep and no stamp/claim -- the predicate gate this builder already had
    is unchanged by threading the token through."""
    directives = wsc.build_directives(_gate(), {}, tmp_path, governing_plan=None)

    ids = [d["id"] for d in directives]
    assert "d-harvest-deferrals-1" not in ids
    assert "d-stamp-plan-implemented" not in ids
    assert "d-claim-plan-execution-lock" not in ids


def test_additional_governing_plan_slug_gets_its_own_ordered_harvest_and_token(tmp_path: Path) -> None:
    """`decisions["additional_governing_plan_slugs"]` resolves a second
    harvest target (`d-harvest-deferrals-2`) -- it too must precede the
    stamp and be named on its `.landed` token, alongside the primary plan's
    `d-harvest-deferrals-1`."""
    governing_plan = _governing_plan(tmp_path, slug="2026-09-26-a-governing-plan")
    additional_slug = "2026-09-20-a-second-governing-plan"
    (tmp_path / "docs" / "plans" / f"{additional_slug}.md").write_text("# second\n", encoding="utf-8")

    directives = wsc.build_directives(
        _gate(),
        {"additional_governing_plan_slugs": [additional_slug]},
        tmp_path,
        governing_plan=governing_plan,
    )

    ids = [d["id"] for d in directives]
    assert ids.index("d-harvest-deferrals-1") < ids.index("d-stamp-plan-implemented")
    assert ids.index("d-harvest-deferrals-2") < ids.index("d-stamp-plan-implemented")

    by_id = {d["id"]: d for d in directives}
    stamp_args = by_id["d-stamp-plan-implemented"]["args"]
    assert "{d-harvest-deferrals-1.landed}" in stamp_args
    assert "{d-harvest-deferrals-2.landed}" in stamp_args
