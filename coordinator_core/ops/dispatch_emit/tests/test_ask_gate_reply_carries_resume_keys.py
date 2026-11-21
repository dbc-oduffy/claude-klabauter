"""A gate reply names `resume_plan`/`resume_rows` even when null, and the relay schema requires both.

A relaying worker that drops an absent key routes an existing plan to a fresh scaffold, which refuses
(example-game-repo 7b7a96): the keys must be present for the relay to copy, and required so it cannot omit them."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose, ask_gate


def test_a_reply_without_resume_names_both_keys_as_null():
    out = ask_gate._close_gate_leg({"sizing_path": "s.yaml"}, {"arm": "m_plus", "halt": None}, None)
    assert out["resume_plan"] is None and out["resume_rows"] is None


def test_a_reply_with_resume_keeps_it():
    reply = {"arm": "m_plus", "halt": None, "resume_plan": "docs/plans/p.md", "resume_rows": ["G"]}
    out = ask_gate._close_gate_leg({"sizing_path": "s.yaml"}, reply, None)
    assert out["resume_plan"] == "docs/plans/p.md" and out["resume_rows"] == ["G"]


def test_the_relay_schema_requires_both_keys():
    for schema in (ask_compose._GATE_SCHEMA, ask_compose._GATE_STAGED_SCHEMA):
        assert {"resume_plan", "resume_rows"} <= set(schema["required"])
