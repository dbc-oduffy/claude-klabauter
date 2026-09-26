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


def test_directive_gate_open_list_form_does_not_raise() -> None:
    directive = {"id": "d1", "depends_on": ["jp1"]}
    jp_by_id = {"jp1": _JP_TWO_WAY}
    decisions = {"jp1": {"disposition": "yes"}}
    assert apply_halt._directive_gate_open(directive, jp_by_id, decisions) is True


def test_directive_gate_open_list_form_all_members_must_be_satisfied() -> None:
    directive = {"id": "d1", "depends_on": ["jp1", "jp2"]}
    jp_by_id = {"jp1": _JP_TWO_WAY, "jp2": _JP_EMPTY_RESOLVES}
    decisions = {"jp1": {"disposition": "yes"}, "jp2": {"disposition": "yes"}}
    assert apply_halt._directive_gate_open(directive, jp_by_id, decisions) is False


def test_directive_gate_open_list_form_empty_list_is_a_no_op_and_fires() -> None:
    assert apply_halt._directive_gate_open({"id": "d1", "depends_on": []}, {}, {}) is True


def test_directive_gate_open_directive_id_dependency_does_not_gate() -> None:
    directive = {"id": "d2", "depends_on": "d1"}
    assert apply_halt._directive_gate_open(directive, {}, {}, frozenset({"d1", "d2"})) is True


def test_directive_gate_open_directive_id_dependency_ignores_producer_outcome() -> None:
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


def test_assert_disjoint_dependency_namespaces_passes_when_disjoint() -> None:
    apply_halt.assert_disjoint_dependency_namespaces({"jp1": _JP_TWO_WAY}, {"d1", "d2"})


def test_assert_disjoint_dependency_namespaces_raises_on_overlap() -> None:
    import pytest

    with pytest.raises(AssertionError):
        apply_halt.assert_disjoint_dependency_namespaces({"d1": _JP_TWO_WAY}, {"d1", "d2"})


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

    jp_directive = {"id": "d1", "depends_on": "jp1"}
    assert apply_halt._directive_gate_open(jp_directive, jp_by_id, {}, directive_ids) is False
    assert apply_base.directive_gate_open(jp_directive, jp_by_id, {})[0] is False

    decisions = {"jp1": {"disposition": "yes"}}
    assert apply_halt._directive_gate_open(jp_directive, jp_by_id, decisions, directive_ids) is True
    assert apply_base.directive_gate_open(jp_directive, jp_by_id, decisions)[0] is True

    directive_id_dep = {"id": "d2", "depends_on": "d1"}
    assert apply_halt._directive_gate_open(directive_id_dep, jp_by_id, {}, directive_ids) is True
    assert apply_base.directive_gate_open(directive_id_dep, jp_by_id, {})[0] is True

    unknown_dep = {"id": "d1", "depends_on": "ghost"}
    assert apply_halt._directive_gate_open(unknown_dep, jp_by_id, {}, directive_ids) is False
    assert apply_base.directive_gate_open(unknown_dep, jp_by_id, {})[0] is True


def test_unrecognized_directive_is_a_runtime_error() -> None:
    assert issubclass(apply_halt.UnrecognizedDirective, RuntimeError)


def test_unrecognized_directive_carries_its_message() -> None:
    exc = apply_halt.UnrecognizedDirective("unknown-cli")
    assert str(exc) == "unknown-cli"


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
    assert apply_halt.CEREMONY_HALT_EXIT_CODES == {
        "HALTED_AT_JUDGMENT": 1,
        "DIRECTIVE_FAILED": 2,
        "TRANSPORT_FAIL": 3,
        "PARTIAL_MUTATION": 4,
    }


#: every row of `_CEREMONY_APPLY_MODULES` names a whole importable module, never a symbol
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


