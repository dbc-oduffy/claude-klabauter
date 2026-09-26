
from __future__ import annotations

import pytest

from coordinator_core.ops.ceremony.node_handlers import (
    STEP_0,
    STEP_1A,
    STEP_1B,
    STEP_1C,
    STEP_1_2,
    STEP_2A,
    STEP_2B,
    STEP_2_4A,
    STEP_2_4B,
    STEP_2_6_1,
    STEP_2_6_2,
    STEP_2_6_3,
    STEP_2_6_3A,
    STEP_2_6_4,
    STEP_2_6_5,
    STEP_2_6_5A,
    STEP_2_6_6A,
    STEP_2_6_6B,
    STEP_2_6_6C,
    STEP_2_6_7,
    STEP_2_6_8,
    STEP_2_65A,
    STEP_2_65B,
    STEP_2_65C,
    STEP_2_67A,
    STEP_2_67B,
    STEP_2_7,
    STEP_2_75,
    STEP_2_8A,
    STEP_2_8B,
    STEP_2_8C,
    STEP_2_9A,
    STEP_2_9B,
    STEP_2_9C,
    STEP_2_9D,
    STEP_2_9E,
    STEP_2_9_OBS,
    STEP_2_95A,
    STEP_2_95B,
    STEP_2_96,
    STEP_3_0,
    STEP_3_1,
    STEP_3_2,
    STEP_3_3,
    STEP_3_5A,
    STEP_3_5B,
    STEP_4A,
    STEP_4B,
    STEP_B1,
    F_SLOTS,
    J_QUESTIONS,
    X_MISSING_SIGNALS,
    classify_bulk_eligibility,
    classify_step,
    emit_b,
    emit_f,
    emit_j,
    emit_x,
    handle_d,
    known_b_step_ids,
    known_f_step_ids,
    known_j_step_ids,
)
from coordinator_core.ops.ceremony.receipt_schema import (
    VALID_NODE_TYPES,
    validate,
)


_ALL_J_STEPS = [
    STEP_1A,
    STEP_1_2,
    STEP_2_6_7,
    STEP_2_65B,
    STEP_2_67B,
    STEP_2_8A,
    STEP_2_8C,
    STEP_2_95B,
]

_ALL_F_STEPS = [
    STEP_2_6_6C,
]

_ALL_D_STEPS = [
    STEP_0, STEP_1C, STEP_2A, STEP_2_4A, STEP_2_6_1, STEP_2_6_2,
    STEP_2_6_3, STEP_2_6_3A, STEP_2_6_4, STEP_2_6_5, STEP_2_6_5A, STEP_2_6_6A,
    STEP_2_6_6B, STEP_2_6_8, STEP_2_65A, STEP_2_65C, STEP_2_7, STEP_2_75,
    STEP_2_8B, STEP_2_9A, STEP_2_9B, STEP_2_9C, STEP_2_9D, STEP_2_9E,
    STEP_2_9_OBS, STEP_2_95A, STEP_2_96, STEP_3_0, STEP_3_1, STEP_3_2, STEP_3_3,
    STEP_3_5A, STEP_3_5B, STEP_4A,
    STEP_2_67A,
    STEP_1B,
    STEP_2_4B,   # reclassified F→D: ALLOWLIST edit written in place (Option B, memo 2026-07-08)
    STEP_2B,
    STEP_4B,
]

_ALL_X_STEPS: list[str] = []

_ALL_B_STEPS = [STEP_B1]


@pytest.mark.parametrize("step_id", _ALL_D_STEPS)
def test_classify_d_steps(step_id: str) -> None:
    assert classify_step(step_id) == "D"


@pytest.mark.parametrize("step_id", _ALL_J_STEPS)
def test_classify_j_steps(step_id: str) -> None:
    assert classify_step(step_id) == "J"


@pytest.mark.parametrize("step_id", _ALL_F_STEPS)
def test_classify_f_steps(step_id: str) -> None:
    assert classify_step(step_id) == "F"


@pytest.mark.parametrize("step_id", _ALL_X_STEPS)
def test_classify_x_steps(step_id: str) -> None:
    assert classify_step(step_id) == "X"


@pytest.mark.parametrize("step_id", _ALL_B_STEPS)
def test_classify_b_steps(step_id: str) -> None:
    assert classify_step(step_id) == "B"


