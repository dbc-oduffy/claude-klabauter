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
        lr.EmittedLeg("size", "Size", "coordinator:op", "haiku"),
        lr.EmittedLeg("review:", "Review", None, "opus"),
    ]


def test_concatenated_label_keeps_literal_prefix():
    legs = lr.emitted_legs("agent(p, { label: 'work:' + r.id, phase: 'x' })")
    assert legs[0].label == "work:"


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


def test_reason_required_and_refused_classes():
    with pytest.raises(ValueError):
        lr.LegDecl("wf", "s", lr.LegClass.DUPLICATE, "  ")
    assert lr.REFUSED_CLASSES == {lr.LegClass.OP_WRAPPER, lr.LegClass.VERDICT_READER}
    assert len(lr.LegClass) == 5
