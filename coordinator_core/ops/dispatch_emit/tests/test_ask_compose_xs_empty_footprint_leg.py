"""An arm-xs refire whose ledger-remaining leg has empty writes halts on the refusal and never dispatches an empty-footprint leg."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import (
    LEG_DISPATCH,
    LEG_REFUSAL_HEAD,
    LEG_REFUSE,
    RESUME_LEDGER_LEG_FIELD,
)
from coordinator_core.ops.dispatch_emit.ledger_remaining_paths import remaining_leg_plan
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_OWED = {"R1": {"remaining": ["do the thing"]}}


def _script(monkeypatch, arm: str) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: arm)
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


def _leg(arm: str, tmp_path) -> dict:
    return remaining_leg_plan(_OWED, ["please finish the work"], [], ["R1"], [], None, tmp_path, arm=arm)


def test_xs_refire_with_empty_writes_leg_refuses_and_a_non_xs_arm_routes_to_replan(tmp_path):
    xs = _leg("xs", tmp_path)
    assert xs["action"] == LEG_REFUSE and xs["writes"] == []
    other = _leg("m_plus", tmp_path)
    assert other["action"] == LEG_DISPATCH and other["writes"] == []


def test_composed_xs_workflow_halts_on_the_refusal_before_any_ledger_remaining_dispatch(monkeypatch, tmp_path):
    assert _leg("xs", tmp_path)["action"] == LEG_REFUSE
    script = _script(monkeypatch, "xs")
    leg = script.index(f".{RESUME_LEDGER_LEG_FIELD} ?? null;")
    refuse = script.index(f"_leg.action === '{LEG_REFUSE}'", leg)
    assert LEG_REFUSAL_HEAD in script[refuse : refuse + 400]
    assert "_halted = {" in script[refuse : refuse + 200]
    # The dispatch is only reachable through the `else if` chain after the refuse branch.
    dispatch = script.index("label: 'ledger-remaining'", refuse)
    assert refuse < script.index("else if (_leg && _leg.action ===", refuse) < dispatch
    assert script.count("label: 'ledger-remaining'") == 1


def test_the_dispatch_never_runs_under_a_halt(monkeypatch):
    script = _script(monkeypatch, "xs")
    assert "if (_resumeLedger && !_ledgerExec &&" in script
    assert "if (!_halted" in script
