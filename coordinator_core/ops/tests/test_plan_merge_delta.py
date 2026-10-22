"""plan.merge delta application and delta tables."""

from __future__ import annotations

from coordinator_core.ops._plan_merge_contract import Refusal, RowAccount, SourceRow
import pytest
import yaml

from coordinator_core.ops._plan_merge_delta import _scalar, apply_delta, render_delta_tables

ROW_A = (
    "- id: A1\n"
    "  title: First\n"
    "  writes:\n"
    "    - a.py\n"
    "  body: |\n"
    "    Line one.\n"
    "      indented | pipe\n"
    "\n"
    "    After blank.\n"
)
ROW_B = "- id: B1\n  title: Second\n  disposition: open\n  body: |\n    Only body.\n"
ROWS = [SourceRow("a.md", "A1", ROW_A), SourceRow("b.md", "B1", ROW_B)]


def _apply(entries):
    return apply_delta(ROWS, {"rows": entries})


def test_unmatched_row_refuses():
    r = _apply([{"id": "ZZ", "note": "x"}])
    assert isinstance(r, Refusal) and r.code == "DELTA_UNMATCHED_ROW" and r.row_ids == ("ZZ",)


def test_unmatched_anchor_refuses():
    r = _apply([{"id": "N1", "new": True, "after": "nope", "fields": {"title": "T"}, "body": "b"}])
    assert isinstance(r, Refusal) and r.code == "DELTA_UNMATCHED_ANCHOR"


def test_empty_delta_returns_original_text():
    texts, accounts = _apply([])
    assert texts == [ROW_A, ROW_B]
    assert [a.status for a in accounts] == ["untouched", "untouched"]


def test_valid_delta_applies():
    texts, accounts = _apply(
        [
            {"id": "A1", "fields": {"title": "Renamed"}},
            {"id": "B1", "deferred": True},
            {"id": "N1", "new": True, "after": "A1", "fields": {"title": "Fresh"}, "body": "New body.\n"},
        ]
    )
    assert [a.row_id for a in accounts] == ["A1", "N1", "B1"]
    assert accounts[0] == RowAccount("A1", "a.md", "amended", ("fields",))
    assert accounts[1] == RowAccount("N1", "(delta)", "new", ("fields",))
    assert accounts[2] == RowAccount("B1", "b.md", "amended", ("drop",))
    assert "  title: Renamed\n" in texts[0]
    assert "  disposition: deferred\n" in texts[2] and "disposition: open" not in texts[2]
    assert texts[1] == "- id: N1\n  title: Fresh\n  body: |\n    New body.\n"


def test_note_keeps_source_body_as_prefix():
    texts, accounts = _apply([{"id": "B1", "note": "merged from b"}])
    assert texts[1].startswith(ROW_B.rstrip("\n"))
    assert "    CONSOLIDATION: merged from b" in texts[1]
    assert accounts[1].edit_classes == ("note",)


def test_fields_edit_keeps_unnamed_fields_and_block_scalar_verbatim():
    texts, _ = _apply([{"id": "A1", "fields": {"title": "T2"}}])
    body_block = ROW_A[ROW_A.index("  body: |"):]
    assert texts[0].endswith(body_block)
    assert "  writes:\n    - a.py\n" in texts[0]


def test_fields_edit_adds_new_key_and_list_value():
    texts, _ = _apply([{"id": "B1", "fields": {"depends_on": ["A1", "x y"], "queue": {"scope": "p"}}}])
    assert "  depends_on:\n    - A1\n    - x y\n" in texts[1]
    assert "  queue:\n    scope: p\n" in texts[1]
    assert "  body: |\n    Only body.\n  depends_on:" in texts[1]


def test_drop_fields_removes_whole_block():
    texts, accounts = _apply([{"id": "A1", "drop_fields": ["writes"]}])
    assert "writes" not in texts[0] and "a.py" not in texts[0]
    assert accounts[0].edit_classes == ("fields",)


def test_noop_entry_reports_untouched():
    texts, accounts = _apply([{"id": "B1", "deferred": False}, {"id": "A1", "drop_fields": ["absent"]}])
    assert texts == [ROW_A, ROW_B]
    assert all(a.status == "untouched" for a in accounts)


def test_mixed_classes_ordered():
    _, accounts = _apply([{"id": "B1", "note": "n", "deferred": True, "fields": {"title": "T"}}])
    assert accounts[1].edit_classes == ("fields", "note", "drop")


def test_anchor_may_be_an_earlier_new_row():
    texts, accounts = _apply(
        [
            {"id": "N1", "new": True, "after": "B1", "fields": {"title": "1"}, "body": "x"},
            {"id": "N2", "new": True, "after": "N1", "fields": {"title": "2"}, "body": "y"},
        ]
    )
    assert [a.row_id for a in accounts] == ["A1", "B1", "N1", "N2"]


def test_new_row_reusing_id_refuses():
    r = _apply([{"id": "A1", "new": True, "after": "B1", "fields": {"title": "1"}, "body": "x"}])
    assert isinstance(r, Refusal) and r.code == "ID_COLLISION"


def test_tables_carry_pinned_columns():
    _, accounts = _apply([{"id": "A1", "note": "n"}, {"id": "B1", "deferred": True}])
    out = render_delta_tables(
        accounts,
        [{"heading": "Old | notes", "source": "a.md", "reason": "superseded"}],
        reasons={"A1": "why"},
    )
    t1, t2 = out.strip().split("\n\n")
    assert t1.splitlines()[0] == "| Row | Source | Edit classes | Reason |"
    assert "| A1 | a.md | note | why |" in t1
    assert "| B1 | b.md | drop |  |" in t1
    assert t2.splitlines()[0] == "| Section | Source | Reason |"
    assert "| Old \\| notes | a.md | superseded |" in t2


def test_tables_skip_untouched_rows_and_stay_two_tables_when_empty():
    out = render_delta_tables([RowAccount("A1", "a.md", "untouched")], [])
    assert len(out.strip().split("\n\n")) == 2 and "A1" not in out


@pytest.mark.parametrize(
    "value", ["2026-10-22", "0x1F", "0b101", ".inf", ".nan", "1_000", "0o17", "12:30:45", "plain text", "v1.2.3"],
)
def test_emitted_string_scalar_round_trips_as_the_same_string(value):
    assert yaml.safe_load("k: " + _scalar(value)) == {"k": value}
