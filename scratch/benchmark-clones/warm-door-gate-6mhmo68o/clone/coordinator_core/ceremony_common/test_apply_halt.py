"""Tests for coordinator_core.ceremony_common.apply_halt — the ceremony
family's shared halt contract (value-aware judgment-point gate,
unrecognized-directive signal, exit-code ladder), extracted from the
byte-identical copies `workday_complete/apply.py` and
`workweek_complete/apply.py` carried before this extraction.

Mirrors the existing workday/workweek halt-contract test shapes (this
plan's own halt-trio unit tests live inline in each of those modules' apply
suites) and the value-aware-predicate coverage in
`coordinator_core/contract/test_apply_base.py`'s
`disposition_resolves_directive`/`directive_gate_open` section — adjusted
for this module's own two-arg `(judgment_point, chosen_value, directive_id)`
signature, which is NOT `apply_base`'s `(judgment_point, decisions,
directive_id)` shape (see this module's own Negative-spec: no
`apply_base` composition).

Spec backlink: docs/plans/2026-07-26-workstream-complete-computed-frontage.md, chunk C2g
"""

from __future__ import annotations

import enum
import inspect

from coordinator_core.ceremony_common import apply_halt

_JP_TWO_WAY: dict = {
    "id": "jp1",
    "dispositions": [
        {"value": "yes", "resolves": ["d1"]},
        {"value": "no", "resolves": ["d2"]},
    ],
}

_JP_EMPTY_RESOLVES: dict = {
    "id": "jp2",
    "dispositions": [{"value": "yes", "resolves": []}],
}


# ---------------------------------------------------------------------------
# _disposition_resolves_directive
# ---------------------------------------------------------------------------


def test_disposition_resolves_directive_true_when_value_and_resolves_match() -> None:
    assert apply_halt._disposition_resolves_directive(_JP_TWO_WAY, "yes", "d1") is True


def test_disposition_resolves_directive_false_for_a_different_directive_id() -> None:
    assert apply_halt._disposition_resolves_directive(_JP_TWO_WAY, "yes", "d2") is False


def test_disposition_resolves_directive_false_for_unrecognized_value() -> None:
    assert apply_halt._disposition_resolves_directive(_JP_TWO_WAY, "maybe", "d1") is False


def test_disposition_resolves_directive_false_when_resolves_list_is_empty() -> None:
    assert apply_halt._disposition_resolves_directive(_JP_EMPTY_RESOLVES, "yes", "d1") is False


def test_disposition_resolves_directive_never_mutates_the_judgment_point() -> None:
    before = {k: list(v) if isinstance(v, list) else v for k, v in _JP_TWO_WAY.items()}
    apply_halt._disposition_resolves_directive(_JP_TWO_WAY, "yes", "d1")
    assert _JP_TWO_WAY == before or _JP_TWO_WAY["dispositions"] == before["dispositions"]


# ---------------------------------------------------------------------------
# _directive_gate_open
# ---------------------------------------------------------------------------


def test_directive_gate_open_no_depends_on_always_fires() -> None:
    assert apply_halt._directive_gate_open({"id": "d1"}, {}, {}) is True


def test_directive_gate_open_unresolved_dependency_blocks() -> None:
    directive = {"id": "d1", "depends_on": "jp1"}
    jp_by_id = {"jp1": _JP_TWO_WAY}
    assert apply_halt._directive_gate_open(directive, jp_by_id, {}) is False


def test_directive_gate_open_resolved_dependency_opens_the_gate() -> None:
    directive = {"id": "d1", "depends_on": "jp1"}
    jp_by_id = {"jp1": _JP_TWO_WAY}
    decisions = {"jp1": {"disposition": "yes"}}
    assert apply_halt._directive_gate_open(directive, jp_by_id, decisions) is True


def test_directive_gate_open_resolved_but_wrong_directive_stays_closed() -> None:
    directive = {"id": "d2", "depends_on": "jp1"}
    jp_by_id = {"jp1": _JP_TWO_WAY}
    decisions = {"jp1": {"disposition": "yes"}}
    assert apply_halt._directive_gate_open(directive, jp_by_id, decisions) is False


def test_directive_gate_open_depends_on_naming_an_absent_judgment_point_stays_closed() -> None:
    directive = {"id": "d1", "depends_on": "jp-missing"}
    assert apply_halt._directive_gate_open(directive, {}, {}) is False


