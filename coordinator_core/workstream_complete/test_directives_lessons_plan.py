"""test_directives_lessons_plan — direct unit coverage of
`coordinator_core.workstream_complete.directives_lessons_plan.
resolve_governing_plan_with_source`'s new deliverable_id-join leg (3.5).

Cross-repo/inbox baton-lifecycle investigation: leg 3 (the consumed
handoff's `governing_plan:` frontmatter) was found dead fleet-wide — 0 of
276 live handoffs carry it — so `build_plan_claim_and_stamp_directives`
almost never fires, `d-stamp-plan-implemented` never emits, and the
downstream `deliverable.cascade_terminal` baton-advance never triggers
(197 of ~204 fleet-wide `/workstream-complete` receipts record
`no-governing-plan-slug`). This file pins the new leg composed from
`__init__.py`'s existing `_resolve_session_handoff_plan_by_deliverable_id`
join, which resolves from the SAME consumed handoff's `deliverable_id`
frontmatter field instead.

`__init__.py`'s `wsc.brief()` owns end-to-end coverage through the full
ceremony (see `test_workstream_complete.py`'s governing-plan section);
this file only exercises the pure resolution function directly, following
this module family's own established convention (see
`test_directives_review_scale.py`'s own docstring for the same chunk
split).
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.workstream_complete.directives_lessons_plan import (
    build_plan_claim_and_stamp_directives,
    resolve_governing_plan_with_source,
)


def _write_plan(tmp_path: Path, slug: str, deliverable_id: str | None = None, status: str | None = None) -> None:
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    if status is not None:
        lines.append(f"status: {status}")
    if deliverable_id is not None:
        lines.append(f"deliverable_id: {deliverable_id}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {slug}")
    lines.append("")
    (plan_dir / f"{slug}.md").write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# The real failing shape: no decisions, no governing_plan: frontmatter, no
# tasks/todo.md -- only a deliverable_id join available.
# ---------------------------------------------------------------------------


def test_deliverable_id_join_resolves_governing_plan_when_frontmatter_leg_is_dead(tmp_path):
    slug = "2026-08-04-some-shipped-plan"
    _write_plan(tmp_path, slug, deliverable_id="dr-129")

    resolved, source = resolve_governing_plan_with_source(
        tmp_path,
        decisions={},
        handoff_governing_plan_field=None,
        consumed_handoff_deliverable_id="dr-129",
    )

    assert source == "deliverable_id_join"
    assert resolved is not None
    assert resolved.slug == slug
    assert resolved.path == tmp_path / "docs" / "plans" / f"{slug}.md"


def test_deliverable_id_join_emits_claim_and_stamp_directives():
    from coordinator_core.workstream_complete.directives_lessons_plan import GoverningPlan

    directives = build_plan_claim_and_stamp_directives(GoverningPlan(slug="dr-129-plan", path=Path("unused")))
    ids = {d["id"] for d in directives}
    assert "d-claim-plan-execution-lock" in ids
    assert "d-stamp-plan-implemented" in ids


def test_stamp_directive_depends_on_the_claim_landing():
    """Field-level pin (secondary check only — see the behavioural test
    below for the actual enforcement). `depends_on` naming a SIBLING
    DIRECTIVE id is inert at `apply_halt.py::_directive_gate_open` (hits a
    bare `continue`); it documents intent for other tooling but does not
    gate. The `{d-claim-plan-execution-lock.landed}` arg token is what
    actually enforces this — see `build_plan_claim_and_stamp_directives`'s
    own docstring for the incident this closes."""
    from coordinator_core.workstream_complete.directives_lessons_plan import GoverningPlan

    directives = build_plan_claim_and_stamp_directives(GoverningPlan(slug="dr-129-plan", path=Path("unused")))
    by_id = {d["id"]: d for d in directives}
    assert by_id["d-stamp-plan-implemented"]["depends_on"] == "d-claim-plan-execution-lock"
    assert by_id["d-claim-plan-execution-lock"]["depends_on"] is None
    assert by_id["d-stamp-plan-implemented"]["args"][-1] == "{d-claim-plan-execution-lock.landed}"


def test_stamp_directive_does_not_execute_when_claim_is_denied():
    """Behavioural coverage: drive the real directives through `apply.py`'s
    directive-execution seam (`_execute_directives`, the function `apply()`
    itself delegates to for directive dispatch/halt) with the claim
    directive's gate CLOSED (denied — modelled here as a judgment-point
    gate that never opens, the same shape a real denial takes), and assert
    the stamp CLI is never invoked and no plan mutation occurs.

    This replaces the pre-existing field-only assertion
    (`test_stamp_directive_depends_on_the_claim_landing` above) that let
    the guard ship inert — asserting `depends_on == "d-claim-plan-
    execution-lock"` proves nothing about whether the stamp actually runs,
    since `apply_halt.py::_directive_gate_open` does not gate on a
    sibling-directive `depends_on` at all.
    """
    from types import ModuleType

    from coordinator_core.workstream_complete import apply as ws_apply
    from coordinator_core.workstream_complete.directives_lessons_plan import GoverningPlan

    directives = build_plan_claim_and_stamp_directives(GoverningPlan(slug="dr-129-plan", path=Path("unused")))

    stamp_calls: list[list[str]] = []

    def claim_main(argv: list[str]) -> int:
        # Denied: the claim CLI itself exits non-zero, so it never lands.
        return 1

    def stamp_main(argv: list[str]) -> int:
        stamp_calls.append(list(argv))
        return 0

    def _fake_module(main_fn, name: str) -> ModuleType:
        mod = ModuleType(name)
        mod.main = main_fn
        return mod

    modules = {
        "wsc-coverage-gate-runner": _fake_module(claim_main, "fake_claim"),
        "archive-stamp-cli": _fake_module(stamp_main, "fake_stamp"),
    }
    original_load = ws_apply._load_cli_module
    ws_apply._load_cli_module = lambda cli_name: modules[cli_name]
    try:
        exit_code, report = ws_apply._execute_directives(directives, [], {})
    finally:
        ws_apply._load_cli_module = original_load

    assert stamp_calls == []
    assert "d-stamp-plan-implemented" not in report["landed"]
    assert "d-claim-plan-execution-lock" not in report["landed"]
    failed_ids = [entry["id"] for entry in report["failed"]]
    assert "d-claim-plan-execution-lock" in failed_ids
    assert "d-stamp-plan-implemented" in failed_ids


# ---------------------------------------------------------------------------
# Precedence -- EM-supplied legs 1/2 and the frontmatter leg 3 still win.
# ---------------------------------------------------------------------------


def test_decisions_slug_still_wins_over_deliverable_id_join(tmp_path):
    decisions_slug = "decisions-named-plan"
    joined_slug = "joined-plan"
    _write_plan(tmp_path, decisions_slug)
    _write_plan(tmp_path, joined_slug, deliverable_id="dr-129")

    resolved, source = resolve_governing_plan_with_source(
        tmp_path,
        decisions={"governing_plan_slug": decisions_slug},
        handoff_governing_plan_field=None,
        consumed_handoff_deliverable_id="dr-129",
    )

    assert source == "decisions_slug"
    assert resolved.slug == decisions_slug


def test_decisions_path_still_wins_over_deliverable_id_join(tmp_path):
    decisions_slug = "decisions-path-plan"
    joined_slug = "joined-plan"
    _write_plan(tmp_path, decisions_slug)
    _write_plan(tmp_path, joined_slug, deliverable_id="dr-129")

    resolved, source = resolve_governing_plan_with_source(
        tmp_path,
        decisions={"governing_plan_path": f"docs/plans/{decisions_slug}.md"},
        handoff_governing_plan_field=None,
        consumed_handoff_deliverable_id="dr-129",
    )

    assert source == "decisions_path"
    assert resolved.slug == decisions_slug


def test_handoff_frontmatter_leg_still_wins_over_deliverable_id_join(tmp_path):
    frontmatter_slug = "frontmatter-named-plan"
    joined_slug = "joined-plan"
    _write_plan(tmp_path, frontmatter_slug)
    _write_plan(tmp_path, joined_slug, deliverable_id="dr-129")

    resolved, source = resolve_governing_plan_with_source(
        tmp_path,
        decisions={},
        handoff_governing_plan_field=f"docs/plans/{frontmatter_slug}.md",
        consumed_handoff_deliverable_id="dr-129",
    )

    assert source == "handoff_frontmatter"
    assert resolved.slug == frontmatter_slug


# ---------------------------------------------------------------------------
# Clean fall-through -- no deliverable_id, no match, ambiguous match.
# ---------------------------------------------------------------------------


def test_no_deliverable_id_falls_through_to_fixed_fallback(tmp_path):
    (tmp_path / "tasks").mkdir()
    (tmp_path / "tasks" / "todo.md").write_text("# todo\n", encoding="utf-8")

    resolved, source = resolve_governing_plan_with_source(
        tmp_path,
        decisions={},
        handoff_governing_plan_field=None,
        consumed_handoff_deliverable_id=None,
    )

    assert source == "fixed_fallback"
    assert resolved.slug == "todo"


def test_deliverable_id_matching_no_plan_falls_through_cleanly(tmp_path):
    _write_plan(tmp_path, "unrelated-plan", deliverable_id="dr-999")

    resolved, source = resolve_governing_plan_with_source(
        tmp_path,
        decisions={},
        handoff_governing_plan_field=None,
        consumed_handoff_deliverable_id="dr-129",
    )

    assert resolved is None
    assert source == "none"


def test_deliverable_id_matching_multiple_plans_falls_through_cleanly(tmp_path):
    _write_plan(tmp_path, "plan-a", deliverable_id="dr-129")
    _write_plan(tmp_path, "plan-b", deliverable_id="dr-129")

    resolved, source = resolve_governing_plan_with_source(
        tmp_path,
        decisions={},
        handoff_governing_plan_field=None,
        consumed_handoff_deliverable_id="dr-129",
    )

    assert resolved is None
    assert source == "none"


def test_literal_string_null_deliverable_id_treated_as_absent(tmp_path):
    _write_plan(tmp_path, "some-plan", deliverable_id="dr-129")

    resolved, source = resolve_governing_plan_with_source(
        tmp_path,
        decisions={},
        handoff_governing_plan_field=None,
        consumed_handoff_deliverable_id="'null'",
    )

    assert resolved is None
    assert source == "none"


# ---------------------------------------------------------------------------
# An already-implemented plan must not produce a spurious/harmful directive
# beyond the normal claim+stamp pair -- `archive-stamp-cli stamp-plan-
# implemented` is documented as a no-op-by-construction on frozen statuses
# (`plan_status_transition._FROZEN_STATUSES`), so this module's own
# contract is only to resolve the plan and emit the same directive pair it
# always emits; it is not this module's job to special-case an already-
# terminal status (see `build_plan_claim_and_stamp_directives`'s own
# docstring: "fires unconditionally once the predicate holds").
# ---------------------------------------------------------------------------


def test_already_implemented_plan_still_resolves_and_emits_the_same_directive_pair(tmp_path):
    slug = "already-implemented-plan"
    _write_plan(tmp_path, slug, deliverable_id="dr-129", status="implemented")

    resolved, source = resolve_governing_plan_with_source(
        tmp_path,
        decisions={},
        handoff_governing_plan_field=None,
        consumed_handoff_deliverable_id="dr-129",
    )
    assert source == "deliverable_id_join"
    assert resolved.slug == slug

    directives = build_plan_claim_and_stamp_directives(resolved)
    ids = {d["id"] for d in directives}
    assert ids == {"d-claim-plan-execution-lock", "d-stamp-plan-implemented"}


# ---------------------------------------------------------------------------
# C8/AC17 -- d-attest-review-verified, emitted alongside d-stamp-plan-
# implemented off the same governing_plan_predicate gate, additionally
# conditioned on decisions["review"] being truthy this pass.
# ---------------------------------------------------------------------------


def test_review_verified_directive_emitted_alongside_stamp_when_review_present():
    from coordinator_core.workstream_complete.directives_lessons_plan import (
        GoverningPlan,
        build_review_verified_directive,
    )

    plan = GoverningPlan(slug="some-plan", path=Path("unused"))
    stamp_directives = build_plan_claim_and_stamp_directives(plan)
    attest_directives = build_review_verified_directive(
        plan, {"review": {"sha_range": "a..b", "reviewer": "code-reviewer", "scope": "chain"}}
    )

    stamp_ids = {d["id"] for d in stamp_directives}
    assert {"d-claim-plan-execution-lock", "d-stamp-plan-implemented"} <= stamp_ids
    assert {d["id"] for d in attest_directives} == {"d-attest-review-verified"}


def test_review_verified_directive_depends_on_the_claim_landing():
    from coordinator_core.workstream_complete.directives_lessons_plan import (
        GoverningPlan,
        build_review_verified_directive,
    )

    plan = GoverningPlan(slug="some-plan", path=Path("unused"))
    directives = build_review_verified_directive(plan, {"review": {"sha_range": "a..b"}})
    by_id = {d["id"]: d for d in directives}
    assert by_id["d-attest-review-verified"]["depends_on"] == "d-claim-plan-execution-lock"
    assert by_id["d-attest-review-verified"]["args"][0] == "stamp-review-verified"
    assert by_id["d-attest-review-verified"]["args"][1] == "docs/plans/some-plan.md"
    assert by_id["d-attest-review-verified"]["cli"] == "archive-stamp-cli"


def test_review_verified_directive_absent_when_no_review_happened():
    from coordinator_core.workstream_complete.directives_lessons_plan import (
        GoverningPlan,
        build_review_verified_directive,
    )

    plan = GoverningPlan(slug="some-plan", path=Path("unused"))
    assert build_review_verified_directive(plan, {}) == []
    assert build_review_verified_directive(plan, {"review": None}) == []
    assert build_review_verified_directive(plan, {"review": {}}) == []


def test_review_verified_directive_absent_without_a_governing_plan():
    from coordinator_core.workstream_complete.directives_lessons_plan import (
        build_review_verified_directive,
    )

    assert build_review_verified_directive(None, {"review": {"sha_range": "a..b"}}) == []


def test_build_governing_plan_directives_composes_the_attest_directive(tmp_path):
    from coordinator_core.workstream_complete.directives_lessons_plan import (
        build_governing_plan_directives,
    )

    slug = "composed-plan"
    _write_plan(tmp_path, slug)

    directives = build_governing_plan_directives(
        tmp_path,
        decisions={"governing_plan_slug": slug, "review": {"sha_range": "a..b"}},
    )
    ids = {d["id"] for d in directives}
    assert {"d-claim-plan-execution-lock", "d-stamp-plan-implemented", "d-attest-review-verified"} <= ids

    # No review this pass -- the claim/stamp pair still fires, the attest does not.
    directives_no_review = build_governing_plan_directives(
        tmp_path, decisions={"governing_plan_slug": slug}
    )
    ids_no_review = {d["id"] for d in directives_no_review}
    assert "d-attest-review-verified" not in ids_no_review
    assert "d-stamp-plan-implemented" in ids_no_review
