"""
coordinator_core.ops.ceremony.tests.test_node_handlers

Tests for the D/J/F/B/X node-handler library.

Coverage:
  (a) classify_step_known_types    — classify_step returns correct type for every registered step
  (b) classify_step_unknown        — classify_step returns None for an unregistered ID
  (c) j_question_count             — exactly 8 J-questions registered (node-map corrected count)
  (d) j_question_set               — all 8 step IDs produce a non-empty question string
  (e) j_question_node_shape        — emit_j returns a schema-valid J-node with id/type/question/answer
  (f) j_question_answer_empty_phase1 — answer="" in phase-1 (EM has not yet answered)
  (g) j_question_answer_filled     — emit_j(step_id, answer="yes") stores the answer
  (h) j_unknown_step_raises        — emit_j raises KeyError for a D/F/B/X step
  (i) f_slot_count                 — exactly 3 F-slots registered
  (j) f_slot_set                   — all 3 step IDs produce a non-empty slot description
  (k) f_slot_node_shape            — emit_f returns a schema-valid F-node with id/type/slot/filled
  (l) f_slot_filled_empty_phase1   — filled="" in phase-1 (EM has not yet authored)
  (m) f_slot_filled_authored       — emit_f(step_id, filled="text") stores the prose
  (n) f_unknown_step_raises        — emit_f raises KeyError for a D/J/B/X step
  (o) b_handler_two_slots          — emit_b returns a B-node with pre_resolved_evidence + em_adjudication
  (p) b_handler_generic_keys       — B-node keys are generic (not wsc-specific names like dispatch_plan)
  (q) b_handler_empty_defaults     — both blobs default to {} when not supplied
  (r) b_handler_wsc_instance       — wsc B1 filled with review-wave blob does NOT rename keys
  (s) b_handler_non_review_bracket — non-review B bracket fills generic slots without renaming
  (t) d_handler_node_shape         — handle_d returns a schema-valid D-node
  (u) d_handler_tail_step          — tail_step=True propagates correctly
  (v) d_handler_defaults           — resolving_op="" and evidence={} defaults work
  (w) x_handler_node_shape         — emit_x returns a schema-valid X-node with missing_signal
  (x) x_unknown_step_raises        — emit_x raises KeyError for non-X step
  (y) known_helpers                — known_j/f/b_step_ids helpers return correct counts
  (z) classify_all_types_covered   — every node type D/J/F/B/X appears in the classification map
  (AC4a-a..e) classify_bulk_eligibility — the five PM-ruled bulk-eligibility cases (C2):
                                          non-fyi refused, still-open in_reply_to refused,
                                          unresolvable in_reply_to refused fail-closed, absent
                                          in_reply_to accepted, resolved-closed in_reply_to accepted

Spec backlink:
  coordinator_core/ops/ceremony/node_handlers.py
  docs/plans/2026-07-06-ceremony-as-pipeline-2-invert-workstream.md § C2.3 / A3
  docs/plans/2026-07-06-ceremony-as-pipeline-2-invert-workstream.node-map.md
  docs/plans/2026-07-26-memo-disposition-flip-op-and-hand-edit-hole.md § C2 / AC4a
"""

from __future__ import annotations

import pytest

from coordinator_core.ops.ceremony.node_handlers import (
    # step ID constants
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
    # corpora
    F_SLOTS,
    J_QUESTIONS,
    X_MISSING_SIGNALS,
    # handlers
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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
    STEP_2B,
    STEP_2_6_6C,
    STEP_4B,
]

_ALL_D_STEPS = [
    STEP_0, STEP_1C, STEP_2A, STEP_2_4A, STEP_2_6_1, STEP_2_6_2,
    STEP_2_6_3, STEP_2_6_3A, STEP_2_6_4, STEP_2_6_5, STEP_2_6_5A, STEP_2_6_6A,
    STEP_2_6_6B, STEP_2_6_8, STEP_2_65A, STEP_2_65C, STEP_2_7, STEP_2_75,
    STEP_2_8B, STEP_2_9A, STEP_2_9B, STEP_2_9C, STEP_2_9D, STEP_2_9E,
    STEP_2_9_OBS, STEP_2_95A, STEP_2_96, STEP_3_0, STEP_3_1, STEP_3_2, STEP_3_3,
    STEP_3_5A, STEP_3_5B, STEP_4A,
    # --- X→D reclassifications ---
    STEP_2_67A,  # reclassified X→D: filesystem-mtime scratch scan (C2 spinoff)
    # --- F→D reclassifications (Option B, memo 2026-07-08) ---
    STEP_1B,     # reclassified F→D: lesson authored disk-first (Option B, memo 2026-07-08)
    STEP_2_4B,   # reclassified F→D: ALLOWLIST edit written in place (Option B, memo 2026-07-08)
]

