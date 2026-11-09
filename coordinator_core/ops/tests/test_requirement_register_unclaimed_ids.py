"""unclaimed_ids: register-order ids outside claimed and exclude."""
from coordinator_core.ops.dispatch_emit import ask_contract
from coordinator_core.ops.requirement_register import spine_claimants, spine_unclaimed_ids, unclaimed_ids


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


def test_spine_claimants_maps_and_skips_non_claiming():
    spine = [
        {"id": "C1", "register_rows": ["R1", 2]},
        {"id": "C2", "register_rows": ["R1"], "disposition": "wont_do"},
        {"id": "C3", "register_rows": ["R3"], "disposition": "backlogged"},
        {"id": "C4", "register_rows": ["R4"], "disposition": "spun_off"},
        {"id": "C5", "register_rows": ["R5"], "disposition": "coded"},
        {"id": "C6"},
    ]
    assert spine_claimants(spine) == {"R1": ["C1"], "2": ["C1"], "R5": ["C5"]}


def test_spine_unclaimed_ids_d9a207_shape():
    register = [{"id": "R1"}, {"id": "R2"}, {"id": "R3"}]
    spine = [{"id": "C1", "register_rows": ["R1"]}, {"id": "C2", "register_rows": ["R3"], "disposition": "wont_do"}]
    assert spine_unclaimed_ids(register, spine) == ["R2", "R3"]
    assert spine_unclaimed_ids(register, spine, exclude=["R2"]) == ["R3"]
    assert spine_unclaimed_ids(register, []) == ["R1", "R2", "R3"]


def test_constants():
    assert ask_contract.UNCLAIMED_ROWS_HALT_KIND == "unclaimed-rows"
    assert ask_contract.REGISTER_ROWS_UNCLAIMED_REFUSAL == "REGISTER-ROWS-UNCLAIMED"
