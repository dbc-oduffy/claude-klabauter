"""Tests for `compute_layer_scaffold.check` — the Sub-shape B conformance
scorer.

Spec: docs/plans/2026-08-13-compute-layer-scaffolder.md, chunk C2 (AC6, AC7,
AC8, AC9). Spike evidence:
docs/research/spike-verdicts/2026-08-13-compute-layer-scaffolder-emits-conformant-producer.md.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from coordinator_core.ops.compute_layer_scaffold import check


def test_ac8_baseline_reproduced_exactly() -> None:
    """The measured baseline this session: closed dispatch 5/5,
    execute_directives 5/5, build_envelope 4/5 (pickup_assemble the miss),
    no-local-_emit 3/5 (pickup, baton the misses), extend_exit_codes 0/5,
    clean-on-all-five 0/5. A run disagreeing with this fails until
    reconciled (AC8) — this test IS that reconciliation gate."""
    report = check.score_fleet()

    assert report.clause_tally("closed_cli_dispatch") == (5, 5)
    assert report.clause_tally("execute_directives") == (5, 5)
    assert report.clause_tally("build_envelope") == (4, 5)
    assert report.clause_tally("no_local_emit") == (3, 5)
    assert report.clause_tally("extend_exit_codes") == (0, 5)
    assert report.clean_on_all() == (0, 5)


def test_ac8_pickup_assemble_is_the_build_envelope_miss() -> None:
    scores = {s.module: s for s in check.score_fleet().scores}
    assert scores["pickup_assemble"].build_envelope is False
    for name in ("baton_assemble", "backlog_grind_assemble", "merge_assemble", "consolidate_assemble"):
        assert scores[name].build_envelope is True


def test_ac8_pickup_and_baton_are_the_local_emit_misses() -> None:
    scores = {s.module: s for s in check.score_fleet().scores}
    assert scores["pickup_assemble"].no_local_emit is False
    assert scores["baton_assemble"].no_local_emit is False
    for name in ("backlog_grind_assemble", "merge_assemble", "consolidate_assemble"):
        assert scores[name].no_local_emit is True


def test_ac6_execute_directives_scores_5_of_5_via_module_qualified_reach() -> None:
    """Every one of the five producers reaches `execute_directives` through
    `from coordinator_core.contract import apply_base` then
    `apply_base.execute_directives(...)` — the module-qualified reach
    style, not a bare symbol import."""
    passed, total = check.score_fleet().clause_tally("execute_directives")
    assert (passed, total) == (5, 5)


def test_ac6_regression_oracle_symbol_only_matcher_scores_0_of_5() -> None:
    """Pins the exact bug this session's first scorer produced: an AST pass
    matching ONLY `ast.ImportFrom` bare-symbol names (never the
    module-qualified `apply_base.execute_directives` attribute-call style)
    reports the fleet at 0/5, though the true answer is 5/5. If this test
    ever also reports 5/5, the regression oracle has been silently
    defeated and no longer proves anything."""

    def naive_reaches_execute_directives(tree: ast.Module) -> bool:
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "coordinator_core.contract.apply_base":
                for alias in node.names:
                    if alias.name == "execute_directives":
                        return True
        return False

    naive_hits = 0
    for module_name in check.SUB_SHAPE_B:
        package_dir = REPO_ROOT / "coordinator_core" / module_name
        trees = [check._parse(p) for p in check._iter_source_files(package_dir)]
        if any(naive_reaches_execute_directives(t) for t in trees):
            naive_hits += 1

    assert naive_hits == 0

    correct_passed, correct_total = check.score_fleet().clause_tally("execute_directives")
    assert (correct_passed, correct_total) == (5, 5)


def test_ac7_extend_exit_codes_renders_as_a_distinct_fleet_finding() -> None:
    """AC7: the ladder clause must render as a FLEET FINDING, structurally
    distinct from a per-module conformance failure — not merely a
    differently-labelled row in the same table."""
    report = check.score_fleet()
    rendered = check.render_report(report)

    assert "FLEET FINDING" in rendered
    fleet_section = rendered.split("FLEET FINDING", 1)[1]
    assert "extend_exit_codes" in fleet_section

    conformance_section = rendered.split("FLEET FINDING", 1)[0]
    assert "extend_exit_codes" not in conformance_section

    passed, total = report.clause_tally(check.FLEET_FINDING_CLAUSE)
    assert (passed, total) == (0, 5)


def test_ac9_sub_shape_a_reports_na_never_a_failing_grade() -> None:
    for module_name in check.SUB_SHAPE_A:
        score = check.score_module(module_name)
        assert score.shape == "A"
        for clause in check.CLAUSES:
            assert score.clause(clause) is None
        assert score.clean() is None


def test_ac9_sub_shape_c_reports_na_never_a_failing_grade() -> None:
    for module_name in check.SUB_SHAPE_C:
        score = check.score_module(module_name)
        assert score.shape == "C"
        for clause in check.CLAUSES:
            assert score.clause(clause) is None
        assert score.clean() is None


def test_ac9_na_modules_never_widen_a_clause_denominator() -> None:
    """A fleet report spanning Sub-shape A/B/C keeps each clause's
    denominator at the Sub-shape B count only — N/A modules are excluded,
    never scored as failures."""
    all_modules = check.SUB_SHAPE_B + check.SUB_SHAPE_A + check.SUB_SHAPE_C
    report = check.score_fleet(modules=all_modules)

    for clause in check.CLAUSES:
        _, total = report.clause_tally(clause)
        assert total == 5

    clean, total = report.clean_on_all()
    assert total == 5
    assert clean == 0


def test_build_envelope_clause_tolerates_a_legitimate_extra_key() -> None:
    """The `build_envelope` clause scores whether the reach happened at all
    (AC6's two reach styles), never the shape of the emitted dict — so a
    call site carrying a legitimate 9th key (as `review_assemble`'s
    `segments`) still scores conformant. Pins that this stays "all required
    keys present", never "exactly the 8 `ENVELOPE_KEYS`" — the coordinator-claude schema of
    record has no `additionalProperties: false`, so a 9-key envelope is
    valid and this clause must not regress into a per-module waiver later.
    """
    source = (
        "from coordinator_core.contract.decision_object import envelope\n"
        "def brief():\n"
        "    return envelope.build_envelope(\n"
        "        preflight={}, gates={}, directives=[], judgment_points=[],\n"
        "        decisions={}, narration=[], next_move=None, segments=[],\n"
        "    )\n"
    )
    tree = ast.parse(source, filename="<synthetic-nine-key-envelope>")

    assert check._reaches_symbol(tree, check.BUILD_ENVELOPE) is True


def test_score_module_unregistered_module_raises() -> None:
    try:
        check.score_module("not_a_registered_module")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for an unregistered module")
