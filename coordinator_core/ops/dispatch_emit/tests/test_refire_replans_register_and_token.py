"""Incident ask-20261022T023251-17c2e0: a re-fire after a ledger-check halt plus workflow.revise planned
only the rows the halt had work recorded for, leaving revise-minted register rows unclaimed; and a re-fire
whose run token the halt had already ended discovered it only at the final commit.

(a) The ledger-resume verdict names the register rows the plan's register_claims omit, the composed script
    forces the re-plan leg on them, and the re-plan's stage claims and re-pins them.
(b) The gate refuses a re-fire with no live run token before any executor spend, naming the fire call
    that mints one.
"""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import ask_compose, ask_gate, run_token
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.ops.dispatch_emit.ask_stage import stage
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL
from coordinator_core.ops.dispatch_emit.tests.test_refire_ledger_halted_sizing_resumes import (
    RUN,
    _git,
    _halted_repo,
    _pointer,
)
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import PLAN as STAGE_PLAN
from coordinator_core.warp_fire import fire_command

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_ROWS = [
    {"id": "R1", "status": "open", "source_text": "first words"},
    {"id": "R2", "status": "open", "source_text": "second words"},
    {"id": "R3", "status": "open", "source_text": "third words"},
]
_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


def _claim_plan(repo, claimed):
    """The halted plan, now carrying a register_claims block over `claimed`."""
    body = (repo / PLAN_REL).read_text(encoding="utf-8")
    claims = f"register_claims:\n  sizing: {REL}\n  rows: [{', '.join(claimed)}]\n"
    (repo / PLAN_REL).write_text(body.replace("---\n", f"---\n{claims}", 1), encoding="utf-8", newline="\n")


def _verdict(repo, claimed):
    _halted_repo(repo)
    _put(repo, tshirt="S", route="spec-dispatch", plan=PLAN_REL, requirement_register={"rows": _ROWS})
    _claim_plan(repo, claimed)
    _pointer(repo)
    return gate(repo, REL)


def _resume(repo, claimed):
    verdict = _verdict(repo, claimed)
    assert verdict.halt is None and verdict.resume_ledger is not None
    return verdict.resume_ledger


def test_the_s_arm_refuses_a_refire_whose_plan_omits_register_rows_naming_them(repo):  # noqa: F811
    halt = _verdict(repo, ["R1"]).halt
    assert halt is not None and "omits register row(s) R2, R3" in halt["reason"]
    assert "arm s has no re-plan leg" in halt["reason"]


def test_resume_ledger_names_none_when_the_plan_claims_every_row(repo):  # noqa: F811
    assert _resume(repo, ["R1", "R2", "R3"]).get("unclaimed_rows", []) == []


def _compose(monkeypatch) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    return compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel=REL, run_id="run-1", session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        script_path="x.mjs", in_run_commit=True, **REVIEW_KW,
    )


def test_unclaimed_rows_force_the_replan_leg_and_the_stage_claims_them(monkeypatch):
    script = _compose(monkeypatch)
    owes = script.index("const _ledgerOwes")
    assert "_gate.resume_ledger.unclaimed_rows ?? []).length > 0" in script[owes:owes + 700]
    leg = script.index("label: 'ledger-replan'")
    prompt = script[leg - 5200:leg]
    assert "_gate.resume_ledger.unclaimed_rows ?? []" in prompt
    assert "claim_register_rows: true" in prompt


def _stage_repo(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    sizing = {"requirement_register": {"sources": [{"kind": "pm"}], "rows": _ROWS}}
    (tmp_path / REL).write_text(yaml.safe_dump(sizing), encoding="utf-8")
    claims = (
        f"register_claims:\n  sizing: {REL}\n  rows: [R1]\n"
        f"  pins:\n    R1: '{rr.row_pin('first words')}'\n"
    )
    plan = STAGE_PLAN.replace("---\n#", f"{claims}---\n#", 1)
    (tmp_path / PLAN_REL).write_text(plan, encoding="utf-8", newline="\n")
    return tmp_path


def test_stage_claims_and_pins_the_omitted_rows_when_asked(tmp_path):
    root = _stage_repo(tmp_path)
    stage(root, run_id="r1", plan_rel=PLAN_REL, claim_register_rows=True)
    text = (root / PLAN_REL).read_text(encoding="utf-8")
    fm = split_frontmatter(text).fm_text
    sizing_rel, ids = rr.plan_claims(fm)
    assert sizing_rel == REL and ids == {"R1", "R2", "R3"}
    assert set(rr.plan_pins(fm)) == {"R1", "R2", "R3"}
    assert rr.plan_pin_drift(fm, root) == []
    assert "id: C3" in text


def test_stage_leaves_the_claims_alone_by_default(tmp_path):
    root = _stage_repo(tmp_path)
    before = (root / PLAN_REL).read_text(encoding="utf-8")
    stage(root, run_id="r1", plan_rel=PLAN_REL)
    assert (root / PLAN_REL).read_text(encoding="utf-8") == before


def _token_repo(repo):  # noqa: F811
    _git(repo, "init", "-q")
    _put(repo, tshirt="S", route="spec-dispatch")
    return repo


def test_refire_without_a_live_token_halts_at_the_gate_naming_the_fire_call(repo):  # noqa: F811
    _token_repo(repo)
    out = ask_gate._handler({"sizing_path": REL, "run_id": RUN, "require_live_token": True}, repo)
    assert out["arm"] is None and out["halt"]["kind"] == "refusal"
    assert "run token" in out["halt"]["reason"] and fire_command(REL) in out["halt"]["reason"]


def test_refire_with_a_live_token_passes_the_gate(repo):  # noqa: F811
    _token_repo(repo)
    run_token.mint(repo, RUN, "x.mjs")
    out = ask_gate._handler({"sizing_path": REL, "run_id": RUN, "require_live_token": True}, repo)
    assert out["halt"] is None


def test_the_composed_script_asks_the_gate_for_the_token_check_only_when_it_holds_a_token(monkeypatch):
    script = _compose(monkeypatch)
    assert "...(_RUN_TOKEN ? { require_live_token: true } : {})" in script
