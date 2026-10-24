"""account_targets: every parsed node maps to a row, a declined row, or a named drop."""

from coordinator_core.bug_blitz.accounting import (
    NOT_ADMITTED_REASON,
    NOT_SELECTED_REASON,
    account_targets,
)
from coordinator_core.bug_blitz.contract import FailingTest

PLAN_REASON = "route-to-plan: needs a plan"


def _t(file, name):
    return FailingTest(file=file, name=name)


def test_blitz_shape_five_nodes_four_ids_one_declined():
    tests = (
        _t("a/test_x.py", "t1"),
        _t("a/test_fleet_mode_reader_set.py", "t2"),
        _t("a/test_fleet_mode_reader_set.py", "t3"),
        _t("b/test_y.py", "t4"),
        _t("c/test_z.py", "t5"),
    )
    ids = ["x-row", "fleet-row", "y-row", "z-row"]
    declined = [{"row_id": "fleet-row", "path": "p", "reason": PLAN_REASON}]
    out = account_targets(tests, ids, ["x-row", "y-row", "z-row"], declined, [])
    assert len(out) == 5
    assert [a.disposition for a in out] == ["row", "declined", "declined", "row", "row"]
    assert out[1].reason == out[2].reason == PLAN_REASON
    assert out[1].row_id == "fleet-row"
    assert out[0].to_json()["nodeid"] == "a/test_x.py::t1"


def test_length_mismatch_drops_all_and_does_not_raise():
    tests = (_t("a.py", "t1"), _t("b.py", "t2"))
    out = account_targets(tests, ["only"], ["only"], [], [])
    assert [a.disposition for a in out] == ["dropped", "dropped"]
    assert all(a.reason == "mint returned 1 ids for 2 files" and a.row_id is None for a in out)


def test_unselected_id_is_dropped():
    out = account_targets((_t("a.py", "t1"),), ["r"], [], [], [])
    assert out[0].disposition == "dropped" and out[0].reason == NOT_SELECTED_REASON


def test_not_admitted_id_is_dropped():
    out = account_targets((_t("a.py", "t1"),), ["r"], ["r"], [], ["r"])
    assert out[0].disposition == "dropped" and out[0].reason == NOT_ADMITTED_REASON
    assert out[0].row_id == "r"


def test_row_disposition_for_selected_id():
    out = account_targets((_t("a.py", "t1"),), ["r"], ["r"], [], [])
    assert out[0].disposition == "row"
