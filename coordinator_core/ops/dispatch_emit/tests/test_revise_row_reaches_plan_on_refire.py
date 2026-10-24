"""workflow.revise on a finished or halted run whose plan omits register rows: the reply is an engine refire
carrying `replans`, never a plan-author hand dispatch."""

from __future__ import annotations

import yaml

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.warp_fire import fire_command

RUN = "ask-20261015T000000-abc123"
REL = "state/sizings/2026-10-15-revise.yaml"
PLAN = "docs/plans/2026-10-15-revise.md"


def _repo(tmp_path, *, plan_edge, tshirt="M"):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    sizing = {
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": tshirt, "provisional": True},
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


def _assert_no_hand_dispatch(out):
    assert "then" not in out and "then" not in out["next"]
    assert "Dispatch" not in out["note"] and "plan-author" not in out["note"]


def test_omitted_row_replies_with_an_engine_refire_that_replans_it(tmp_path):
    out = _revise(_repo(tmp_path, plan_edge=True))
    assert out["item"] == "R4"
    assert out["next"] == {"kind": "refire", "call": fire_command(REL), "replans": {"plan": PLAN, "rows": ["R4"]}}
    _assert_no_hand_dispatch(out)
    assert out["note"].startswith("run finished:") and PLAN in out["note"] and "R4" in out["note"]
    assert "re-plan leg" in out["note"]


def test_non_m_plus_arm_names_the_refusal_and_the_resize(tmp_path):
    out = _revise(_repo(tmp_path, plan_edge=True, tshirt="S"))
    assert out["next"] == {"kind": "refire", "call": fire_command(REL), "replans": {"plan": PLAN, "rows": ["R4"]}}
    _assert_no_hand_dispatch(out)
    assert "REGISTER-ROWS-UNCLAIMED" in out["note"] and "M+" in out["note"]


def test_halted_run_words_the_note_as_halted(tmp_path):
    repo = _repo(tmp_path, plan_edge=True)
    out = rl._refire(REL, repo, (PLAN, ["R4"]), halted=True)
    assert out["note"].startswith("run halted:")
    assert out["next"]["replans"] == {"plan": PLAN, "rows": ["R4"]}
    _assert_no_hand_dispatch(out)


def test_warning_names_the_engine_route(tmp_path):
    out = _revise(_repo(tmp_path, plan_edge=True))
    assert "Re-plan against the register" not in out["warning"]
    assert "refire after this run ends re-plans" in out["warning"]


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
