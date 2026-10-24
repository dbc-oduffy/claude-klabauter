"""unclaimed_ids: register-order ids outside claimed and exclude."""
from coordinator_core.ops.dispatch_emit import ask_contract
from coordinator_core.ops.requirement_register import unclaimed_ids


def test_order_and_claimed():
    rows = [{"id": "R1"}, {"id": "R2"}, {"id": "R3"}]
    assert unclaimed_ids(rows, ["R2"]) == ["R1", "R3"]


def test_str_int_ids():
    rows = [{"id": 1}, {"id": "2"}, {"id": 3}]
    assert unclaimed_ids(rows, ["1", 2]) == ["3"]


def test_exclude():
    rows = [{"id": "R1"}, {"id": "R2"}, {"id": "R3"}]
    assert unclaimed_ids(rows, ["R1"], exclude=["R3"]) == ["R2"]
    assert unclaimed_ids(rows, [], exclude=("R1", "R2", "R3")) == []


def test_met_rows_count_and_idless_skipped():
    rows = [{"id": "R1", "status": "met", "wired": True}, {"status": "open"}, {"id": None}]
    assert unclaimed_ids(rows, []) == ["R1"]


def test_empty_register():
    assert unclaimed_ids([], ["R1"]) == []


def test_constants():
    assert ask_contract.UNCLAIMED_ROWS_HALT_KIND == "unclaimed-rows"
    assert ask_contract.REGISTER_ROWS_UNCLAIMED_REFUSAL == "REGISTER-ROWS-UNCLAIMED"
