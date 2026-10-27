"""shared_path_landing: sibling-held product join, withheld paths, refusal text, contract keys."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import land_run_contract, peer_claim_contract, shared_path_landing as spl

UNLANDED = [
    ("C2", "a/x.py", "peer-held"),
    ("C1", "a/x.py", "peer-held"),
    ("C1", "b/y.py", "never-commit"),
    ("C3", "c/z.py", "peer-held"),
]


def test_sibling_held_product_keeps_only_peer_held_sorted_with_holders():
    held = spl.sibling_held_product(UNLANDED, {"a/x.py": ["s2", "s1"], "c/z.py": ["s3"]})
    assert held == [("C1", "a/x.py", ["s1", "s2"]), ("C2", "a/x.py", ["s1", "s2"]), ("C3", "c/z.py", ["s3"])]


def test_sibling_held_product_unknown_path_has_empty_holders_and_malformed_triple_raises():
    assert spl.sibling_held_product([("C1", "q", "peer-held")], {}) == [("C1", "q", [])]
    assert spl.sibling_held_product(None, None) == []
    with pytest.raises(ValueError):
        spl.sibling_held_product([("bad",)], {})


def test_withheld_product_paths_all_reasons_unique_sorted():
    assert spl.withheld_product_paths(UNLANDED) == ["a/x.py", "b/y.py", "c/z.py"]
    assert spl.withheld_product_paths([]) == []
    assert spl.withheld_product_paths(None) == []


def test_refusal_message_names_path_row_holder_and_both_routes(tmp_path):
    msg = spl.refusal_message([("C1", "a/x.py", ["s1"])], "ask-1", root=tmp_path)
    assert msg.startswith(spl.SHARED_PATH_SIBLING_HELD)
    assert "a/x.py (row C1) held by s1" in msg
    assert "Wait for the holder to land, then re-fire." in msg
    assert peer_claim_contract.WITHHOLD_CALL.format(run_id="ask-1") in msg


def test_refusal_message_without_root_or_holders():
    msg = spl.refusal_message([("C1", "a/x.py", [])], "ask-1")
    assert "held by a live sibling" in msg
    assert "re-fire." in msg


def test_reply_keys_gain_the_two_members():
    assert spl.LEDGER_WITHHELD_PRODUCT in land_run_contract.REPLY_KEYS
    assert "exit_criterion_not_stamped" in land_run_contract.REPLY_KEYS
