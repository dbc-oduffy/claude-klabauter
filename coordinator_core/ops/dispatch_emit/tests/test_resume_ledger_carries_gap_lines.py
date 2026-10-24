"""A ledger resume carries halt remaining lines as gaps to close and asks the judge for gaps_closed."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_contract import GAPS_CLOSED_FIELD, HALT_GAP_RULE
from coordinator_core.ops.dispatch_emit.ask_compose import _LEDGER_CHECK_SCHEMA, compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_OLD_LEAD = "Work still owed, by row: "
_NEW_LEAD = "Gaps still open, by row (each line is still true; close it, never pin it): "


def _script(monkeypatch) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "s")
    return compose_ask_script(
        repo_root="REPO",
        prompt=None,
        sizing_rel="state/sizings/x.yaml",
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        script_path="x.mjs",
        in_run_commit=True,
        **REVIEW_KW,
    )


def test_judge_clause_carries_halt_gap_lines_and_the_gaps_closed_instruction():
    clause = ask_compose._RESUME_LEDGER_CLAUSE_JS
    assert "Halt gap lines, by row: " in clause
    assert "JSON.stringify(_gate.resume_ledger.remaining ?? {})" in clause
    assert "pins the gap and does not close it" in clause
    assert f"return {GAPS_CLOSED_FIELD} with one {{gap, how}} per halt gap line" in clause
    assert "asserts a gap line's state is not_met" in clause
    assert clause.index("Halt gap lines, by row: ") > clause.index("Judge only these requirement rows")
    assert clause.index("Halt gap lines, by row: ") < clause.index("No requirement row is left to judge")


def test_refire_legs_say_gaps_still_open_and_carry_the_rule(monkeypatch):
    script = _script(monkeypatch)
    assert _OLD_LEAD not in script
    assert script.count(_NEW_LEAD) == 2
    assert HALT_GAP_RULE in script


def test_the_next_action_ledger_verdict_forwards_gaps_closed(monkeypatch):
    script = _script(monkeypatch)
    assert f"...(i.{GAPS_CLOSED_FIELD} ? {{ {GAPS_CLOSED_FIELD}: i.{GAPS_CLOSED_FIELD} }} : {{}})" in script


def test_the_ledger_verdict_item_schema_accepts_gaps_closed():
    item = _LEDGER_CHECK_SCHEMA["properties"]["items"]["items"]["properties"][GAPS_CLOSED_FIELD]
    assert item["type"] == "array"
    assert item["items"]["required"] == ["gap", "how"]
    assert set(item["items"]["properties"]) == {"gap", "how"}