def test_classify_unknown_step_returns_none() -> None:
    assert classify_step("step_99.99") is None
    assert classify_step("") is None
    assert classify_step("step_4c") is None


def test_j_question_count_is_8() -> None:
    assert len(J_QUESTIONS) == 8


@pytest.mark.parametrize("step_id", _ALL_J_STEPS)
def test_j_question_is_non_empty(step_id: str) -> None:
    """Every J-step has a non-empty question string in J_QUESTIONS."""
    question = J_QUESTIONS[step_id]
    assert isinstance(question, str)
    assert len(question) > 0


def test_j_question_all_8_step_ids_in_corpus() -> None:
    """All 8 canonical J-step IDs are present as keys in J_QUESTIONS."""
    for step_id in _ALL_J_STEPS:
        assert step_id in J_QUESTIONS, f"J-step {step_id!r} missing from J_QUESTIONS"


@pytest.mark.parametrize("step_id", _ALL_J_STEPS)
def test_emit_j_node_shape(step_id: str) -> None:
    node = emit_j(step_id)
    assert node["id"] == step_id
    assert node["type"] == "J"
    assert isinstance(node["question"], str) and len(node["question"]) > 0
    assert "answer" in node


@pytest.mark.parametrize("step_id", _ALL_J_STEPS)
def test_emit_j_answer_empty_by_default(step_id: str) -> None:
    node = emit_j(step_id)
    assert node["answer"] == ""


def test_emit_j_answer_stored() -> None:
    node = emit_j(STEP_1A, answer="yes")
    assert node["answer"] == "yes"


@pytest.mark.parametrize("non_j_step", [STEP_0, STEP_1B, STEP_B1, STEP_2_6_3, "step_unknown"])
def test_emit_j_raises_for_non_j_step(non_j_step: str) -> None:
    """emit_j raises KeyError when the step_id is not in J_QUESTIONS."""
    with pytest.raises(KeyError):
        emit_j(non_j_step)


def test_f_slot_count_is_1() -> None:
    """Exactly 1 F-slot is registered (STEP_1B/STEP_2_4B/STEP_2B/STEP_4B all reclassified F→D)."""
    assert len(F_SLOTS) == 1


@pytest.mark.parametrize("step_id", _ALL_F_STEPS)
def test_f_slot_is_non_empty(step_id: str) -> None:
    slot = F_SLOTS[step_id]
    assert isinstance(slot, str)
    assert len(slot) > 0


def test_f_slot_all_1_step_ids_in_corpus() -> None:
    for step_id in _ALL_F_STEPS:
        assert step_id in F_SLOTS, f"F-step {step_id!r} missing from F_SLOTS"


@pytest.mark.parametrize("step_id", _ALL_F_STEPS)
def test_emit_f_node_shape(step_id: str) -> None:
    node = emit_f(step_id)
    assert node["id"] == step_id
    assert node["type"] == "F"
    assert isinstance(node["slot"], str) and len(node["slot"]) > 0
    assert "filled" in node


@pytest.mark.parametrize("step_id", _ALL_F_STEPS)
def test_emit_f_filled_empty_by_default(step_id: str) -> None:
    node = emit_f(step_id)
    assert node["filled"] == ""


def test_emit_f_filled_stored() -> None:
    node = emit_f(STEP_2_6_6C, filled="This is the completion entry body.")
    assert node["filled"] == "This is the completion entry body."


@pytest.mark.parametrize(
    "non_f_step",
    [
        STEP_0,
        STEP_1A,
        STEP_B1,
        STEP_2_6_3,
        "step_unknown",
        STEP_2B,
        STEP_4B,
    ],
)
def test_emit_f_raises_for_non_f_step(non_f_step: str) -> None:
    with pytest.raises(KeyError):
        emit_f(non_f_step)


def test_emit_b_has_two_generic_slots() -> None:
    node = emit_b(STEP_B1)
    assert "pre_resolved_evidence" in node
    assert "em_adjudication" in node
    assert node["id"] == STEP_B1
    assert node["type"] == "B"