def test_directive_gate_open_decision_present_but_not_a_mapping_stays_closed() -> None:
    directive = {"id": "d1", "depends_on": "jp1"}
    jp_by_id = {"jp1": _JP_TWO_WAY}
    decisions = {"jp1": "yes"}
    assert apply_halt._directive_gate_open(directive, jp_by_id, decisions) is False


def test_directive_gate_open_decision_with_no_disposition_stays_closed() -> None:
    directive = {"id": "d1", "depends_on": "jp1"}
    jp_by_id = {"jp1": _JP_TWO_WAY}
    decisions = {"jp1": {}}
    assert apply_halt._directive_gate_open(directive, jp_by_id, decisions) is False


# ---------------------------------------------------------------------------
# _directive_gate_open — list-form depends_on (Finding 3: TypeError regression)
# ---------------------------------------------------------------------------


def test_directive_gate_open_list_form_does_not_raise() -> None:
    directive = {"id": "d1", "depends_on": ["jp1"]}
    jp_by_id = {"jp1": _JP_TWO_WAY}
    decisions = {"jp1": {"disposition": "yes"}}
    assert apply_halt._directive_gate_open(directive, jp_by_id, decisions) is True


def test_directive_gate_open_list_form_all_members_must_be_satisfied() -> None:
    directive = {"id": "d1", "depends_on": ["jp1", "jp2"]}
    jp_by_id = {"jp1": _JP_TWO_WAY, "jp2": _JP_EMPTY_RESOLVES}
    decisions = {"jp1": {"disposition": "yes"}, "jp2": {"disposition": "yes"}}
    # jp2's "yes" disposition resolves nothing, so the list-form AND fails.
    assert apply_halt._directive_gate_open(directive, jp_by_id, decisions) is False


def test_directive_gate_open_list_form_empty_list_is_a_no_op_and_fires() -> None:
    assert apply_halt._directive_gate_open({"id": "d1", "depends_on": []}, {}, {}) is True


# ---------------------------------------------------------------------------
# _directive_gate_open — directive-id depends_on (Finding 1/2: union namespace)
# ---------------------------------------------------------------------------


def test_directive_gate_open_directive_id_dependency_does_not_gate() -> None:
    directive = {"id": "d2", "depends_on": "d1"}
    # "d1" names no judgment point at all — only a sibling directive id.
    assert apply_halt._directive_gate_open(directive, {}, {}, frozenset({"d1", "d2"})) is True


def test_directive_gate_open_directive_id_dependency_ignores_producer_outcome() -> None:
    # Even though nothing in `decisions` marks "d1" as landed/failed, a
    # directive-id dependency never gates — producer readiness is the
    # caller's arg-token-resolution concern, not this gate's (Finding 2).
    directive = {"id": "d2", "depends_on": "d1"}
    decisions = {"d1": {"disposition": "irrelevant"}}
    assert apply_halt._directive_gate_open(directive, {}, decisions, frozenset({"d1", "d2"})) is True


def test_directive_gate_open_unknown_in_both_namespaces_fails_closed() -> None:
    directive = {"id": "d2", "depends_on": "ghost"}
    assert apply_halt._directive_gate_open(directive, {}, {}, frozenset({"d1", "d2"})) is False


def test_directive_gate_open_mixed_list_form_jp_and_directive_id() -> None:
    directive = {"id": "d2", "depends_on": ["jp1", "d1"]}
    jp_by_id = {"jp1": _JP_TWO_WAY}
    decisions = {"jp1": {"disposition": "no"}}
    assert (
        apply_halt._directive_gate_open(directive, jp_by_id, decisions, frozenset({"d1", "d2"}))
        is True
    )


# ---------------------------------------------------------------------------
# assert_disjoint_dependency_namespaces
# ---------------------------------------------------------------------------


def test_assert_disjoint_dependency_namespaces_passes_when_disjoint() -> None:
    apply_halt.assert_disjoint_dependency_namespaces({"jp1": _JP_TWO_WAY}, {"d1", "d2"})


def test_assert_disjoint_dependency_namespaces_raises_on_overlap() -> None:
    import pytest

    with pytest.raises(AssertionError):
        apply_halt.assert_disjoint_dependency_namespaces({"d1": _JP_TWO_WAY}, {"d1", "d2"})


