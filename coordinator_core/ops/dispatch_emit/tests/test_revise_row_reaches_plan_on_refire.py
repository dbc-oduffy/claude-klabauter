"""workflow.revise on a finished run whose sizing routes to a plan: the refire reply names the plan
revision when the plan's register_claims omit the new row, instead of a bare fire of the stale plan."""

from __future__ import annotations

import yaml

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.warp_fire import fire_command

RUN = "ask-20261015T000000-abc123"
REL = "state/sizings/2026-10-15-revise.yaml"
PLAN = "docs/plans/2026-10-15-revise.md"


def _repo(tmp_path, *, plan_edge):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    sizing = {
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": "M", "provisional": True},
        "route": "plan", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "build the op"}, {"turn": 2, "text": "keep it fast"}],
    }
    if plan_edge:
        sizing["plan"] = PLAN
    (tmp_path / REL).write_text(yaml.safe_dump(sizing, sort_keys=False), encoding="utf-8")
    fm = {"title": "p", "register_claims": {"sizing": REL, "rows": ["R1", "R2", "R3"]}}
    (tmp_path / PLAN).write_text("---\n" + yaml.safe_dump(fm) + "---\n# p\n", encoding="utf-8")
    rl.start(tmp_path, RUN)
    rl.bind(tmp_path, RUN, REL, ask_text="a raw ask")
    return tmp_path


def _revise(root):
    return rl._handler({"run_id": RUN, "verbatim": "also cover Windows"}, repo_root=root)


def test_refire_names_the_plan_revision_when_the_plan_omits_the_new_row(tmp_path):
    out = _revise(_repo(tmp_path, plan_edge=True))
    assert out["item"] == "R4"
    assert out["next"]["kind"] == "revise-plan" and out["next"]["rows"] == ["R4"]
    assert out["next"]["plan"] == PLAN and "call" not in out["next"]
    assert out["next"]["then"] == fire_command(REL)
    assert PLAN in out["note"] and "R4" in out["note"]


def test_revise_plan_reply_names_the_sizing_the_plan_author_prompt_must_bind(tmp_path):
    out = _revise(_repo(tmp_path, plan_edge=True))
    assert out["next"]["sizing"] == REL
    assert f"{REL} named in its prompt" in out["note"]


def test_refire_is_the_bare_fire_when_the_plan_claims_every_row(tmp_path):
    repo = _repo(tmp_path, plan_edge=True)
    plan = repo / PLAN
    plan.write_text(plan.read_text(encoding="utf-8").replace("- R3", "- R3\n  - R4"), encoding="utf-8")
    out = _revise(repo)
    assert out["next"] == {"kind": "refire", "call": fire_command(REL)}


def test_ask_route_without_a_plan_edge_keeps_the_bare_refire(tmp_path):
    out = _revise(_repo(tmp_path, plan_edge=False))
    assert out["next"] == {"kind": "refire", "call": fire_command(REL)}
    assert (tmp_path / RECORD_DIR_ROOT / RUN).is_dir()
