"""A ruled-out register row is never offered for a verdict: resume blocking, rulable ids and the verify prompt."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import LedgerHalt, rulable_ids
from coordinator_core.ops.dispatch_emit.ask_gate import _resume_ledger
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW


def _row(rid: str, status: str, ref: str | None = None) -> dict:
    row = {"id": rid, "source_text": rid, "anchor": "ask", "surface": "internal", "status": status}
    if ref:
        row["ruling"] = {"source": "pm", "quote": "q", "ref": ref}
    return row


ROWS = [
    _row("R1", "deferred", "workflow.revise:ask-20990101T000000-aaaaaa:out_of_scope"),
    _row("R2", "open"),
    _row("R3", "deferred"),
]


def test_resume_blocking_drops_the_ruled_out_row(tmp_path):
    # Blocking is the halt's ids then every other rulable row; R3 is deferred with no ruling, so rulable.
    halt = LedgerHalt(run_id="r", plan_rel="p", sizing_rel="s", blocking=("R1", "R2"))
    reply = _resume_ledger({"requirement_register": {"rows": ROWS}}, halt, tmp_path)
    assert reply["blocking"] == ["R2", "R3"]
    assert "R1" not in reply["rulable_ids"]


def test_resume_blocking_may_be_empty(tmp_path):
    halt = LedgerHalt(run_id="r", plan_rel="p", sizing_rel="s", blocking=("R1",))
    assert _resume_ledger({"requirement_register": {"rows": ROWS[:1]}}, halt, tmp_path)["blocking"] == []


def test_rulable_ids_omit_ruled_out_but_keep_plain_deferred():
    assert rulable_ids(ROWS) == ("R2", "R3")


def test_composed_script_states_the_out_of_scope_rule_and_empty_resume(monkeypatch):
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "s")
    script = compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel="state/sizings/x.yaml", run_id="run-1", session_id=None,
        wrap_stage=lambda _t: ("  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]),
        plan_blitz_text="stub", script_path="x.mjs", in_run_commit=True, **REVIEW_KW,
    )
    assert "Out of scope heading are ruled out: give them no item" in script
    assert "No requirement row is left to judge. Return items: []." in script
