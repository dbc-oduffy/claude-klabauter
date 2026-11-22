"""halted_record_message: the commit message a halted run's record-only commit carries."""

from coordinator_core.ops.dispatch_emit.ask_contract import (
    HALT_NO_REASON_TEXT,
    HALT_PLAN_TRAILER,
    HALT_REASON_FIELDS,
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
    assert msg.endswith(f"Halted-By: ledger-check\nHalt-Reason: {HALT_NO_REASON_TEXT}\nHalt-Plan: docs/plans/x.md")


def test_absent_or_unsafe_plan_adds_no_line():
    # abs-path-ok: unsafe-plan fixtures
    for plan in (None, "", "/abs/x.md", "../x.md", "docs/../../x.md", "C:\\x.md", 7, "a.md\nLands-Chunks: C9", "a b.md"):
        msg = halted_record_message("x", {"halted": "ledger-check", "plan": plan})
        assert "Halt-Plan" not in msg, plan
    assert "Halt-Plan" not in halted_record_message("x", {"halted": "abandoned"})


def test_no_reason_no_items_gets_generic_reason():
    msg = halted_record_message("x", {"halted": "room"})
    assert msg.endswith(f"Halted-By: room\nHalt-Reason: {HALT_NO_REASON_TEXT}")


def test_blocking_items_build_the_reason():
    halted = {
        "halted": "ledger-check",
        "blocking_items": [
            {"id": "T23.1", "remaining": "x", "evidence": "e"},
            {"id": "T23.2", "remaining": ["a", "b"]},
            {"id": "T23.3"},
        ],
    }
    assert "Halt-Reason: T23.1: x; T23.2: a, b; T23.3: unmet" in halted_record_message("x", halted)


def test_reason_field_outranks_blocking_items():
    halted = {"halted": "k", "error": "boom", "blocking_items": [{"id": "T1", "remaining": "x"}]}
    msg = halted_record_message("x", halted)
    assert "Halt-Reason: boom" in msg and "T1" not in msg


def test_blocking_items_reason_is_cut_and_one_line():
    halted = {"halted": "k", "blocking_items": [{"id": "T1", "remaining": "a\nb " + "z" * 500}]}
    line = halted_record_message("x", halted).split("\n")[-1]
    assert line.startswith("Halt-Reason: T1: a b z") and len(line) == len("Halt-Reason: ") + 300


def test_every_record_shape_carries_one_non_empty_halt_reason():
    shapes = [{"halted": "k", f: "why"} for f in HALT_REASON_FIELDS]
    shapes += [
        {"halted": "k", "blocking_items": [{"id": "T1", "remaining": "x"}]},
        {"halted": "k", "blocking_items": []},
        {"halted": "k", "blocking_items": [{"remaining": "x"}, "junk", None]},
        {"halted": "k", "blocking_items": "T1"},
        {"halted": "k", "reason": "", "detail": None},
        {"halted": "k"},
        {},
    ]
    for halted in shapes:
        lines = [l for l in halted_record_message("x", halted).split("\n") if l.startswith("Halt-Reason:")]
        assert len(lines) == 1 and lines[0][len("Halt-Reason:") :].strip(), halted
