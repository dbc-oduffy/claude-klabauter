"""Pins the plan-size bounds table, lookup and measurement."""

from coordinator_core.roadmap.plan_size_bounds import BOUNDS, PlanSize, bounds_for, measure


def test_table_values_exact():
    assert dict(BOUNDS) == {"L": (6, 3000), "XL": (8, 3000), "XXL": (10, 3000)}


def test_lookup_case_insensitive_and_off_table_none():
    assert bounds_for("xl") == (8, 3000)
    assert bounds_for(" Xxl ") == (10, 3000)
    assert bounds_for("M") is None
    assert bounds_for(None) is None


def test_closed_and_held_rows_not_counted():
    rows = [
        {"id": "A", "body": "x"},
        {"id": "B", "disposition": "open", "body": "yy"},
        {"id": "C", "disposition": "coded", "body": "zzz"},
        {"id": "D", "disposition": "done"},
        {"id": "E", "deferred": True},
        {"id": "F", "deferred_until": "2027-01-01"},
        {"id": "G"},
    ]
    size = measure(rows)
    assert size.rows == 3
    assert size.row_body_bytes == (("A", 1), ("B", 2), ("G", 0))


def test_multibyte_body_measured_in_bytes():
    assert measure([{"id": "A", "body": "é€"}]).row_body_bytes == (("A", 5),)


def test_over_names_each_exceeded_bound_and_clean_is_empty():
    rows = [{"id": f"C{i}", "body": "a"} for i in range(7)]
    rows[3]["body"] = "a" * 4120
    size = measure(rows)
    assert size.over(BOUNDS["L"], "L") == ["7 rows > 6 at L", "C3 body 4120 bytes > 3000"]
    assert size.over(BOUNDS["XL"]) == ["C3 body 4120 bytes > 3000"]
    assert PlanSize(rows=2, row_body_bytes=()).over((6, 3000)) == []
