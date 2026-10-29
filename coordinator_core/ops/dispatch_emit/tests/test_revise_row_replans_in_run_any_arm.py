"""A register row a revise minted after the plan was written is claimed and re-planned in-run on every arm."""

from __future__ import annotations

import subprocess

import pytest
import yaml

from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import ask_compose, ask_stage, requirements_ledger as rl, run_token
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import REGISTER_ROWS_UNCLAIMED_REFUSAL
from coordinator_core.ops.dispatch_emit.ask_stage_blocks import AskStageError
from coordinator_core.ops.dispatch_emit.halt_remedy_class import MAX_IN_RUN_REMEDY_ATTEMPTS
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import SIZING_REL, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_refire_replans_register_and_token import _ROWS, _stage_repo
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process]

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


def _compose(monkeypatch, arm) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: arm)
    return compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel=REL, run_id="run-1", session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        script_path="x.mjs", in_run_commit=True, **REVIEW_KW,
    )


@pytest.mark.parametrize("tshirt", ["S", "M"])
def test_live_revise_adding_a_row_is_neither_refused_nor_told_to_resize(repo, monkeypatch, tshirt):  # noqa: F811
    doc = {
        "schema": "sizing-object", "name": "fixture", "intent": "Fix the thing",
        "estimate": {"tshirt": tshirt, "provisional": True}, "route": "spec-dispatch",
        "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 0, "text": "fix the thing"}],
        "exit_criterion": {"statement": "it works", "accepted": None},
        "deliverable_id": "dlv-xs-abc123",
    }
    (repo / SIZING_REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())
    rl.start(repo, "ra1")
    rl.bind(repo, "ra1", SIZING_REL)
    monkeypatch.setattr(run_token, "is_live", lambda *_a, **_k: True)
    out = rl._handler({"run_id": "ra1", "verbatim": "also refuse undeclared writes"}, repo_root=repo)
    assert out["ok"] is True and out["refused"] == []
    assert "resize" not in out and "no re-plan leg" not in out.get("warning", "")


@pytest.mark.parametrize("arm", ["s", "xs", "m_plus"])
def test_the_replan_leg_runs_on_unclaimed_rows_on_every_arm_and_is_bounded(monkeypatch, arm):
    script = _compose(monkeypatch, arm)
    owes = script.index("const _ledgerOwes")
    owes_expr = script[owes:script.index(";", owes)]
    unclaimed = "unclaimed_rows ?? []).length > 0"
    assert unclaimed in owes_expr
    # The unclaimed clause sits outside the M+-only conjunction.
    assert owes_expr.index(unclaimed) > owes_expr.index("'m_plus'") and "'m_plus'" in owes_expr
    assert "claim_register_rows: true" in script
    assert f"_try < {MAX_IN_RUN_REMEDY_ATTEMPTS}" in script
    assert REGISTER_ROWS_UNCLAIMED_REFUSAL in script


def _fm_with_sizing_object(root):
    path = root / PLAN_REL
    path.write_text(path.read_text(encoding="utf-8").replace("---\n", f"---\nsizing_object: {REL}\n", 1), encoding="utf-8", newline="\n")
    return split_frontmatter(path.read_text(encoding="utf-8")).fm_text


def _run_sizing():
    return {"requirement_register": {"sources": [{"kind": "pm"}], "rows": _ROWS}}


def test_stage_claims_the_omitted_rows_in_run_instead_of_refusing(tmp_path):
    root = _stage_repo(tmp_path)
    plan = root / PLAN_REL
    fm = _fm_with_sizing_object(root)
    assert ask_stage._missing_register_rows(fm, REL, _run_sizing())[1] == ["R2", "R3"]
    ask_stage._settle_unclaimed_register_rows(root, plan, fm, PLAN_REL, REL, _run_sizing())
    assert rr.plan_claims(split_frontmatter(plan.read_text(encoding="utf-8")).fm_text)[1] == {"R1", "R2", "R3"}


def test_stage_halts_register_rows_unclaimed_only_once_the_attempts_are_spent(tmp_path, monkeypatch):
    root = _stage_repo(tmp_path)
    calls = []
    monkeypatch.setattr(ask_stage, "_claim_register_rows", lambda *_a: calls.append(1))
    with pytest.raises(AskStageError, match=REGISTER_ROWS_UNCLAIMED_REFUSAL):
        ask_stage._settle_unclaimed_register_rows(
            root, root / PLAN_REL, _fm_with_sizing_object(root), PLAN_REL, REL, _run_sizing()
        )
    assert len(calls) == MAX_IN_RUN_REMEDY_ATTEMPTS


def _gate(register_class):
    return {"plan": "p.md", "verdict": "NOT-PREPPED", "withheld_rows": [], "message": "",
            "classes": {"REGISTER": register_class}}


def test_readiness_routes_unclaimed_register_rows_to_the_in_run_replan_not_a_judgment_pull():
    from coordinator_core.ops.plan_readiness_verdict import readiness_verdict

    out = readiness_verdict(_gate({
        "status": "FAIL", "kind": "register-rows-unclaimed", "mechanical": False, "repair": None,
        "detail": "register_claims.rows omit 2 row(s) of s: R2, R3",
    }), None, [])
    assert out["pull_class"] == "mechanical" and "needs judgment" not in out["reason"]
    assert out["mechanical_repairs"][0]["kind"] == "register-rows-unclaimed"
    assert "R2, R3" in out["mechanical_repairs"][0]["instruction"]


def test_readiness_still_pulls_other_register_defects_for_judgment():
    from coordinator_core.ops.plan_readiness_verdict import readiness_verdict

    out = readiness_verdict(_gate({"status": "FAIL", "kind": "register-pin-missing", "mechanical": False, "repair": None}), None, [])
    assert out["pull_class"] == "substantive" and "needs judgment" in out["reason"]
