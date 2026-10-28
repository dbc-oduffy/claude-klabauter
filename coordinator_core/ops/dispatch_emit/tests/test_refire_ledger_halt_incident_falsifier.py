"""The incident falsifier, rebuilt under tmp_path: a re-fire of the ledger-halted sizing keeps the met
rows out of blocking, carries R3's remaining work, and composes a script that keeps arm m_plus and
re-plans with the owed work instead of taking the xs tail."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, ask_gate, sizing_fire
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import RESUME_LEDGER_CARRIED_MET_FIELD
from coordinator_core.ops.dispatch_emit.sizing_fire import load_sizing, resolve_arm
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL
from coordinator_core.ops.dispatch_emit.tests.test_refire_carries_met_rows_across_halts import (
    R3_ITEM,
    SECOND,
    _chain,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


def _compose(monkeypatch, arm: str) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: arm)
    return compose_ask_script(
        repo_root="REPO",
        prompt=None,
        sizing_rel=REL,
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        script_path="x.mjs",
        in_run_commit=True,
        **REVIEW_KW,
    )


def test_the_falsifier_read_prints_the_expected_when_true_gate_half(repo):  # noqa: F811
    _chain(repo)
    sizing = load_sizing(repo, REL)
    arm = resolve_arm(sizing)
    plan = sizing_fire.resumable_plan(sizing, REL, repo, arm)
    assert plan.ledger.run_id == SECOND
    assert plan.ledger.blocking == ("R3",)
    assert set(plan.ledger.carried_met) == {"T2", "R2"}
    assert plan.ledger.remaining["R3"]["remaining"]


def test_the_gate_verdict_and_composed_script_dispatch_r3s_work_at_m_plus(repo, monkeypatch):  # noqa: F811
    _chain(repo)
    resume = ask_gate._handler({"sizing_path": REL, "run_id": "warp-2026-10-17-refire-20261017T120000-aaaaaa"}, repo)[
        "resume_ledger"
    ]
    assert resume["plan"] == PLAN_REL
    assert resume["blocking"] == ["R3"]
    assert set(resume[RESUME_LEDGER_CARRIED_MET_FIELD]) == {"T2", "R2"}
    assert resume["remaining"]["R3"]["remaining"] == R3_ITEM["remaining"]

    script = _compose(monkeypatch, "m_plus")
    owes = script.index("const _ledgerOwes")
    assert "_gate.arm === 'm_plus'" in script[owes : owes + 200]
    assert ".remaining ?? []).length > 0" in script[owes : owes + 400]
    assert "else if (!_ledgerOwes) { _xsTail = true; }" in script
    leg = script.index("label: 'ledger-replan'")
    prompt = script[script.index("Ledger-resume re-plan leg.") : leg]
    assert "(_gate.resume_ledger.blocking ?? []).join(', ')" in prompt
    assert "_gate.resume_ledger.remaining ?? {}" in prompt
    assert "label: 'ledger-restage'" not in script
    assert owes < leg < script.index("label: 'verify'")
    assert "to the ids of exactly the rows you added" in prompt and "only_rows" in prompt


def test_the_xs_baseline_is_gone_from_the_composed_script(monkeypatch):
    script = _compose(monkeypatch, "m_plus")
    assert "_planRel = _gate.resume_ledger.plan; _gate = { ..._gate, arm: 'xs' }; }" not in script
