"""Tests for the fleet.migrate_prose_queue field-fill rule."""

from __future__ import annotations

import pytest

from coordinator_core.ops.fleet._prose_queue_contract import (
    BUCKET_PARSE_ERROR,
    BUCKET_SKIPPED,
    BUCKET_TO_MIGRATE,
    BUCKET_UNIVERSAL_LEFTOVER,
    NOT_RECORDED,
    RawRow,
)
from coordinator_core.ops.fleet._prose_queue_fields import fill_fields, first_sentence


def table_row(**cells: str) -> RawRow:
    named = {k.replace("_", " "): v for k, v in cells.items()}
    return RawRow("table", 1, 1, "| " + " | ".join(named.values()) + " |", named)


def pipe_row(created="2026-01-02", from_repo="self", surface="a/b.py", body="Fix it. Then more.",
             target="proposed target: do x") -> RawRow:
    cells = {"0": created, "1": from_repo, "2": surface, "3": body, "4": target}
    return RawRow("pipe", 1, 1, "- " + " | ".join(cells.values()), cells)


def test_first_sentence():
    assert first_sentence("One thing.  Two\nthings.") == "One thing."
    assert first_sentence("no terminator") == "no terminator"


def test_pipe_improvement_identity_and_constant_free_fields():
    c = fill_fields("improvement-queue", pipe_row(surface="x/SKILL.md"))
    assert c.bucket_hint == BUCKET_TO_MIGRATE
    f = c.fields
    assert (f["created"], f["title"], f["status"]) == ("2026-01-02", "Fix it.", "open")
    assert f["proposed_action"] == "do x"
    assert f["change_kind"] == "skill-edit" and "ambiguous" not in c.flags
    assert f["queue_scope"] == "project" and "from_repo" not in f
    assert "tags" not in f


def test_change_kind_default_flags_ambiguous():
    c = fill_fields("improvement-queue", pipe_row(surface="foo.py"))
    assert c.fields["change_kind"] == "script-edit" and "ambiguous" in c.flags


@pytest.mark.parametrize("surface,kind", [("run.sh", "script-edit"), ("docs" + "/wiki/a.md", "wiki-append")])
def test_change_kind_rules(surface, kind):
    assert fill_fields("improvement-queue", pipe_row(surface=surface)).fields["change_kind"] == kind


def test_non_self_from_repo_goes_to_evidence():
    c = fill_fields("improvement-queue", pipe_row(from_repo="other-em"))
    assert "from_repo: other-em" in c.fields["evidence"] and "from_repo" not in c.fields
    assert "from_repo" not in fill_fields("improvement-queue", pipe_row()).fields.get("evidence", "")


def test_improvement_table_gets_constant_proposed_action():
    c = fill_fields("improvement-queue", table_row(ID="IQ-1", System="s.py", Description="Do. More.", Found="2026-03-04"))
    assert c.fields["proposed_action"] == NOT_RECORDED
    assert c.fields["tags"] == ["migrated-incomplete"] and "migrated-incomplete" in c.flags
    assert c.fields["evidence"] == "legacy-id: IQ-1"


def test_bug_table_with_surface_and_severity():
    c = fill_fields("bug-backlog", table_row(ID="B-1", System="eng", Severity="P1", Description="Broken. Why.",
                                              Status="open", Found="2026-03-04", Extra="note"))
    f = c.fields
    assert (f["surface"], f["severity"], f["status"]) == ("eng", "P1", "open")
    assert f["body"] == "Broken. Why.\nExtra: note" and "tags" not in f


def test_bug_without_surface_gets_constant():
    c = fill_fields("bug-backlog", table_row(ID="B-1", Severity="P2", Description="Broken.", Found="2026-03-04"))
    assert c.fields["surface"] == NOT_RECORDED and "migrated-incomplete" in c.flags


def test_bug_missing_severity_is_parse_error():
    c = fill_fields("bug-backlog", table_row(ID="B-1", Description="Broken.", Found="2026-03-04"))
    assert c.bucket_hint == BUCKET_PARSE_ERROR


def test_debt_constants_on_three_fields():
    c = fill_fields("debt-backlog", table_row(ID="D-1", Description="Debt.", Added="2026-03-04"))
    f = c.fields
    assert (f["risk"], f["proposed_action"], f["source"]) == (NOT_RECORDED,) * 3
    assert f["tags"] == ["migrated-incomplete"]


def test_debt_source_column_and_why_blocked():
    c = fill_fields("debt-backlog", table_row(ID="D-1", Description="Debt.", Added="2026-03-04",
                                               Source="review", Why_Blocked="needs PM"))
    assert c.fields["source"] == "review" and c.fields["why_blocked"] == "needs PM"
    assert c.fields["risk"] == NOT_RECORDED


def test_why_blocked_absent_without_column():
    assert "why_blocked" not in fill_fields("debt-backlog", table_row(ID="D-1", Description="Debt.", Added="2026-03-04")).fields


def test_dsr_bold_row_created_from_id():
    seg = "- **DSR-2026-05-06-3** Something rotted. More text."
    c = fill_fields("debt-backlog", RawRow("dsr", 1, 1, seg, {}))
    assert c.bucket_hint == BUCKET_TO_MIGRATE
    assert c.fields["created"] == "2026-05-06"
    assert c.fields["title"] == "Something rotted."
    assert c.fields["evidence"] == "legacy-id: DSR-2026-05-06-3"


