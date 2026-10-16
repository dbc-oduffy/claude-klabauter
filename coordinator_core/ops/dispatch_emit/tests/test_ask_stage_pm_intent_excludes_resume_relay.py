"""pm_intent_block quotes the PM's words on a resumed ask, never the harness relay text."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.ask_stage import pm_intent_block

_RELAY = "[Workflow harness — computed task] The task text below was computed at runtime.\nResume the run."
_SIZING = {"pm_verbatims": [{"turn": 0, "text": "fix it all, literally"}]}


def test_a_harness_relay_ask_text_is_not_quoted_as_pm_intent():
    block = pm_intent_block(_SIZING, _RELAY)
    assert "> fix it all, literally" in block
    assert "Workflow harness" not in block
    assert "Resume the run" not in block


def test_a_relay_alone_leaves_no_pm_block():
    assert pm_intent_block({}, _RELAY) == ""


def test_a_relayed_from_driving_session_text_is_excluded():
    text = "Any other text was relayed from the driving session's chat"
    assert "relayed" not in pm_intent_block(_SIZING, text)


def test_a_marker_deep_in_a_real_ask_does_not_drop_it():
    text = "fix this. " + "x" * 500 + " pasted: <system-reminder> was relayed from the driving session"
    assert "> fix this." in pm_intent_block(_SIZING, text)


def test_a_genuine_pm_ask_text_still_joins():
    assert "> a real ask" in pm_intent_block(_SIZING, "a real ask")
