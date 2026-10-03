"""`_in_band_failure_line` reads result.error, then result.message, then stderr."""
from coordinator_core.invoke.__main__ import _in_band_failure_line as line


def test_error_field_is_the_reason():
    out = line({"result": {"exit_code": 1, "error": "boom"}}, "op.x", "")
    assert "boom" in out and "result.error" in out
    assert "carries no reason" not in out


def test_message_field_is_the_reason():
    out = line({"result": {"exit_code": 1, "message": "bad"}}, "op.x", "stale\n")
    assert "bad" in out and "result.message" in out
    assert "carries no reason" not in out


def test_error_wins_over_message_and_stderr():
    out = line({"result": {"exit_code": 2, "error": "E", "message": "M"}}, "op.x", "S")
    assert "(result.error): E" in out


def test_blank_fields_fall_back_to_stderr():
    out = line({"result": {"exit_code": 1, "error": "  ", "message": ""}}, "op.x", "a\nlast\n")
    assert out.endswith("Reason: last")


def test_nothing_anywhere_keeps_legacy_text():
    out = line({"result": {"exit_code": 1}}, "op.x", "")
    assert "no reason was written to stderr" in out


def test_zero_exit_code_returns_none():
    assert line({"result": {"exit_code": 0, "error": "x"}}, "op.x", "") is None
