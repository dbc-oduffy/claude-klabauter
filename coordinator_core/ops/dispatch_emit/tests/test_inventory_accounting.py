"""Pins the inventory_accounting contract: sum invariant, gap and double-entry
refusals, part merge, and the CLI summary lines."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.inventory_accounting import (
    IN_ANOTHER_PART,
    InventoryAccountingError,
    account,
    merge_parts,
    summary_lines,
)

IDS = ["m1", "m2", "m3", "m4"]


def test_counts_sum_to_inventory_in_table_order():
    acc = account(IDS, ["m3", "m1"], {"m2": "routed-out: plan not prepped"}, [{"id": "m4", "reason": "operator"}])
    assert acc["inventory_rows"] == 4
    assert acc["emitted"] == ["m1", "m3"]
    assert acc["skipped"] == [{"id": "m2", "reason": "routed-out: plan not prepped"}]
    assert acc["refused"] == [{"id": "m4", "reason": "operator"}]
    assert sum(acc["counts"].values()) == acc["inventory_rows"]


def test_unaccounted_id_refuses_naming_it():
    with pytest.raises(InventoryAccountingError, match="m4"):
        account(IDS, ["m1"], {"m2": "x"}, {"m3": "y"})


def test_double_accounted_id_refuses_naming_it():
    with pytest.raises(InventoryAccountingError, match="in two buckets: m2"):
        account(IDS, ["m1", "m2"], {"m2": "x", "m3": "y"}, {"m4": "z"})


def test_id_outside_inventory_refuses():
    with pytest.raises(InventoryAccountingError, match="m9"):
        account(IDS, ["m1", "m9"], {"m2": "x", "m3": "y", "m4": "z"}, {})


def test_merge_discharges_in_another_part_when_sibling_emitted():
    a = account(IDS, ["m1", "m2"], {"m3": IN_ANOTHER_PART, "m4": IN_ANOTHER_PART}, {})
    b = account(IDS, ["m3"], {"m1": IN_ANOTHER_PART, "m2": IN_ANOTHER_PART, "m4": "deferred"}, {})
    merged = merge_parts([a, b])
    assert merged["emitted"] == ["m1", "m2", "m3"]
    assert merged["skipped"] == [{"id": "m4", "reason": "deferred"}]
    assert merged["inventory_rows"] == 4
    assert sum(merged["counts"].values()) == 4


def test_merge_refusal_beats_in_another_part():
    a = account(IDS, ["m1", "m2", "m3"], {"m4": IN_ANOTHER_PART}, {})
    b = account(IDS, [], {"m1": IN_ANOTHER_PART, "m2": IN_ANOTHER_PART, "m3": IN_ANOTHER_PART}, {"m4": "gate"})
    assert merge_parts([a, b])["refused"] == [{"id": "m4", "reason": "gate"}]


def test_merge_with_no_sibling_taking_the_item_refuses():
    a = account(IDS, ["m1", "m2", "m3"], {"m4": IN_ANOTHER_PART}, {})
    with pytest.raises(InventoryAccountingError, match="m4"):
        merge_parts([a])


def test_summary_prints_one_edited_line_per_plan():
    acc = account(IDS, ["m1"], {"m2": "landed", "m3": "routed-out"}, {"m4": "operator"})
    lines = summary_lines(
        acc, {"docs/plans/a.md": ["C1", "C2"], "docs/plans/b.md": ["D1"]}
    )
    assert lines[0] == "inventory accounting: 4 rows = 1 emitted + 2 skipped + 1 refused"
    assert "  skipped m2: landed" in lines
    assert "  refused m4: operator" in lines
    assert [l for l in lines if l.startswith("edited ")] == [
        "edited docs/plans/a.md: C1, C2 -> coded",
        "edited docs/plans/b.md: D1 -> coded",
    ]


def test_summary_prints_no_edited_line_when_nothing_edited():
    acc = account(IDS, IDS, {}, {})
    assert not [l for l in summary_lines(acc, {}) if l.startswith("edited")]