def test_every_ceremony_apply_module_threads_exit_code_label(
) -> None:
    """Widened per docs/plans/2026-09-11-half-the-compositions-do-not-finish-
    clea.md chunk C2: a ninth (here, fourth) ceremony call site added later
    without threading `exit_code_label` fails this pin rather than shipping
    an unlabelled flush."""
    for dotted_name in _CEREMONY_APPLY_MODULES:
        source = inspect.getsource(_import_ceremony_apply_module(dotted_name))
        assert "exit_code_label(" in source, (
            f"{dotted_name} never calls apply_halt.exit_code_label"
        )
        assert "flush_composition_record(" in source and "exit_code_label=" in source, (
            f"{dotted_name} must pass exit_code_label= on its "
            "flush_composition_record call"
        )


def test_every_ceremony_apply_module_constructs_its_own_budget_via_the_factory() -> None:
    for dotted_name in _CEREMONY_APPLY_MODULES:
        source = inspect.getsource(_import_ceremony_apply_module(dotted_name))
        assert "make_fleet_budget(" in source, (
            f"{dotted_name} never calls telemetry.composition_record.make_fleet_budget"
        )


class TestExitCodeLabel:
    """AC2: the failure-path oracle for the ceremony ladder's own
    `exit_code_label`. Every ladder constant maps to its own name -- the
    ceremony case includes SUCCESS = 0, which CEREMONY_HALT_EXIT_CODES does
    not hold; DIRECTIVE_FAILED plus a `budget_breach` report key maps to
    BUDGET_BREACH; DIRECTIVE_FAILED without the key keeps its ladder name;
    the post-loop breach (SUCCESS/HALTED_AT_JUDGMENT with the key) keeps its
    ladder name; an unrecognized int returns None."""

    def test_success_maps_to_its_own_name(self) -> None:
        assert apply_halt.exit_code_label(0) == "SUCCESS"

    def test_halted_at_judgment_maps_to_its_own_name(self) -> None:
        assert (
            apply_halt.exit_code_label(apply_halt.CEREMONY_HALT_EXIT_CODES["HALTED_AT_JUDGMENT"])
            == "HALTED_AT_JUDGMENT"
        )

    def test_directive_failed_without_budget_breach_key_keeps_its_ladder_name(self) -> None:
        assert (
            apply_halt.exit_code_label(
                apply_halt.CEREMONY_HALT_EXIT_CODES["DIRECTIVE_FAILED"], {"error": "x"}
            )
            == "DIRECTIVE_FAILED"
        )

    def test_directive_failed_with_no_report_keeps_its_ladder_name(self) -> None:
        assert (
            apply_halt.exit_code_label(apply_halt.CEREMONY_HALT_EXIT_CODES["DIRECTIVE_FAILED"])
            == "DIRECTIVE_FAILED"
        )

    def test_directive_failed_with_budget_breach_key_labels_budget_breach(self) -> None:
        assert (
            apply_halt.exit_code_label(
                apply_halt.CEREMONY_HALT_EXIT_CODES["DIRECTIVE_FAILED"],
                {"budget_breach": "over budget"},
            )
            == "BUDGET_BREACH"
        )

    def test_transport_fail_maps_to_its_own_name(self) -> None:
        assert (
            apply_halt.exit_code_label(apply_halt.CEREMONY_HALT_EXIT_CODES["TRANSPORT_FAIL"])
            == "TRANSPORT_FAIL"
        )

    def test_partial_mutation_maps_to_its_own_name(self) -> None:
        assert (
            apply_halt.exit_code_label(apply_halt.CEREMONY_HALT_EXIT_CODES["PARTIAL_MUTATION"])
            == "PARTIAL_MUTATION"
        )

    def test_success_with_budget_breach_key_keeps_ladder_name_not_budget_breach(self) -> None:
        assert apply_halt.exit_code_label(0, {"budget_breach": "over budget"}) == "SUCCESS"

    def test_halted_at_judgment_with_budget_breach_key_keeps_ladder_name(self) -> None:
        assert (
            apply_halt.exit_code_label(
                apply_halt.CEREMONY_HALT_EXIT_CODES["HALTED_AT_JUDGMENT"],
                {"budget_breach": "over budget"},
            )
            == "HALTED_AT_JUDGMENT"
        )

    def test_unrecognized_int_returns_none(self) -> None:
        assert apply_halt.exit_code_label(99) is None