# X is currently unpopulated: all prior X-steps reclassified to D (C1/C2/C3 spinoffs).
# The parametrized tests below collect 0 cases; they re-activate if X ever returns.
_ALL_X_STEPS: list[str] = []

_ALL_B_STEPS = [STEP_B1]


# ---------------------------------------------------------------------------
# (a) classify_step — returns correct type for every registered step
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("step_id", _ALL_D_STEPS)
def test_classify_d_steps(step_id: str) -> None:
    """classify_step returns 'D' for every registered D-step."""
    assert classify_step(step_id) == "D"


@pytest.mark.parametrize("step_id", _ALL_J_STEPS)
def test_classify_j_steps(step_id: str) -> None:
    """classify_step returns 'J' for every registered J-step."""
    assert classify_step(step_id) == "J"


@pytest.mark.parametrize("step_id", _ALL_F_STEPS)
def test_classify_f_steps(step_id: str) -> None:
    """classify_step returns 'F' for every registered F-step."""
    assert classify_step(step_id) == "F"


@pytest.mark.parametrize("step_id", _ALL_X_STEPS)
def test_classify_x_steps(step_id: str) -> None:
    """classify_step returns 'X' for every registered X-step."""
    assert classify_step(step_id) == "X"


@pytest.mark.parametrize("step_id", _ALL_B_STEPS)
def test_classify_b_steps(step_id: str) -> None:
    """classify_step returns 'B' for every registered B-step."""
    assert classify_step(step_id) == "B"


# ---------------------------------------------------------------------------
# (b) classify_step_unknown — returns None for an unregistered ID
# ---------------------------------------------------------------------------


def test_classify_unknown_step_returns_none() -> None:
    """classify_step returns None for an unknown step ID."""
    assert classify_step("step_99.99") is None
    assert classify_step("") is None
    assert classify_step("step_4c") is None  # meta-step, not a pipeline node


# ---------------------------------------------------------------------------
# (c) j_question_count — exactly 8 J-questions registered
# ---------------------------------------------------------------------------


def test_j_question_count_is_8() -> None:
    """Exactly 8 J-questions are registered (corrected node-map count: Scout A 9 → -1 for 2.9b→D)."""
    assert len(J_QUESTIONS) == 8


# ---------------------------------------------------------------------------
# (d) j_question_set — all 8 step IDs produce a non-empty question string
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# (e) j_question_node_shape — emit_j returns schema-valid J-node
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("step_id", _ALL_J_STEPS)
def test_emit_j_node_shape(step_id: str) -> None:
    """emit_j returns a dict with id/type/question/answer and validates as J-node."""
    node = emit_j(step_id)
    assert node["id"] == step_id
    assert node["type"] == "J"
    assert isinstance(node["question"], str) and len(node["question"]) > 0
    assert "answer" in node


# ---------------------------------------------------------------------------
# (f) j_question_answer_empty_phase1 — answer="" in phase-1
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("step_id", _ALL_J_STEPS)
def test_emit_j_answer_empty_by_default(step_id: str) -> None:
    """emit_j defaults answer to '' — the EM has not yet answered in phase-1."""
    node = emit_j(step_id)
    assert node["answer"] == ""


# ---------------------------------------------------------------------------
# (g) j_question_answer_filled — supplied answer is stored
# ---------------------------------------------------------------------------


def test_emit_j_answer_stored() -> None:
    """emit_j(step_id, answer='yes') stores the supplied answer."""
    node = emit_j(STEP_1A, answer="yes")
    assert node["answer"] == "yes"


# ---------------------------------------------------------------------------
# (h) j_unknown_step_raises — KeyError for non-J step
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("non_j_step", [STEP_0, STEP_1B, STEP_B1, STEP_2_6_3, "step_unknown"])
def test_emit_j_raises_for_non_j_step(non_j_step: str) -> None:
    """emit_j raises KeyError when the step_id is not in J_QUESTIONS."""
    with pytest.raises(KeyError):
        emit_j(non_j_step)


# ---------------------------------------------------------------------------
# (i) f_slot_count — exactly 3 F-slots registered
# ---------------------------------------------------------------------------


