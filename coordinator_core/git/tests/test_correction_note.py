"""Round-trip and rejection pins for coordinator_core.git.correction_note."""

from coordinator_core.git.correction_note import SENTINEL, format_note, parse_note


def test_round_trip():
    assert parse_note(format_note("fix: the thing")) == "fix: the thing"


def test_round_trip_with_body():
    assert parse_note(format_note("s", "free text\nmore")) == "s"


def test_free_form_note_is_none():
    assert parse_note("just a remark") is None


def test_crlf_note():
    assert parse_note(f"{SENTINEL} subj\r\nbody\r\n") == "subj"


def test_leading_blank_lines_skipped():
    assert parse_note(f"\n\n{SENTINEL} subj") == "subj"


def test_empty_note():
    assert parse_note("") is None
    assert parse_note("   \n") is None
    assert parse_note(f"{SENTINEL}") is None
