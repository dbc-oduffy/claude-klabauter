"""A halt gap line that is a pytest node id closes when the run's reverify record shows it passing; replay of DoE run aee30b."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.ask_contract import HALT_GAP_UNCLOSED_REASON
from coordinator_core.ops.dispatch_emit.halt_gap_reverify_closure import closed_by_reverify, node_id_of
from coordinator_core.ops.dispatch_emit.land_run import _unclosed_halt_gaps

NODE = "coordinator/tests/test_sizing_objects_validate_against_schema.py::test_no_post_bump_sizing_object_carries_unrecorded_premise"
LINE = f"{NODE}: AssertionError: shrink-only baseline lists records that no longer carry unrecorded premise: 2026-09-23-doctrine-gate-baseline"
RECORD = {
    "edited": [],
    "failing": [],
    "reverted": [{"path": "coordinator/tests/test_plan_blitz_standing_readiness.py", "tests": [NODE]}],
    "kept": [{"path": "coordinator/workflows/plan-blitz.mjs", "tests": ["coordinator/tests/test_x.py::test_y"]}],
    "pre_existing": ["coordinator/tests/test_oss_payload_locality.py::test_no_file_exceeds_its_locality_budget"],
    "unattributed": [],
    "status_before": "fail",
    "status_after": "pass",
}
RESUME = {"blocking": ["R1"]}
VERDICT = {"items": [{"id": "R1", "status": "met"}]}
GAPS = {"R1": {"remaining": [LINE]}}


def test_node_id_parses_with_message_params_and_backslashes():
    assert node_id_of(LINE) == NODE
    assert node_id_of("a/b.py::test_c[x y]: boom") == "a/b.py::test_c[x y]"
    assert node_id_of("a\\b.py::K::test_c") == "a/b.py::K::test_c"
    assert node_id_of("verify-stage prompts do not carry the clause") is None
    assert node_id_of(None) is None


def test_listed_passing_closes_the_line():
    assert closed_by_reverify(LINE, RECORD) is True
    assert _unclosed_halt_gaps(RESUME, VERDICT, GAPS, RECORD) == []


def test_missing_or_unparseable_record_does_not_close():
    for record in (None, [], "x", {}):
        assert closed_by_reverify(LINE, record) is False
        failures = _unclosed_halt_gaps(RESUME, VERDICT, GAPS, record)
        assert failures and failures[0]["reason"].startswith(HALT_GAP_UNCLOSED_REASON)
    assert _unclosed_halt_gaps(RESUME, VERDICT, GAPS)


def test_failing_pre_existing_unattributed_or_outside_run_set_do_not_close():
    for key in ("failing", "pre_existing", "unattributed"):
        assert closed_by_reverify(LINE, {**RECORD, key: [NODE]}) is False
    assert closed_by_reverify(LINE, {**RECORD, "reverted": [], "kept": []}) is False


def test_errored_or_indeterminate_record_does_not_close():
    assert closed_by_reverify(LINE, {**RECORD, "reverify_error": "boom"}) is False
    assert closed_by_reverify(LINE, {**RECORD, "status_after": None, "status_before": "indeterminate"}) is False


def test_non_node_gap_line_still_needs_gaps_closed():
    gaps = {"R1": {"remaining": ["some prose gap"]}}
    assert _unclosed_halt_gaps(RESUME, VERDICT, gaps, RECORD)
