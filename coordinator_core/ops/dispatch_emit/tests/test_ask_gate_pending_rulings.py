"""ask_gate sets pending_rulings beside resume_plan: J<n> ids of open revision rows; absent without a resume."""

from __future__ import annotations

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import _plan

REV = "revision@2026-10-23T00:00:00Z"


def _register(text: str) -> dict:
    return {"rows": [{rr.ROW_ANCHOR: REV, rr.ROW_TEXT: text, rr.ROW_STATUS: rr.STATUS_OPEN}]}


def test_resumable_sizing_with_a_ruling_revision_row_carries_it(repo):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch", requirement_register=_register("J1: use sonnet"))
    _plan(repo)
    v = gate(repo, REL)
    assert v.halt is None and v.resume_plan
    assert v.to_json()["pending_rulings"] == ["J1"]


def test_resumable_sizing_without_a_ruling_carries_an_empty_list(repo):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch", requirement_register=_register("also handle the edge"))
    _plan(repo)
    v = gate(repo, REL)
    assert v.halt is None and v.resume_plan
    assert v.to_json()["pending_rulings"] == []


def test_non_resume_verdict_has_no_pending_rulings_key(repo):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch", requirement_register=_register("J1: use sonnet"))
    v = gate(repo, REL)
    assert v.halt is None and v.resume_plan is None
    assert "pending_rulings" not in v.to_json()
