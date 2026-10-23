"""Tests for the leg register extractor, declarations and lookup."""

import pytest

from coordinator_core.ops.dispatch_emit import leg_register as lr

SCRIPT = """
const a = await agent(`prompt with label: "fake" and agent(x)`, { label: 'size', phase: 'Size', agentType: 'coordinator:op', model: 'haiku' });
const b = await agent(prompt, { label: `review:${role}`, phase: "Review", model: 'opus' });
// agent(nope, { label: 'commented' })
"""


def test_extracts_two_legs_one_templated():
    legs = lr.emitted_legs(SCRIPT)
    assert legs == [
        lr.EmittedLeg("size", "Size", "coordinator:op", "haiku", 'prompt with label: "fake" and agent(x)'),
        lr.EmittedLeg("review:", "Review", None, "opus", None),
    ]


def test_templated_prompt_keeps_its_literal_prefix():
    legs = lr.emitted_legs("agent(`Run exactly \\`git ${cmd}\\``, { label: 'a' })")
    assert legs[0].prompt == "Run exactly `git "


def test_variable_prompt_yields_none():
    assert lr.emitted_legs("agent(prompt, { label: 'a' })")[0].prompt is None


@pytest.mark.parametrize("marker", lr.RELAY_MARKERS)
def test_relay_markers_finds_each_marker(marker):
    assert lr.relay_markers(f"do this. {marker} then stop") == (marker,)


def test_relay_markers_empty_for_judgment_prompt_and_none():
    assert lr.relay_markers("Review the diff and rule on each finding.") == ()
    assert lr.relay_markers(None) == ()


def test_concatenated_label_keeps_literal_prefix():
    legs = lr.emitted_legs("agent(p, { label: 'work:' + r.id, phase: 'x' })")
    assert legs[0].label == "work:"


def test_a_quote_inside_a_regex_literal_does_not_hide_later_legs():
    script = (
        "const q = (v) => \"'\" + String(v).replace(/'/g, \"'\\\\''\") + \"'\";\n"
        "const a = await agent('p', { label: 'after', phase: 'x' });\n"
    )
    assert [leg.label for leg in lr.emitted_legs(script)] == ["after"]


def test_division_beside_strings_is_left_alone():
    script = "const r = a / 2 + 'x'; const b = await agent('p', { label: 'one' }); const c = d / 4 + 'y';"
    assert [leg.label for leg in lr.emitted_legs(script)] == ["one"]


def test_lookup_hit_and_miss(monkeypatch):
    decl = lr.LegDecl("wf", "size", lr.LegClass.EARNS_ITS_COST, "judgment")
    monkeypatch.setattr(lr, "REGISTER", (decl,))
    assert lr.lookup("wf", "size") is decl
    assert lr.lookup("wf", "other") is None
    assert lr.lookup("other", "size") is None


def test_runtime_floor_only_on_op_wrapper():
    lr.LegDecl("wf", "s", lr.LegClass.OP_WRAPPER, "r", runtime_floor="state/apm-rulings/x.md")
    with pytest.raises(ValueError):
        lr.LegDecl("wf", "s", lr.LegClass.EARNS_ITS_COST, "r", runtime_floor="state/apm-rulings/x.md")


def test_register_declares_each_leg_once():
    from collections import Counter

    counts = Counter((d.workflow, d.label) for d in lr.REGISTER)
    assert [k for k, n in counts.items() if n > 1] == []


def test_reason_required_and_refused_classes():
    with pytest.raises(ValueError):
        lr.LegDecl("wf", "s", lr.LegClass.DUPLICATE, "  ")
    assert lr.REFUSED_CLASSES == {lr.LegClass.OP_WRAPPER, lr.LegClass.VERDICT_READER}
    assert len(lr.LegClass) == 5