def test_emit_b_no_wsc_specific_keys() -> None:
    node = emit_b(STEP_B1)
    forbidden = {"dispatch_plan", "adjudication", "review_verdict", "review_wave"}
    present_forbidden = set(node.keys()) & forbidden
    assert not present_forbidden, (
        f"B-node contains wsc-specific key(s) {present_forbidden}; "
        "these must not appear — generality is the invariant."
    )


def test_emit_b_defaults_to_empty_dicts() -> None:
    node = emit_b(STEP_B1)
    assert node["pre_resolved_evidence"] == {}
    assert node["em_adjudication"] == {}


def test_emit_b_wsc_review_wave_instance() -> None:
    wsc_pre = {
        "slice_count": 2,
        "partition_boundaries": ["src/", "docs/"],
        "docs_checker_inclusion": True,
        "brightline_verdict": "PARTITION-MANDATORY",
    }
    wsc_adj = {
        "aggregate_verdict": "WARN",
        "integration_disposition": "proceed-with-notes",
    }
    node = emit_b(STEP_B1, pre_resolved_evidence=wsc_pre, em_adjudication=wsc_adj)

    assert "pre_resolved_evidence" in node
    assert "em_adjudication" in node
    assert node["pre_resolved_evidence"]["slice_count"] == 2
    assert node["pre_resolved_evidence"]["brightline_verdict"] == "PARTITION-MANDATORY"
    assert node["em_adjudication"]["aggregate_verdict"] == "WARN"


def test_emit_b_non_review_bracket_no_rename() -> None:
    pm_pre = {
        "approval_checklist": ["item-a", "item-b"],
        "auto_approved_items": ["item-a"],
    }
    pm_adj = {
        "pm_decision": "approved",
        "notes": "deferred item-b to follow-up",
    }
    node = emit_b("step_pm_bracket", pre_resolved_evidence=pm_pre, em_adjudication=pm_adj)

    assert node["type"] == "B"
    assert "pre_resolved_evidence" in node
    assert "em_adjudication" in node
    assert node["pre_resolved_evidence"]["approval_checklist"] == ["item-a", "item-b"]
    assert node["em_adjudication"]["pm_decision"] == "approved"


def test_handle_d_node_shape() -> None:
    node = handle_d(
        STEP_2_6_1,
        resolving_op="git log --diff-filter=A",
        evidence={"method": "git-log", "value": ["docs/plans/foo.md"]},
    )
    assert node["id"] == STEP_2_6_1
    assert node["type"] == "D"
    assert node["resolving_op"] == "git log --diff-filter=A"
    assert isinstance(node["evidence"], dict)
    assert node["tail_step"] is False


def test_handle_d_tail_step_true() -> None:
    node = handle_d(
        STEP_3_5A,
        resolving_op="cs_archive",
        evidence={"acted": ["sid-abc"], "skipped": [], "failed": []},
        tail_step=True,
    )
    assert node["tail_step"] is True


def test_handle_d_defaults() -> None:
    node = handle_d(STEP_0)
    assert node["resolving_op"] == ""
    assert node["evidence"] == {}
    assert node["tail_step"] is False


# transcriber — the same disk-first / silent-drop shape STEP_1B/STEP_2_4B had


@pytest.mark.parametrize("step_id", [STEP_2B, STEP_4B])
def test_step_2b_step_4b_emit_as_d_nodes(step_id: str) -> None:
    assert classify_step(step_id) == "D"
    assert step_id not in F_SLOTS
    node = handle_d(step_id, resolving_op="disk-first", evidence={"note": "authored in place"})
    assert node["id"] == step_id
    assert node["type"] == "D"
    with pytest.raises(KeyError):
        emit_f(step_id)


@pytest.mark.parametrize("step_id", _ALL_X_STEPS)
def test_emit_x_node_shape(step_id: str) -> None:
    node = emit_x(step_id)
    assert node["id"] == step_id
    assert node["type"] == "X"
    assert isinstance(node["missing_signal"], str)
    assert len(node["missing_signal"]) > 0


# X_MISSING_SIGNALS injection.  _ALL_X_STEPS is [] (all X-steps reclassified D); the


