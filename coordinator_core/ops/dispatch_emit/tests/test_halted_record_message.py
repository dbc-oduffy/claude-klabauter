"""halted_record_message: the commit message a halted run's record-only commit carries."""

from coordinator_core.ops.dispatch_emit.ask_contract import (
    HALT_PLAN_TRAILER,
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


def test_halt_plan_line_follows_halted_by_and_reason():
    msg = halted_record_message(
        "x", {"halted": "ledger-check", "reason": "r", "plan": "docs/plans/x.md"}
    )
    assert msg.split("\n")[2:] == [
        "Halted-By: ledger-check",
        "Halt-Reason: r",
        f"{HALT_PLAN_TRAILER}: docs/plans/x.md",
    ]
    assert HALT_PLAN_TRAILER == "Halt-Plan"


def test_halt_plan_line_without_reason():
    msg = halted_record_message("x", {"halted": "ledger-check", "plan": "docs/plans/x.md"})
    assert msg.endswith("Halted-By: ledger-check\nHalt-Plan: docs/plans/x.md")


def test_absent_or_unsafe_plan_adds_no_line():
    # abs-path-ok: unsafe-plan fixtures
    for plan in (None, "", "/abs/x.md", "../x.md", "docs/../../x.md", "C:\\x.md", 7, "a.md\nLands-Chunks: C9", "a b.md"):
        msg = halted_record_message("x", {"halted": "ledger-check", "plan": plan})
        assert "Halt-Plan" not in msg, plan
    assert "Halt-Plan" not in halted_record_message("x", {"halted": "abandoned"})


def test_no_reason_no_line():
    msg = halted_record_message("x", {"halted": "room"})
    assert "Halt-Reason" not in msg
    assert msg.endswith("Halted-By: room")
