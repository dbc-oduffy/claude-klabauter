"""halted_record_message: the commit message a halted run's record-only commit carries."""

from coordinator_core.ops.dispatch_emit.ask_contract import (
    HALTED_RECORD_PARAM,
    halted_record_message,
)


def test_param_name():
    assert HALTED_RECORD_PARAM == "halted_by"


def test_subject_and_halted_by():
    lines = halted_record_message("ask-1", {"halted": "refusal", "reason": "too big"}).split("\n")
    assert lines[0] == "warp: record halted ask-1 run artifacts"
    assert lines[1] == ""
    assert lines[2] == "Halted-By: refusal"
    assert lines[3] == "Halt-Reason: too big"


def test_reason_precedence():
    full = {"halted": "k", "reason": "r", "detail": "d", "refused": "f", "error": "e"}
    assert "Halt-Reason: r" in halted_record_message("x", full)
    del full["reason"]
    assert "Halt-Reason: d" in halted_record_message("x", full)
    del full["detail"]
    assert "Halt-Reason: f" in halted_record_message("x", full)
    del full["refused"]
    assert "Halt-Reason: e" in halted_record_message("x", full)


def test_empty_and_null_fields_skipped():
    msg = halted_record_message("x", {"halted": "k", "reason": "", "detail": None, "error": "boom"})
    assert msg.endswith("Halt-Reason: boom")


def test_newlines_collapse_to_one_line():
    msg = halted_record_message("x", {"halted": "k", "reason": "a\n\nb\r\n  c"})
    assert msg.split("\n")[-1] == "Halt-Reason: a b c"
    assert msg.count("Halt-Reason:") == 1


def test_cut_at_300_chars():
    msg = halted_record_message("x", {"halted": "k", "reason": "z" * 500})
    assert msg.split("\n")[-1] == "Halt-Reason: " + "z" * 300


def test_no_reason_no_line():
    msg = halted_record_message("x", {"halted": "room"})
    assert "Halt-Reason" not in msg
    assert msg.endswith("Halted-By: room")
