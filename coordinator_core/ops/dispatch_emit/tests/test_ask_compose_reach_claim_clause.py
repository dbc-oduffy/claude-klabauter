"""The verify leg's docs step asks for reach claims in the canonical backticked form."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.ask_compose import _docs_prompt_parts


def _prompt():
    return "".join(p for p in _docs_prompt_parts(run_id="run-1", manifest_rel="m.json"))


def test_step_b_asks_for_the_checked_reach_form_once():
    text = _prompt()
    assert text.count('"`X` reaches `Y`"') == 1
    assert "refuses the landing when the tree does not support it" in text


def test_clause_sits_inside_step_b_before_the_process_record_ban():
    text = _prompt()
    b = text.index("(b) Write the delivery note")
    assert b < text.index("reaches `Y`") < text.index("Never edit a plan, sizing")