# ---------------------------------------------------------------------------
# Cross-lineage contract test — apply_halt vs. apply_base agreement/divergence
# ---------------------------------------------------------------------------


def test_cross_lineage_agreement_and_divergence() -> None:
    """The same synthetic envelope fed to both lineages' gates. Regression
    guard for Finding 5 (staff-eng review, 2026-07-27): this field has
    already drifted twice unnoticed — fail-open vs. fail-closed on an
    unknown id, and list-form support — because nothing asserted the two
    readings' relationship. Asserts: agreement on judgment-point ids,
    agreement on directive ids (neither gates), and a DELIBERATE,
    documented divergence on unknown ids (apply_halt closed, apply_base
    open)."""
    from coordinator_core.contract import apply_base

    jp_by_id = {"jp1": _JP_TWO_WAY}
    directive_ids = frozenset({"d1", "d2"})

    # Agreement: unresolved judgment-point id blocks in both.
    jp_directive = {"id": "d1", "depends_on": "jp1"}
    assert apply_halt._directive_gate_open(jp_directive, jp_by_id, {}, directive_ids) is False
    assert apply_base.directive_gate_open(jp_directive, jp_by_id, {})[0] is False

    # Agreement: resolved judgment-point id opens the gate in both.
    decisions = {"jp1": {"disposition": "yes"}}
    assert apply_halt._directive_gate_open(jp_directive, jp_by_id, decisions, directive_ids) is True
    assert apply_base.directive_gate_open(jp_directive, jp_by_id, decisions)[0] is True

    # Agreement: a directive-id dependency never gates, in either lineage.
    directive_id_dep = {"id": "d2", "depends_on": "d1"}
    assert apply_halt._directive_gate_open(directive_id_dep, jp_by_id, {}, directive_ids) is True
    assert apply_base.directive_gate_open(directive_id_dep, jp_by_id, {})[0] is True

    # Deliberate divergence: an id in NEITHER namespace. apply_halt fails
    # CLOSED (stricter — a typo'd id stays a stuck directive, not a
    # silent auto-fire). apply_base fails OPEN (ignores the unrecognized
    # dep entirely). Both behaviors are intentional and this asserts the
    # SHAPE of the divergence, not a bug to reconcile.
    unknown_dep = {"id": "d1", "depends_on": "ghost"}
    assert apply_halt._directive_gate_open(unknown_dep, jp_by_id, {}, directive_ids) is False
    assert apply_base.directive_gate_open(unknown_dep, jp_by_id, {})[0] is True


# ---------------------------------------------------------------------------
# UnrecognizedDirective
# ---------------------------------------------------------------------------


def test_unrecognized_directive_is_a_runtime_error() -> None:
    assert issubclass(apply_halt.UnrecognizedDirective, RuntimeError)


def test_unrecognized_directive_carries_its_message() -> None:
    exc = apply_halt.UnrecognizedDirective("unknown-cli")
    assert str(exc) == "unknown-cli"


# ---------------------------------------------------------------------------
# build_ceremony_halt_exit_codes
# ---------------------------------------------------------------------------


def test_build_ceremony_halt_exit_codes_anchors_success_at_zero() -> None:
    ExitCode = apply_halt.build_ceremony_halt_exit_codes("SomeCeremonyExitCode")
    assert int(ExitCode.SUCCESS) == 0


def test_build_ceremony_halt_exit_codes_matches_the_shared_ladder() -> None:
    ExitCode = apply_halt.build_ceremony_halt_exit_codes("SomeCeremonyExitCode")
    assert int(ExitCode.HALTED_AT_JUDGMENT) == 1
    assert int(ExitCode.DIRECTIVE_FAILED) == 2
    assert int(ExitCode.TRANSPORT_FAIL) == 3
    assert int(ExitCode.PARTIAL_MUTATION) == 4


def test_build_ceremony_halt_exit_codes_uses_the_caller_supplied_name() -> None:
    ExitCode = apply_halt.build_ceremony_halt_exit_codes("MyCeremonyExitCode")
    assert ExitCode.__name__ == "MyCeremonyExitCode"


def test_build_ceremony_halt_exit_codes_is_a_distinct_int_enum_per_call() -> None:
    FirstExitCode = apply_halt.build_ceremony_halt_exit_codes("FirstExitCode")
    SecondExitCode = apply_halt.build_ceremony_halt_exit_codes("SecondExitCode")
    assert FirstExitCode is not SecondExitCode
    assert issubclass(FirstExitCode, enum.IntEnum)


