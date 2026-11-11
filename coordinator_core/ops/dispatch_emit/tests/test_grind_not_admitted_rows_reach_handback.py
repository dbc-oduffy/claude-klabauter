"""
coordinator_core.ops.dispatch_emit.tests.test_grind_not_admitted_rows_reach_handback

Purpose: pins that rows the emit-time budget did not admit are carried into the emitted manifest and
returned in the grind HANDBACK with a reason naming the cap, so a budget-cut row never vanishes.
"""
from __future__ import annotations

from coordinator_core.ops.dispatch_emit.grind_admission import admit, not_admitted_entries

from .test_grind_compose import _compose


def _manifest_literal(script):
    return script[script.index("const QUEUE_GRIND_MANIFEST"): script.index("const HANDBACK = {")]


def test_budget_cut_rows_carry_a_reason_naming_the_cap():
    admission = admit(["a", "b", "c"], max_agent_calls=None, budget_tokens=140076 * 2)
    entries = not_admitted_entries(admission)
    assert [e["row_id"] for e in entries] == ["c"]
    assert "budget_tokens=280152" in entries[0]["reason"]


def test_not_admitted_rows_reach_manifest_and_handback():
    admission = admit(["a", "b", "c"], max_agent_calls=None, budget_tokens=140076)
    script = _compose(not_admitted=not_admitted_entries(admission))
    literal = _manifest_literal(script)
    assert '"row_id": "b"' in literal and '"row_id": "c"' in literal
    assert "budget_tokens=140076" in literal
    assert "not_admitted: QUEUE_GRIND_MANIFEST.not_admitted," in script[script.index("const HANDBACK = {"):]


def test_full_admission_emits_an_empty_list():
    admission = admit(["a", "b"], max_agent_calls=None, budget_tokens=None)
    assert not_admitted_entries(admission) == []
    assert '"not_admitted": []' in _manifest_literal(_compose(not_admitted=[]))
    assert '"not_admitted": []' in _manifest_literal(_compose())


def test_peer_claimed_rows_carry_a_reason_naming_the_holder():
    from types import SimpleNamespace

    from coordinator_core.ops.dispatch_emit.grind_admission import claimed_entries

    entries = claimed_entries([SimpleNamespace(row_id="x", holder_session="sess-1")])
    assert entries == [{"row_id": "x", "reason": "not-admitted: claimed by live session sess-1"}]
    assert claimed_entries([]) == []
