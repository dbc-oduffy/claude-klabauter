"""A sibling_owned failing node yields no blocker; an unclassified one still does."""

from coordinator_core.ops.review_test_blocker import from_reverify, own_diff_blockers


def _result():
    return {
        "failing": ["a/t.py::sib", "a/t.py::new"],
        "sibling_owned": ["a/t.py::sib"],
        "failing_detail": [{"node_id": "a/t.py::new", "message": "boom"}],
    }


def test_sibling_owned_excluded_unclassified_kept():
    assert [b.node_id for b in from_reverify(_result())] == ["a/t.py::new"]


def test_sibling_owned_absent_still_blocks():
    r = _result()
    del r["sibling_owned"]
    assert [b.node_id for b in from_reverify(r)] == ["a/t.py::new", "a/t.py::sib"]


def test_own_diff_blockers_excludes_sibling_owned():
    assert [b.node_id for b in own_diff_blockers(_result(), ["a/t.py"])] == ["a/t.py::new"]


def test_foreign_red_keys_are_excluded_from_blockers():
    r = {**_result(), "foreign_red": {"a/t.py::new": ["x/y.py"]}}
    assert from_reverify(r) == []
    assert own_diff_blockers(r, ["a/t.py"]) == []


def test_composed_js_excluded_set_names_sibling_owned():
    from coordinator_core.ops.dispatch_emit import ask_compose

    assert "?.sibling_owned ?? []" in ask_compose._test_blockers_js("_r", "_t")