def test_f_slot_count_is_3() -> None:
    """Exactly 3 F-slots are registered (Option B: STEP_1B/STEP_2_4B reclassified F→D)."""
    assert len(F_SLOTS) == 3


# ---------------------------------------------------------------------------
# (j) f_slot_set — all 3 step IDs produce a non-empty slot description
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("step_id", _ALL_F_STEPS)
def test_f_slot_is_non_empty(step_id: str) -> None:
    """Every F-step has a non-empty slot description in F_SLOTS."""
    slot = F_SLOTS[step_id]
    assert isinstance(slot, str)
    assert len(slot) > 0


def test_f_slot_all_3_step_ids_in_corpus() -> None:
    """All 3 canonical F-step IDs are present as keys in F_SLOTS."""
    for step_id in _ALL_F_STEPS:
        assert step_id in F_SLOTS, f"F-step {step_id!r} missing from F_SLOTS"


# ---------------------------------------------------------------------------
# (k) f_slot_node_shape — emit_f returns schema-valid F-node
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("step_id", _ALL_F_STEPS)
def test_emit_f_node_shape(step_id: str) -> None:
    """emit_f returns a dict with id/type/slot/filled."""
    node = emit_f(step_id)
    assert node["id"] == step_id
    assert node["type"] == "F"
    assert isinstance(node["slot"], str) and len(node["slot"]) > 0
    assert "filled" in node


# ---------------------------------------------------------------------------
# (l) f_slot_filled_empty_phase1 — filled="" in phase-1
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("step_id", _ALL_F_STEPS)
def test_emit_f_filled_empty_by_default(step_id: str) -> None:
    """emit_f defaults filled to '' — the EM has not yet authored in phase-1."""
    node = emit_f(step_id)
    assert node["filled"] == ""


# ---------------------------------------------------------------------------
# (m) f_slot_filled_authored — supplied prose is stored
# ---------------------------------------------------------------------------


def test_emit_f_filled_stored() -> None:
    """emit_f(step_id, filled='prose') stores the supplied prose."""
    node = emit_f(STEP_2B, filled="This is the plan completion note.")
    assert node["filled"] == "This is the plan completion note."


# ---------------------------------------------------------------------------
# (n) f_unknown_step_raises — KeyError for non-F step
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("non_f_step", [STEP_0, STEP_1A, STEP_B1, STEP_2_6_3, "step_unknown"])
def test_emit_f_raises_for_non_f_step(non_f_step: str) -> None:
    """emit_f raises KeyError when the step_id is not in F_SLOTS."""
    with pytest.raises(KeyError):
        emit_f(non_f_step)


# ---------------------------------------------------------------------------
# (o) b_handler_two_slots — B-node has pre_resolved_evidence + em_adjudication
# ---------------------------------------------------------------------------


def test_emit_b_has_two_generic_slots() -> None:
    """emit_b returns a B-node dict with both generic slot keys present."""
    node = emit_b(STEP_B1)
    assert "pre_resolved_evidence" in node
    assert "em_adjudication" in node
    assert node["id"] == STEP_B1
    assert node["type"] == "B"


# ---------------------------------------------------------------------------
# (p) b_handler_generic_keys — keys are generic, not wsc-specific
# ---------------------------------------------------------------------------


def test_emit_b_no_wsc_specific_keys() -> None:
    """emit_b must NOT produce wsc-specific keys like dispatch_plan or adjudication."""
    node = emit_b(STEP_B1)
    forbidden = {"dispatch_plan", "adjudication", "review_verdict", "review_wave"}
    present_forbidden = set(node.keys()) & forbidden
    assert not present_forbidden, (
        f"B-node contains wsc-specific key(s) {present_forbidden}; "
        "these must not appear — generality is the invariant."
    )


# ---------------------------------------------------------------------------
# (q) b_handler_empty_defaults — both blobs default to {}
# ---------------------------------------------------------------------------


def test_emit_b_defaults_to_empty_dicts() -> None:
    """emit_b with no blobs supplied produces pre_resolved_evidence={} and em_adjudication={}."""
    node = emit_b(STEP_B1)
    assert node["pre_resolved_evidence"] == {}
    assert node["em_adjudication"] == {}


# ---------------------------------------------------------------------------
# (r) b_handler_wsc_instance — wsc B1 filled with review-wave blob without rename
# ---------------------------------------------------------------------------


