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


def test_file_level_target_nodeid_is_the_bare_file():
    out = account_targets((_t("a/test_x.py", ""), _t("a/test_y.py", "t1")), ["x-row", "y-row"], ["x-row", "y-row"], [], [])
    assert [a.to_json()["nodeid"] for a in out] == ["a/test_x.py", "a/test_y.py::t1"]


def test_short_mint_keeps_paired_nodes_and_names_the_unpaired_drop():
    tests = (_t("a.py", "t1"), _t("a.py", "t2"), _t("b.py", "t3"))
    out = account_targets(tests, ["only"], ["only"], [], [])
    assert [a.disposition for a in out] == ["row", "row", "dropped"]
    assert [a.row_id for a in out] == ["only", "only", None]
    assert out[2].reason == "mint returned 1 ids for 2 files"


def test_empty_mint_drops_every_node_by_name_and_never_raises():
    out = account_targets((_t("a.py", "t1"), _t("b.py", "t2")), [], [], [], [])
    assert [a.disposition for a in out] == ["dropped", "dropped"]
    assert all(a.reason == "mint returned 0 ids for 2 files" for a in out)


def test_every_node_of_a_shared_file_is_its_own_account():
    tests = tuple(_t("a.py", f"t{i}") for i in range(4))
    out = account_targets(tests, ["r"], ["r"], [], [])
    assert [a.nodeid for a in out] == [f"a.py::t{i}" for i in range(4)]


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