def test_build_ceremony_halt_exit_codes_matches_workday_and_workweeks_literal_ladder() -> None:
    # workday_complete.apply.WorkdayApplyExitCode / workweek_complete.apply.
    # WorkweekApplyExitCode both hand-typed this exact ladder before C2h
    # repoints them onto this factored builder — pinning the literal values
    # here guards against silent drift in the shared constant.
    assert apply_halt.CEREMONY_HALT_EXIT_CODES == {
        "HALTED_AT_JUDGMENT": 1,
        "DIRECTIVE_FAILED": 2,
        "TRANSPORT_FAIL": 3,
        "PARTIAL_MUTATION": 4,
    }


# ---------------------------------------------------------------------------
# Composition-budget wiring (chunk C3, docs/plans/2026-08-18-arm-the-
# composition-budget.md): a structural, source-level assertion that every
# ceremony consumer actually calls all three `apply_halt` budget primitives
# AND flushes its composition record — not a behavioral test of the budget
# arithmetic itself (that lives in `tests/test_composition_budget_
# boundaries.py`, which stands the primitives in for a caller's own loop by
# its own admission — this file is the one place that pins the ceremony
# family's three hand-rolled loops actually wire them in). Mirrors C2's own
# structural-coverage rationale for its five `*_assemble/apply.py` modules
# (grep-shaped, not AST-shaped, per this repo's established source-
# inspection test idiom — e.g. `test_no_unbatched_per_item_git_spawn.py`).
# ---------------------------------------------------------------------------

#: Declaration for the register-aging sweep (C5,
#: `docs/plans/2026-08-26-every-register-either-derives-or-fails-on-its-dead-rows.md`):
#: every row of `_CEREMONY_APPLY_MODULES` names a whole importable module, never a symbol
#: living inside a parent module.
_CEREMONY_APPLY_MODULES__SUBJECT_CLASS = "module"

_CEREMONY_APPLY_MODULES = (
    "coordinator_core.workday_complete.apply",
    "coordinator_core.workweek_complete.apply",
    "coordinator_core.workstream_complete.apply",
)


def _import_ceremony_apply_module(dotted_name: str):
    import importlib

    return importlib.import_module(dotted_name)


def test_every_ceremony_apply_module_calls_the_pre_mutation_boundary() -> None:
    for dotted_name in _CEREMONY_APPLY_MODULES:
        source = inspect.getsource(_import_ceremony_apply_module(dotted_name))
        assert "budget_check_pre_mutation(" in source, (
            f"{dotted_name} never calls apply_halt.budget_check_pre_mutation"
        )


def test_every_ceremony_apply_module_calls_the_post_mutation_boundary() -> None:
    for dotted_name in _CEREMONY_APPLY_MODULES:
        source = inspect.getsource(_import_ceremony_apply_module(dotted_name))
        assert "budget_check_post_mutation(" in source, (
            f"{dotted_name} never calls apply_halt.budget_check_post_mutation"
        )


def test_every_ceremony_apply_module_calls_the_mid_directive_advisory() -> None:
    for dotted_name in _CEREMONY_APPLY_MODULES:
        source = inspect.getsource(_import_ceremony_apply_module(dotted_name))
        assert "budget_advisory_mid_directive(" in source, (
            f"{dotted_name} never calls apply_halt.budget_advisory_mid_directive"
        )


def test_every_ceremony_apply_module_flushes_its_composition_record() -> None:
    for dotted_name in _CEREMONY_APPLY_MODULES:
        source = inspect.getsource(_import_ceremony_apply_module(dotted_name))
        assert "flush_composition_record(" in source, (
            f"{dotted_name} never calls telemetry.composition_record.flush_composition_record"
        )
        assert "finally:" in source, (
            f"{dotted_name} must flush from a try/finally, not a bare success-path call"
        )


def test_every_ceremony_apply_module_constructs_its_own_budget_via_the_factory() -> None:
    for dotted_name in _CEREMONY_APPLY_MODULES:
        source = inspect.getsource(_import_ceremony_apply_module(dotted_name))
        assert "make_fleet_budget(" in source, (
            f"{dotted_name} never calls telemetry.composition_record.make_fleet_budget"
        )
