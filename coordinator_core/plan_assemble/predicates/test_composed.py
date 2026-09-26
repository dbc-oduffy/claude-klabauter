from __future__ import annotations

from coordinator_core.plan_assemble.predicates import composed, undetermined

U = undetermined("fixture: input not supplied")


def test_trivial_conjunction_true_when_both_arms_true():
    row = composed.trivial_conjunction(
        {"scope_file_count": 1, "scope_file_count_le_2": True},
        {"crossing_paths": [], "no_cross_repo_contract": True},
    )
    assert row is True


def test_trivial_conjunction_false_when_one_arm_false():
    row = composed.trivial_conjunction(
        {"scope_file_count": 5, "scope_file_count_le_2": False},
        {"crossing_paths": [], "no_cross_repo_contract": True},
    )
    assert row is False


def test_trivial_conjunction_undetermined_propagates():
    row = composed.trivial_conjunction(U, {"crossing_paths": [], "no_cross_repo_contract": True})
    assert row == {"undetermined": True, "reason": "one or more input arms undetermined"}


def test_trivial_conjunction_false_wins_over_undetermined():
    row = composed.trivial_conjunction(
        {"scope_file_count": 5, "scope_file_count_le_2": False}, U
    )
    assert row is False


def test_nontrivial_disjunction_true_when_one_arm_true():
    row = composed.nontrivial_disjunction(
        {"scope_file_count": 1, "scope_file_count_le_2": False},
        {"crossing_paths": [], "no_cross_repo_contract": False},
        {"candidate": True},
        {"mutates_shared_symbol": False, "consumer_count": 0},
        U,
    )
    assert row is True


def test_nontrivial_disjunction_undetermined_when_scaffold_row_unresolved_and_rest_false():
    row = composed.nontrivial_disjunction(
        {"scope_file_count": 5, "scope_file_count_le_2": False},
        {"crossing_paths": ["../peer"], "no_cross_repo_contract": False},
        {"candidate": False},
        {"mutates_shared_symbol": False, "consumer_count": 1},
        U,
    )
    assert row == {"undetermined": True, "reason": "one or more input arms undetermined"}


def test_nontrivial_disjunction_scaffold_row_presence_is_the_signal():
    row = composed.nontrivial_disjunction(
        {"scope_file_count": 5, "scope_file_count_le_2": False},
        {"crossing_paths": ["../peer"], "no_cross_repo_contract": False},
        {"candidate": False},
        {"mutates_shared_symbol": False, "consumer_count": 1},
        {"items_1_5": [True, True, True, True, True], "item_6_grep_present": True},
    )
    assert row is True


def test_nontrivial_disjunction_undetermined_when_no_arm_settles_true():
    row = composed.nontrivial_disjunction(
        U,
        {"crossing_paths": [], "no_cross_repo_contract": False},
        {"candidate": False},
        {"mutates_shared_symbol": False, "consumer_count": 1},
        U,
    )
    assert row == {"undetermined": True, "reason": "one or more input arms undetermined"}


def test_judgment_point_carries_three_criteria_and_null_recommendation():
    row = composed.architectural_tier_judgment_point(
        {"crossing_paths": ["../peer"], "no_cross_repo_contract": False},
        {"candidate": True, "matched_paths": ["state/foo.json"]},
        {"citation_present": True, "citation": "x.py:1", "registry_has_gate_type": True},
    )
    criteria_names = [c["criterion"] for c in row["candidate_criteria"]]
    assert criteria_names == [
        "cross-system-irreversible",
        "security-privacy-boundary",
        "naming-collision-with-product-policy",
    ]
    assert "multi-stakeholder" not in criteria_names
    assert row["recommendation"] is None
    assert row["id"] == "architectural-tier-criterion-classification"


def test_judgment_point_no_verdict_recommended_or_fires_field():
    row = composed.architectural_tier_judgment_point(
        {"crossing_paths": [], "no_cross_repo_contract": True},
        {"candidate": False, "matched_paths": []},
        {"citation_present": False, "citation": None, "registry_has_gate_type": False},
    )
    assert "verdict" not in row
    assert "recommended" not in row
    assert "fires" not in row
    for criterion in row["candidate_criteria"]:
        assert set(criterion.keys()) == {"criterion", "computed"}


def test_judgment_point_computed_values_propagate_undetermined_arms():
    row = composed.architectural_tier_judgment_point(U, U, U)
    for criterion in row["candidate_criteria"]:
        assert criterion["computed"] == {
            "undetermined": True,
            "reason": "fixture: input not supplied",
        }


def test_seven_dim_fix_locus_recombines_111_and_112():
    row = composed.seven_dim_fix_locus(
        {"citation_present": True, "citation": "a.py:10", "registry_has_gate_type": False}
    )
    assert row == {"citation_present": True, "registry_has_gate_type": False}


