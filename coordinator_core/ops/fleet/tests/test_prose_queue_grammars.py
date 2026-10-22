"""parse_file: one case per grammar and DSR shape, ignored tables, unparseable prose, CRLF parity."""

from coordinator_core.ops.fleet._prose_queue_contract import (
    OUTCOME_FILE_UNPARSEABLE,
    OUTCOME_PARSED,
)
from coordinator_core.ops.fleet._prose_queue_grammars import (
    GRAMMAR_DSR,
    GRAMMAR_PIPE,
    GRAMMAR_TABLE,
    parse_file,
)

PIPE = (
    "# Improvement queue\n\n"
    "- 2026-06-01 | self | scripts/a.sh | Fix the thing | proposed target: scripts/a.sh\n"
    "- 2026-06-02 | self | SKILL.md | body with | pipe | proposed target: x\n"
    "- 2026-06-03 | self | short\n"
)

TABLE = (
    "# Debt\n\n"
    "| ID | System | Severity | Summary | Source | Status | Added | Why Blocked | Owner |\n"
    "|---|---|---|---|---|---|---|---|---|\n"
    "| D-1 | Build | P1 | Slow build. Very. | review | open | 2026-05-01 | none | alice |\n"
    "| D-2 | CI | P2 | Flaky \\| test | review | **fixed** | 2026-05-02 | | bob |\n"
)

OTHER_TABLE = "| cwd | env |\n|---|---|\n| root | none |\n"

DSR = (
    "Schema: `DSR-YYYY-MM-DD-N | file:line | one-line description | severity`\n\n"
    "- DSR-2026-05-27-1 | core/x.py | Description here | P2\n"
    "- DSR-2026-05-28-1 | core/y.py | too | many | cells | P3\n"
    "- **DSR-2026-06-16-1** [tag] Bold text. More.\n"
    "| DSR-2026-06-17-1 | a | b | c |\n"
    "| DSR-2026-06-17-2 | a | b |\n"
)


def test_pipe_grammar():
    fp = parse_file(PIPE)
    assert fp.outcome == OUTCOME_PARSED
    assert [r.grammar for r in fp.rows] == [GRAMMAR_PIPE] * 3
    r0, r1, r2 = fp.rows
    assert (r0.line_start, r0.line_end) == (3, 3)
    assert r0.cells == {
        "0": "2026-06-01",
        "1": "self",
        "2": "scripts/a.sh",
        "3": "Fix the thing",
        "4": "proposed target: scripts/a.sh",
    }
    assert r1.cells["3"] == "body with"
    assert r1.cells["4"] == "pipe|proposed target: x"
    assert len(r2.cells) == 3


def test_table_grammar_maps_by_header_name():
    fp = parse_file(TABLE)
    assert [r.grammar for r in fp.rows] == [GRAMMAR_TABLE] * 2
    r0, r1 = fp.rows
    assert r0.cells["ID"] == "D-1"
    assert r0.cells["Description"] == "Slow build. Very."
    assert r0.cells["Found"] == "2026-05-01"
    assert r0.cells["Why Blocked"] == "none"
    assert r0.cells["Owner"] == "alice"
    assert r1.cells["Description"] == "Flaky | test"
    assert r1.cells["Status"] == "**fixed**"
    assert (r0.line_start, r1.line_start) == (5, 6)


def test_table_column_order_is_irrelevant():
    text = "| Description | ID |\n|---|---|\n| thing | X-1 |\n"
    (row,) = parse_file(text).rows
    assert row.cells == {"Description": "thing", "ID": "X-1"}


def test_non_entry_table_is_ignored():
    fp = parse_file(OTHER_TABLE)
    assert fp.outcome == OUTCOME_FILE_UNPARSEABLE
    assert fp.rows == []
    assert [r.cells["ID"] for r in parse_file(OTHER_TABLE + "\n" + TABLE).rows] == ["D-1", "D-2"]


def test_table_without_description_or_summary_is_ignored():
    assert parse_file("| ID | Owner |\n|---|---|\n| X | y |\n").rows == []


def test_dsr_dash_pipe_with_matching_schema():
    r = parse_file(DSR).rows[0]
    assert r.grammar == GRAMMAR_DSR
    assert r.cells == {
        "ID": "DSR-2026-05-27-1",
        "date": "2026-05-27",
        "file:line": "core/x.py",
        "one-line description": "Description here",
        "severity": "P2",
    }


def test_dsr_cell_count_mismatch_keeps_id_and_date_only():
    r = parse_file(DSR).rows[1]
    assert r.cells == {
        "ID": "DSR-2026-05-28-1",
        "date": "2026-05-28",
        "body": "core/y.py | too | many | cells | P3",
    }


def test_dsr_bold_form_carries_no_cells():
    r = parse_file(DSR).rows[2]
    assert r.cells == {"ID": "DSR-2026-06-16-1", "date": "2026-06-16", "body": "[tag] Bold text. More."}


def test_dsr_headerless_table_rows():
    fp = parse_file(DSR)
    r3, r4 = fp.rows[3], fp.rows[4]
    assert r3.grammar == GRAMMAR_DSR
    assert r3.cells["severity"] == "c"
    assert r4.cells == {"ID": "DSR-2026-06-17-2", "date": "2026-06-17", "body": "a | b"}


def test_dsr_without_schema_line_maps_id_and_date_only():
    (r,) = parse_file("- DSR-2026-05-27-1 | a | b | c\n").rows
    assert r.cells == {"ID": "DSR-2026-05-27-1", "date": "2026-05-27", "body": "a | b | c"}


def test_headered_table_rows_are_not_double_counted_as_dsr():
    text = "| ID | Description |\n|---|---|\n| DSR-2026-01-01-1 | x |\n"
    (r,) = parse_file(text).rows
    assert r.grammar == GRAMMAR_TABLE


def test_heading_prose_is_file_unparseable():
    text = "# Bugs\n\n## BUG-1 something broke\n\nSome paragraph.\n\n## BUG-2 other\n"
    fp = parse_file(text)
    assert fp.outcome == OUTCOME_FILE_UNPARSEABLE
    assert fp.rows == []


def test_fenced_examples_are_not_rows():
    text = "```\n- 2026-06-01 | self | s | b | proposed target: t\n```\n"
    assert parse_file(text).rows == []


def test_crlf_copy_parses_to_same_rows():
    for text in (PIPE, TABLE, DSR, TABLE + OTHER_TABLE):
        lf = parse_file(text)
        crlf = parse_file(text.replace("\n", "\r\n"))
        assert crlf.outcome == lf.outcome
        assert [(r.grammar, r.line_start, r.cells) for r in crlf.rows] == [
            (r.grammar, r.line_start, r.cells) for r in lf.rows
        ]
        assert all("\r" not in r.segment for r in crlf.rows)