def test_emit_x_direct_with_synthetic_registration(monkeypatch) -> None:
    """emit_x produces a valid X-node when a synthetic entry is injected into X_MISSING_SIGNALS.

    Monkeypatches the module-level X_MISSING_SIGNALS dict to inject a test-only
    key, then calls emit_x and asserts the returned node has the correct shape.
    This covers the emit_x → make_x_node happy path that _ALL_X_STEPS=[] leaves dark.

    emit_x positive-path coverage via synthetic injection.
    """
    import coordinator_core.ops.ceremony.node_handlers as nh

    synthetic_signals = {"step_test_x_synthetic": "TEST_SYNTHETIC_SIGNAL"}
    monkeypatch.setattr(nh, "X_MISSING_SIGNALS", synthetic_signals)

    node = emit_x("step_test_x_synthetic")
    assert node["id"] == "step_test_x_synthetic"
    assert node["type"] == "X"
    assert isinstance(node["missing_signal"], str)
    assert len(node["missing_signal"]) > 0
    assert node["missing_signal"] == "TEST_SYNTHETIC_SIGNAL"


@pytest.mark.parametrize(
    "non_x_step",
    [
        STEP_0,
        STEP_1A,
        STEP_B1,
        STEP_1B,
        "step_unknown",
        # STEP_2_6_3, STEP_2_96, and STEP_2_67A reclassified X→D 2026-07-06;
        # adding them here locks the reclassification: re-adding to X_MISSING_SIGNALS turns red.
        STEP_2_6_3,
        STEP_2_96,
        STEP_2_67A,
    ],
)
def test_emit_x_raises_for_non_x_step(non_x_step: str) -> None:
    """emit_x raises KeyError when the step_id is not in X_MISSING_SIGNALS."""
    with pytest.raises(KeyError):
        emit_x(non_x_step)


def test_known_j_step_ids_count() -> None:
    assert len(known_j_step_ids()) == 8


def test_known_f_step_ids_count() -> None:
    assert len(known_f_step_ids()) == 1


def test_known_b_step_ids_count() -> None:
    ids = known_b_step_ids()
    assert len(ids) == 1
    assert STEP_B1 in ids


def test_known_j_step_ids_match_corpus() -> None:
    """known_j_step_ids() matches the J_QUESTIONS keys (same 8 steps)."""
    assert set(known_j_step_ids()) == set(J_QUESTIONS.keys())


def test_known_f_step_ids_match_corpus() -> None:
    assert set(known_f_step_ids()) == set(F_SLOTS.keys())


def test_all_node_types_represented_in_classification_map() -> None:
    from coordinator_core.ops.ceremony.node_handlers import _STEP_NODE_TYPES

    observed_types = set(_STEP_NODE_TYPES.values())
    assert observed_types <= VALID_NODE_TYPES, (
        f"Unknown node types: {observed_types - VALID_NODE_TYPES}"
    )
    assert {"D", "J", "F", "B"} <= observed_types, (
        f"Missing required populated types: {{'D', 'J', 'F', 'B'}} - {observed_types}"
    )


def test_total_registered_step_count_is_49() -> None:
    from coordinator_core.ops.ceremony.node_handlers import _STEP_NODE_TYPES

    assert len(_STEP_NODE_TYPES) == 49, (
        f"Expected 49 pipeline steps; got {len(_STEP_NODE_TYPES)}"
    )


def test_bulk_eligibility_non_fyi_refused() -> None:
    eligible, reason = classify_bulk_eligibility("consult", None, None)
    assert eligible is False
    assert "not kind: fyi" in reason


def test_bulk_eligibility_fyi_in_reply_to_still_open_refused() -> None:
    eligible, reason = classify_bulk_eligibility("fyi", "2026-07-20-ask.md", "open")
    assert eligible is False
    assert "still-open" in reason


def test_bulk_eligibility_fyi_in_reply_to_unresolvable_fails_closed() -> None:
    eligible, reason = classify_bulk_eligibility("fyi", "2026-07-20-typo.md", "unresolvable")
    assert eligible is False
    assert "unresolvable" in reason
    assert "fail CLOSED" in reason


def test_bulk_eligibility_fyi_no_in_reply_to_accepted() -> None:
    eligible, reason = classify_bulk_eligibility("fyi", None, None)
    assert eligible is True
    assert "no in_reply_to" in reason


def test_bulk_eligibility_fyi_in_reply_to_resolved_closed_accepted() -> None:
    eligible, reason = classify_bulk_eligibility("fyi", "2026-07-20-ask.md", "closed")
    assert eligible is True
    assert "closed target" in reason