def test_seven_dim_fix_locus_propagates_undetermined():
    assert composed.seven_dim_fix_locus(U) == U


def test_seven_dim_fix_locus_preserves_nested_undetermined_field():
    row = composed.seven_dim_fix_locus(
        {"citation_present": True, "citation": "a.py:10", "registry_has_gate_type": U}
    )
    assert row["citation_present"] is True
    assert row["registry_has_gate_type"] == U


def test_seven_dim_all_green_true_when_all_four_arms_true():
    row = composed.seven_dim_all_green(
        {
            "no_duplicate": True,
            "no_fabrication": {"no_fabrication": True, "absent_citations": []},
            "official_docs_read": True,
            "reference_impl_seen": True,
        }
    )
    assert row is True


def test_seven_dim_all_green_false_when_one_arm_false():
    row = composed.seven_dim_all_green(
        {
            "no_duplicate": True,
            "no_fabrication": {"no_fabrication": False, "absent_citations": [{"line": 1, "token": "x"}]},
            "official_docs_read": True,
            "reference_impl_seen": True,
        }
    )
    assert row is False


def test_seven_dim_all_green_undetermined_propagates_never_false():
    row = composed.seven_dim_all_green(
        {
            "no_duplicate": U,
            "no_fabrication": {"no_fabrication": True, "absent_citations": []},
            "official_docs_read": True,
            "reference_impl_seen": True,
        }
    )
    assert row == {"undetermined": True, "reason": "one or more input arms undetermined"}
    assert row is not False


def test_seven_dim_all_green_no_fabrication_arm_reads_nested_field_not_the_whole_dict():
    row = composed.seven_dim_all_green(
        {
            "no_duplicate": True,
            "no_fabrication": {"no_fabrication": False, "absent_citations": [{"line": 3, "token": "y"}]},
            "official_docs_read": True,
            "reference_impl_seen": True,
        }
    )
    assert row is False


def test_seven_dim_all_green_false_wins_over_undetermined_sibling():
    row = composed.seven_dim_all_green(
        {
            "no_duplicate": False,
            "no_fabrication": U,
            "official_docs_read": True,
            "reference_impl_seen": True,
        }
    )
    assert row is False


def test_collapse_seven_dim_green_passes_through_true():
    assert composed.collapse_seven_dim_green(True) is True


def test_collapse_seven_dim_green_passes_through_undetermined():
    assert composed.collapse_seven_dim_green(U) == U


def test_collapse_premise_gate_green_m_band_is_undetermined_never_green():
    row = composed.collapse_premise_gate_green(
        {"m_band_uncovered": True, "tshirt": "M"}
    )
    assert row["undetermined"] is True
    assert row is not False
    assert row is not True


def test_collapse_premise_gate_green_non_m_band_resolves_true():
    row = composed.collapse_premise_gate_green(
        {"m_band_uncovered": False, "tshirt": "XL"}
    )
    assert row is True


def test_collapse_premise_gate_green_propagates_undetermined_input():
    row = composed.collapse_premise_gate_green(
        {"m_band_uncovered": U, "tshirt": None}
    )
    assert row == U


def test_collapse_premise_gate_green_undetermined_tshirt_propagates_never_computed():
    row = composed.collapse_premise_gate_green(
        {"m_band_uncovered": False, "tshirt": U}
    )
    assert row == U
    assert row is not True
    assert row is not False


def test_scope_mode_declared_true_when_value_present():
    assert composed.scope_mode_declared({"value": "spec-dispatch"}) is True


def test_scope_mode_declared_propagates_undetermined():
    assert composed.scope_mode_declared({"value": U}) == U


def test_route_triggers_review_true_for_plan_route():
    assert composed.route_triggers_review({"route": "plan"}) is True


def test_route_triggers_review_false_for_spec_dispatch_route():
    assert composed.route_triggers_review({"route": "spec-dispatch"}) is False


def test_route_triggers_review_propagates_undetermined():
    assert composed.route_triggers_review({"route": U}) == U


def test_terminal_table_result_plan_route():
    assert composed.terminal_table_result({"route": "plan"}) == {"result": "full_terminal"}


def test_terminal_table_result_spec_dispatch_route():
    assert composed.terminal_table_result({"route": "spec-dispatch"}) == {
        "result": "light_terminal"
    }


def test_terminal_table_result_unmapped_route_is_undetermined():
    row = composed.terminal_table_result({"route": "dispatch"})
    assert row["result"]["undetermined"] is True


def test_terminal_table_result_propagates_undetermined_route():
    row = composed.terminal_table_result({"route": U})
    assert row == {"result": U}
