"""TestBlocker interface: gap_line shape, reverify filtering, pinned constants."""

from __future__ import annotations

from coordinator_core.ops import review_test_blocker as tb
from coordinator_core.ops.review_test_blocker import TestBlocker, from_reverify


def test_constants_are_pinned():
    assert tb.TEST_BLOCKER == "failing_test"
    assert tb.FAILING_DETAIL_FIELD == "failing_detail"


def test_own_diff_blockers_keeps_only_own_files_both_sides():
    result = {"failing": ["pkg/t.py::a", "other/u.py::b", "pkg\\t.py::c"], "pre_existing": []}
    got = tb.own_diff_blockers(result, ["./pkg/t.py"])
    assert [b.node_id for b in got] == ["pkg/t.py::a", "pkg\\t.py::c"]
    assert tb.own_diff_blockers(result, []) == []
    assert tb.own_diff_blockers(result, ["pkg/other.py"]) == []


def test_own_diff_blockers_respects_pre_existing_and_pins_field():
    result = {"failing": ["t.py::a", "t.py::b"], "pre_existing": ["t.py::a"]}
    assert tb.own_diff_blockers(result, ["t.py"]) == [TestBlocker("t.py::b", "")]
    assert tb.OWN_DIFF_PATHS_FIELD == "review_declared_paths"


def test_gap_line_takes_first_line_only():
    assert TestBlocker("t.py::a", "boom\nsecond").gap_line() == "t.py::a: boom"


def test_gap_line_truncates_to_300_chars():
    line = TestBlocker("t.py::a", "x" * 500).gap_line()
    assert line == "t.py::a: " + "x" * 300


def test_gap_line_empty_message():
    assert TestBlocker("t.py::a").gap_line() == "t.py::a: "


def test_from_reverify_drops_pre_existing_and_kept():
    result = {
        "failing": ["a::1", "a::2", "a::3", "a::4"],
        "pre_existing": ["a::2"],
        "kept": ["a::3"],
        "failing_detail": [
            {"node_id": "a::1", "message": "assert 1 == 2\ntrace"},
            {"node_id": "a::2", "message": "old"},
        ],
    }
    got = from_reverify(result)
    assert got == [TestBlocker("a::1", "assert 1 == 2\ntrace"), TestBlocker("a::4", "")]


def test_from_reverify_accepts_list_detail_and_missing_fields():
    result = {"failing": ["a::1"], "failing_detail": [{"node_id": "a::1", "message": "m"}]}
    assert from_reverify(result) == [TestBlocker("a::1", "m")]
    assert from_reverify({}) == []