def test_emit_b_wsc_review_wave_instance() -> None:
    """wsc B1 carries review-wave keys in the pre_resolved_evidence blob without renaming the outer key."""
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

    # outer keys are GENERIC
    assert "pre_resolved_evidence" in node
    assert "em_adjudication" in node
    # wsc-specific fields live INSIDE the blobs, not as top-level keys
    assert node["pre_resolved_evidence"]["slice_count"] == 2
    assert node["pre_resolved_evidence"]["brightline_verdict"] == "PARTITION-MANDATORY"
    assert node["em_adjudication"]["aggregate_verdict"] == "WARN"


# ---------------------------------------------------------------------------
# (s) b_handler_non_review_bracket — non-review B bracket uses same generic keys
# ---------------------------------------------------------------------------


def test_emit_b_non_review_bracket_no_rename() -> None:
    """A non-review B bracket (e.g. PM-approval bracket) fills generic slots without renaming."""
    pm_pre = {
        "approval_checklist": ["item-a", "item-b"],
        "auto_approved_items": ["item-a"],
    }
    pm_adj = {
        "pm_decision": "approved",
        "notes": "deferred item-b to follow-up",
    }
    node = emit_b("step_pm_bracket", pre_resolved_evidence=pm_pre, em_adjudication=pm_adj)

    # still uses the same generic keys — no rename needed
    assert node["type"] == "B"
    assert "pre_resolved_evidence" in node
    assert "em_adjudication" in node
    assert node["pre_resolved_evidence"]["approval_checklist"] == ["item-a", "item-b"]
    assert node["em_adjudication"]["pm_decision"] == "approved"


# ---------------------------------------------------------------------------
# (t) d_handler_node_shape — handle_d returns schema-valid D-node
# ---------------------------------------------------------------------------


def test_handle_d_node_shape() -> None:
    """handle_d returns a dict with id/type/resolving_op/evidence/tail_step."""
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


# ---------------------------------------------------------------------------
# (u) d_handler_tail_step — tail_step=True propagates
# ---------------------------------------------------------------------------


def test_handle_d_tail_step_true() -> None:
    """handle_d(tail_step=True) sets tail_step=True in the returned dict."""
    node = handle_d(
        STEP_3_5A,
        resolving_op="cs_archive",
        evidence={"acted": ["sid-abc"], "skipped": [], "failed": []},
        tail_step=True,
    )
    assert node["tail_step"] is True


# ---------------------------------------------------------------------------
# (v) d_handler_defaults — empty resolving_op and evidence={} work
# ---------------------------------------------------------------------------


def test_handle_d_defaults() -> None:
    """handle_d with no optional args produces well-formed defaults."""
    node = handle_d(STEP_0)
    assert node["resolving_op"] == ""
    assert node["evidence"] == {}
    assert node["tail_step"] is False


# ---------------------------------------------------------------------------
# (w) x_handler_node_shape — emit_x returns schema-valid X-node
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("step_id", _ALL_X_STEPS)
def test_emit_x_node_shape(step_id: str) -> None:
    """emit_x returns a dict with id/type/missing_signal for every X-step."""
    node = emit_x(step_id)
    assert node["id"] == step_id
    assert node["type"] == "X"
    assert isinstance(node["missing_signal"], str)
    assert len(node["missing_signal"]) > 0


# Review: code-reviewer F8 — add direct positive-path test for emit_x via a synthetic
# X_MISSING_SIGNALS injection.  _ALL_X_STEPS is [] (all X-steps reclassified D); the
# parametrized tests collect 0 cases.  This direct test keeps the happy path covered.


