"""ask_contract's plan-row anchor prefix and empty-register refusal text."""

from coordinator_core.ops.dispatch_emit import ask_contract as ac

SIZING = "state/sizings/x.yaml"


def test_plan_row_anchor_prefix():
    assert ac.PLAN_ROW_ANCHOR_PREFIX == "plan-row:"


def test_empty_register_reason_bytes_match_the_ledger_literal():
    assert ac.EMPTY_REGISTER_REASON == "the register holds no requirement row: nothing was judged"


def test_refusal_names_reason_and_sizing():
    text = ac.empty_register_refusal(SIZING)
    assert ac.EMPTY_REGISTER_REASON in text
    assert SIZING in text
    assert "workflow.revise" in text


def test_refusal_without_sizing_names_the_run_sizing():
    text = ac.empty_register_refusal(None)
    assert text.startswith(ac.EMPTY_REGISTER_REASON)
    assert "of this run's sizing" in text


def test_head_path_tail_compose_the_refusal():
    assert (
        ac.EMPTY_REGISTER_REASON + ac.EMPTY_REGISTER_REFUSAL_HEAD + SIZING + ac.EMPTY_REGISTER_REFUSAL_TAIL
        == ac.empty_register_refusal(SIZING)
    )
