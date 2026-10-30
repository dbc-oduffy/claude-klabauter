"""A refire of a plan with rows still open whose register_claims omit register rows resumes through the
ledger re-plan leg (`UNCLAIMED_ROWS_HALT_KIND`), never as a plain `resume_plan` that restages the stale plan.
"""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import UNCLAIMED_ROWS_HALT_KIND
from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.ops.dispatch_emit.refire_noop import identical_rehalt_reason
from coordinator_core.ops.dispatch_emit.sizing_fire import resumable_plan
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import ACCEPTED, REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL, _plan
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_ROWS = [
    {"id": "R1", "status": "open", "source_text": "first words", "surface": "internal"},
    {"id": "R2", "status": "open", "source_text": "second words", "surface": "internal"},
]
_REGISTER = {"sources": [{"doc": "ask", "ref": "ask"}], "rows": _ROWS}


def _claims(repo, claimed):
    _plan(repo)
    body = (repo / PLAN_REL).read_text(encoding="utf-8")
    block = f"register_claims:\n  sizing: {REL}\n  rows: [{', '.join(claimed)}]\n"
    (repo / PLAN_REL).write_text(body.replace("---\n", f"---\n{block}", 1), encoding="utf-8", newline="\n")


def _sizing(repo, rows=_ROWS, **over):
    subprocess.run(["git", "init", "-q", str(repo)], capture_output=True, **no_console_creationflags())
    _plan(repo)
    _claims(repo, ["R1"])
    return {"plan": PLAN_REL, "requirement_register": {"rows": rows}, **over}


def test_open_rows_with_an_omitted_register_row_resume_as_an_unclaimed_rows_ledger(repo):  # noqa: F811
    sizing = _sizing(repo)
    plan = resumable_plan(sizing, REL, repo, "m_plus")
    assert plan.incomplete == ()
    assert plan.ledger.halted == UNCLAIMED_ROWS_HALT_KIND
    assert plan.ledger.unlanded == ("A1",)
    assert plan.ledger.plan_rel == PLAN_REL and plan.ledger.sizing_rel == REL


def test_a_plan_claiming_every_row_keeps_the_plain_resume(repo):  # noqa: F811
    sizing = _sizing(repo)
    _claims(repo, ["R1", "R2"])
    plan = resumable_plan(sizing, REL, repo, "m_plus")
    assert plan.ledger is None and plan.incomplete == ("A1",)


def test_a_ruled_out_omitted_row_does_not_resume_through_the_replan(repo):  # noqa: F811
    ruled = {**_ROWS[1], "status": "deferred", "ruling": {"ref": "workflow.revise:run-1:out_of_scope"}}
    sizing = _sizing(repo, rows=[_ROWS[0], ruled])
    plan = resumable_plan(sizing, REL, repo, "m_plus")
    assert plan.ledger is None


def test_a_plan_claiming_another_sizing_is_not_judged(repo):  # noqa: F811
    sizing = _sizing(repo)
    body = (repo / PLAN_REL).read_text(encoding="utf-8")
    (repo / PLAN_REL).write_text(body.replace(f"sizing: {REL}", "sizing: state/sizings/other.yaml"), encoding="utf-8", newline="\n")
    assert resumable_plan(sizing, REL, repo, "m_plus").ledger is None


def test_gate_at_m_plus_resumes_the_ledger_with_the_unclaimed_rows(repo):  # noqa: F811
    _sizing(repo)
    _put(repo, tshirt="M", route="plan", accepted=ACCEPTED, plan=PLAN_REL, requirement_register=_REGISTER)
    v = gate(repo, REL)
    assert v.halt is None and v.arm == "m_plus"
    assert v.resume_plan is None
    ledger = v.resume_ledger
    assert ledger["kind"] == UNCLAIMED_ROWS_HALT_KIND
    assert ledger["unclaimed_rows"] == ["R2"]
    assert ledger["unlanded"] == ["A1"]


def test_gate_below_m_plus_arms_a_resume_ledger_naming_the_rows(repo):  # noqa: F811
    _sizing(repo)
    _put(repo, tshirt="S", route="spec-dispatch", plan=PLAN_REL, requirement_register={"rows": _ROWS})
    v = gate(repo, REL)
    assert v.arm == "s" and v.halt is None
    ledger = v.resume_ledger
    assert ledger["kind"] == UNCLAIMED_ROWS_HALT_KIND and ledger["unclaimed_rows"] == ["R2"]


def test_gate_below_m_plus_with_every_row_claimed_does_not_refuse(repo):  # noqa: F811
    _sizing(repo)
    _claims(repo, ["R1", "R2"])
    _put(repo, tshirt="S", route="spec-dispatch", plan=PLAN_REL, requirement_register={"rows": _ROWS})
    v = gate(repo, REL)
    assert v.halt is None and v.resume_plan == PLAN_REL


def test_the_composed_script_runs_the_replan_leg_with_claim_register_rows(monkeypatch):
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    script = compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel=REL, run_id="run-1", session_id=None,
        wrap_stage=lambda _t: ("  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]),
        plan_blitz_text="stub", script_path="x.mjs", in_run_commit=True, **REVIEW_KW,
    )
    assert "ledger-replan" in script and "claim_register_rows: true" in script


def test_an_unclaimed_rows_resume_is_never_a_noop():
    ledger = {
        "kind": UNCLAIMED_ROWS_HALT_KIND, "blocking": ["R1"], "unclaimed_rows": ["R2"], "halt_sha": "abc",
    }
    assert identical_rehalt_reason(ledger, [], [], [], [], None) is None
    ledger["unclaimed_rows"] = []
    assert identical_rehalt_reason(ledger, [], [], [], [], None) is not None
