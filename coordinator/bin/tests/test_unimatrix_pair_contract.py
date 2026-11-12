"""unimatrix_pair_contract: expected directions and deterministic mirror owner."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "unimatrix_pair_contract_under_test",
    Path(__file__).resolve().parents[1] / "lib" / "unimatrix_pair_contract.py")
pc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(pc)


def test_pair_expects_both_directions():
    assert pc.expected_directions({"A", "B"}) == {("A", "B"), ("B", "A")}


def test_three_members_expect_six_directed_links_no_self():
    got = pc.expected_directions(["a", "b", "c"])
    assert len(got) == 6
    assert all(x != y for x, y in got)


def test_single_or_empty_membership_expects_nothing():
    assert pc.expected_directions(["solo"]) == set()
    assert pc.expected_directions([]) == set()


def test_duplicate_and_case_variants_collapse():
    assert pc.expected_directions(["Machine-a", "machine-a", "Mac"]) == {("Machine-a", "Mac"), ("Mac", "Machine-a")}


def test_mirror_owner_is_symmetric():
    assert pc.mirror_owner("A", "B") == pc.mirror_owner("B", "A") == "A"


def test_mirror_owner_casefolds_not_ascii_order():
    # plain sort would pick "Zed" (uppercase sorts first); casefold picks "apple"
    assert pc.mirror_owner("Zed", "apple") == pc.mirror_owner("apple", "Zed") == "apple"


def test_mirror_owner_case_tie_still_symmetric():
    assert pc.mirror_owner("box", "BOX") == pc.mirror_owner("BOX", "box")


def test_exactly_one_owner_per_pair_across_mesh():
    members = ["m", "S", "k"]
    owners = {frozenset(p): pc.mirror_owner(*p) for p in pc.expected_directions(members)}
    assert len(owners) == 3
    assert all(o in p for p, o in owners.items())
