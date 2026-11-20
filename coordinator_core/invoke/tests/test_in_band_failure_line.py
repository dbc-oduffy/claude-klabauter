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


def test_refusal_is_a_process_failure_naming_its_reason():
    from coordinator_core.invoke.__main__ import _exit_code_for_response as code

    resp = {"result": {"refused": "gate-refused", "reason": "lock denied"}}
    assert code(resp, 99, "engine_ask.submit") == 1
    out = line(resp, "engine_ask.submit", "")
    assert "gate-refused" in out and "lock denied" in out
    assert code(resp, 99, "ceremony.commit_v2") == 0
    assert line(resp, "ceremony.commit_v2", "") is None


def test_fire_wait_nonzero_exit_fails_process_and_names_result_reason():
    from coordinator_core.invoke.__main__ import _exit_code_for_response as code

    resp = {"result": {"exit_code": 1, "reason": "outcome=failed"}}
    assert code(resp, 99, "workflow.fire_wait") == 1
    assert "outcome=failed" in line(resp, "workflow.fire_wait", "")
    assert code({"result": {"exit_code": 0}}, 99, "workflow.fire_wait") == 0
    assert code({"result": {"exit_code": 1}}, 99, "op.x") == 0
    assert code({"result": {"refused": None}}, 99, "op.x") == 0


def test_zero_exit_code_returns_none():
    assert line({"result": {"exit_code": 0, "error": "x"}}, "op.x", "") is None
