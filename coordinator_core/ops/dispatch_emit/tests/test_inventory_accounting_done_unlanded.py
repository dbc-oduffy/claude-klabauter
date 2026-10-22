"""done_unlanded rendering and merging in inventory_accounting."""

from coordinator_core.ops.dispatch_emit import inventory_accounting as ia

NOOP = {"id": "I1.C1", "class": "no-op", "report": "r/C1.md", "route": "", "held_behind": []}
LAND = {
    "id": "I2.C2",
    "class": "land",
    "report": "r/C2.md",
    "route": "land it",
    "held_behind": ["I2.C3", "I2.C4"],
}


def _acc(ids, emitted, **extra):
    acc = ia.account(ids, emitted, {}, {})
    acc.update(extra)
    return acc


def test_print_forms():
    acc = _acc(["a"], ["a"], done_unlanded=[NOOP, LAND])
    lines = ia.summary_lines(acc, {})
    assert "  no-op I1.C1: executor report r/C1.md DONE, nothing to land" in lines
    assert "  landing I2.C2: land it" in lines
    assert "  held I2.C3: behind I2.C2" in lines
    assert "  held I2.C4: behind I2.C2" in lines


def test_absent_key_renders_unchanged():
    acc = _acc(["a"], ["a"])
    assert ia.summary_lines(acc, {}) == [
        "inventory accounting: 1 rows = 1 emitted + 0 skipped + 0 refused"
    ]


def test_merge_dedups_by_id_first_wins():
    first = dict(NOOP, report="first")
    p1 = _acc(["a"], ["a"], done_unlanded=[first])
    p2 = _acc(["a"], ["a"], done_unlanded=[dict(NOOP, report="second"), LAND])
    merged = ia.merge_parts([p1, p2])
    assert [e["id"] for e in merged["done_unlanded"]] == ["I1.C1", "I2.C2"]
    assert merged["done_unlanded"][0]["report"] == "first"


def test_merge_omits_key_when_empty():
    merged = ia.merge_parts([_acc(["a"], ["a"]), _acc(["a"], ["a"])])
    assert "done_unlanded" not in merged