def test_emit_x_direct_with_synthetic_registration(monkeypatch) -> None:
    """emit_x produces a valid X-node when a synthetic entry is injected into X_MISSING_SIGNALS.

    Monkeypatches the module-level X_MISSING_SIGNALS dict to inject a test-only
    key, then calls emit_x and asserts the returned node has the correct shape.
    This covers the emit_x → make_x_node happy path that _ALL_X_STEPS=[] leaves dark.

    Review: code-reviewer F8 — emit_x positive-path coverage via synthetic injection.
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


# ---------------------------------------------------------------------------
# (x) x_unknown_step_raises — KeyError for non-X step
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "non_x_step",
    [
        STEP_0,
        STEP_1A,
        STEP_B1,
        STEP_1B,
        "step_unknown",
        # Review: code-reviewer — STEP_2_6_3, STEP_2_96, and STEP_2_67A reclassified X→D 2026-07-06;
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


# ---------------------------------------------------------------------------
# (y) known_helpers — known_j/f/b_step_ids return correct counts
# ---------------------------------------------------------------------------


def test_known_j_step_ids_count() -> None:
    """known_j_step_ids() returns exactly 8 entries."""
    assert len(known_j_step_ids()) == 8


def test_known_f_step_ids_count() -> None:
    """known_f_step_ids() returns exactly 3 entries."""
    assert len(known_f_step_ids()) == 3


def test_known_b_step_ids_count() -> None:
    """known_b_step_ids() returns exactly 1 entry (step_B1)."""
    ids = known_b_step_ids()
    assert len(ids) == 1
    assert STEP_B1 in ids


def test_known_j_step_ids_match_corpus() -> None:
    """known_j_step_ids() matches the J_QUESTIONS keys (same 8 steps)."""
    assert set(known_j_step_ids()) == set(J_QUESTIONS.keys())


def test_known_f_step_ids_match_corpus() -> None:
    """known_f_step_ids() matches the F_SLOTS keys (same 3 steps)."""
    assert set(known_f_step_ids()) == set(F_SLOTS.keys())


# ---------------------------------------------------------------------------
# (z) classify_all_types_covered — every type D/J/F/B/X appears in the map
# ---------------------------------------------------------------------------


def test_all_node_types_represented_in_classification_map() -> None:
    """The classification map uses only valid node types, and D/J/F/B are all populated.

    X is reserved-but-currently-unpopulated in the step registry (BranchResolution/receipts
    may still emit X); the map-population invariant softens from '==' to 'every populated
    type is valid, and D/J/F/B are all present'.
    """
    from coordinator_core.ops.ceremony.node_handlers import _STEP_NODE_TYPES

    observed_types = set(_STEP_NODE_TYPES.values())
    assert observed_types <= VALID_NODE_TYPES, (
        f"Unknown node types: {observed_types - VALID_NODE_TYPES}"
    )
    assert {"D", "J", "F", "B"} <= observed_types, (
        f"Missing required populated types: {{'D', 'J', 'F', 'B'}} - {observed_types}"
    )


def test_total_registered_step_count_is_49() -> None:
    """The classification map contains exactly 49 pipeline steps (corrected node-map count)."""
    from coordinator_core.ops.ceremony.node_handlers import _STEP_NODE_TYPES

    assert len(_STEP_NODE_TYPES) == 49, (
        f"Expected 49 pipeline steps; got {len(_STEP_NODE_TYPES)}"
    )


# ---------------------------------------------------------------------------
# classify_bulk_eligibility (C2, AC4a) — the five PM-ruled bulk-eligibility cases
# ---------------------------------------------------------------------------
# Spec backlink: docs/plans/2026-07-26-memo-disposition-flip-op-and-hand-edit-hole.md § C2 / AC4a


def test_bulk_eligibility_non_fyi_refused() -> None:
    """(a) A non-fyi memo is NOT bulk-eligible regardless of in_reply_to."""
    eligible, reason = classify_bulk_eligibility("consult", None, None)
    assert eligible is False
    assert "not kind: fyi" in reason


def test_bulk_eligibility_fyi_in_reply_to_still_open_refused() -> None:
    """(b) fyi whose in_reply_to resolves to a still-open target is NOT bulk-eligible."""
    eligible, reason = classify_bulk_eligibility("fyi", "2026-07-20-ask.md", "open")
    assert eligible is False
    assert "still-open" in reason


def test_bulk_eligibility_fyi_in_reply_to_unresolvable_fails_closed() -> None:
    """(c) fyi whose in_reply_to is present but unresolvable fails CLOSED, not open."""
    eligible, reason = classify_bulk_eligibility("fyi", "2026-07-20-typo.md", "unresolvable")
    assert eligible is False
    assert "unresolvable" in reason
    assert "fail CLOSED" in reason


def test_bulk_eligibility_fyi_no_in_reply_to_accepted() -> None:
    """(d) fyi with no in_reply_to at all is bulk-eligible."""
    eligible, reason = classify_bulk_eligibility("fyi", None, None)
    assert eligible is True
    assert "no in_reply_to" in reason


def test_bulk_eligibility_fyi_in_reply_to_resolved_closed_accepted() -> None:
    """(e) fyi whose in_reply_to resolves to a closed (archived) target is bulk-eligible."""
    eligible, reason = classify_bulk_eligibility("fyi", "2026-07-20-ask.md", "closed")
    assert eligible is True
    assert "closed target" in reason