def test_dsr_headerless_table_and_dash_pipe():
    for seg in ("| DSR-2026-05-06-4 | Thing broke. | x |", "- DSR-2026-05-06-4 | Thing broke. | x"):
        c = fill_fields("debt-backlog", RawRow("dsr", 1, 1, seg, {}))
        assert c.fields["created"] == "2026-05-06" and c.fields["title"] == "Thing broke."


def test_missing_created_title_body_are_parse_errors():
    assert fill_fields("bug-backlog", table_row(ID="B-1", Severity="P1", Description="X.")).bucket_hint == BUCKET_PARSE_ERROR
    assert fill_fields("bug-backlog", table_row(ID="B-1", Severity="P1", Description="", Found="2026-01-01")).bucket_hint == BUCKET_PARSE_ERROR


def test_created_never_today():
    c = fill_fields("debt-backlog", table_row(ID="D-1", Description="Debt.", Added="2020-01-01"))
    assert c.fields["created"] == "2020-01-01"


@pytest.mark.parametrize("value", ["resolved", "fixed", "done", "**fixed**", "closed", "CLOSED 2026-01-01 — shipped",
                                   "Status: CLOSED 2026-01-01 — x", "Resolved", "DONE", "wontfix"])
def test_closed_synonyms_and_tokens_skip(value):
    c = fill_fields("bug-backlog", table_row(ID="B-1", Severity="P1", Description="X.", Status=value, Found="2026-01-01"))
    assert c.bucket_hint == BUCKET_SKIPPED and c.flags == ["closed-in-source"]


@pytest.mark.parametrize("value,expected", [("open", "open"), ("deferred", "deferred"), ("Open", "open")])
def test_open_statuses_pass_through(value, expected):
    c = fill_fields("bug-backlog", table_row(ID="B-1", Severity="P1", Description="X.", Status=value, Found="2026-01-01"))
    assert c.fields["status"] == expected


def test_unknown_status_is_parse_error():
    c = fill_fields("bug-backlog", table_row(ID="B-1", Severity="P1", Description="X.", Status="in progress", Found="2026-01-01"))
    assert c.bucket_hint == BUCKET_PARSE_ERROR


@pytest.mark.parametrize("sev", ["P0", "P1", "P2", "P3"])
def test_severity_verbatim(sev):
    c = fill_fields("bug-backlog", table_row(ID="B-1", Severity=sev, Description="X.", Found="2026-01-01"))
    assert c.fields["severity"] == sev


def test_unknown_severity_is_parse_error():
    c = fill_fields("bug-backlog", table_row(ID="B-1", Severity="high", Description="X.", Found="2026-01-01"))
    assert c.bucket_hint == BUCKET_PARSE_ERROR


def test_no_status_column_is_open():
    assert fill_fields("debt-backlog", table_row(ID="D-1", Description="Debt.", Added="2026-01-01")).fields["status"] == "open"


def test_dsr_closure_token_in_any_cell_or_text():
    seg = "- **DSR-2026-05-06-3** CLOSED 2026-06-01 — shipped."
    assert fill_fields("debt-backlog", RawRow("dsr", 1, 1, seg, {})).bucket_hint == BUCKET_SKIPPED
    cells = {"Description": "Thing.", "Notes": "DONE already"}
    row = RawRow("dsr", 1, 1, "- DSR-2026-05-06-5 | Thing. | DONE already", cells)
    assert fill_fields("debt-backlog", row).bucket_hint == BUCKET_SKIPPED


def test_dsr_grammar_cells_map_to_roles_without_leaking_into_body():
    cells = {"ID": "DSR-2026-05-27-1", "date": "2026-05-27", "file:line": "core/x.py",
             "one-line description": "Description here", "severity": "P2"}
    seg = "- DSR-2026-05-27-1 | core/x.py | Description here | P2"
    f = fill_fields("debt-backlog", RawRow("dsr-led", 1, 1, seg, cells)).fields
    assert (f["title"], f["body"], f["surface"], f["created"]) == (
        "Description here", "Description here", "core/x.py", "2026-05-27")
    bold = {"ID": "DSR-2026-06-16-1", "date": "2026-06-16", "body": "Bold text. More."}
    g = fill_fields("debt-backlog", RawRow("dsr-led", 1, 1, "- **DSR-2026-06-16-1** Bold text. More.", bold)).fields
    assert g["title"] == "Bold text." and g["body"] == "Bold text. More."


def test_debt_pipe_row_keeps_its_proposed_target():
    c = fill_fields("debt-backlog", pipe_row())
    assert c.fields["proposed_action"] == "do x"
    assert c.fields["risk"] == NOT_RECORDED


def test_pipe_improvement_body_starting_with_done_is_not_closed():
    assert fill_fields("improvement-queue", pipe_row(body="Done tracking is broken.")).bucket_hint == BUCKET_TO_MIGRATE


def test_universal_tag_is_leftover():
    row = pipe_row(body="Thing [universal] here.")
    assert fill_fields("improvement-queue", row).bucket_hint == BUCKET_UNIVERSAL_LEFTOVER


def test_unknown_family_raises():
    with pytest.raises(ValueError):
        fill_fields("lessons", pipe_row())
