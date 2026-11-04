"""ask_gate splits a research sizing that names writes into a ready research run and an after-held build run."""

from __future__ import annotations

import json

import pytest
import yaml

from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.warp_fire import fire_command

REL = "state/sizings/2026-10-29-split-gate.yaml"
RESEARCH = {"value_class": "scouts", "appetite": "small", "sources": ["web"], "questions": ["why?"]}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.delenv("DELIVERABLE_ID", raising=False)
    return tmp_path


def _put(repo, research):
    doc = {
        "schema": "sizing-object",
        "name": "split fixture",
        "intent": "research then write",
        "estimate": {"tshirt": "S", "provisional": True},
        "route": "spec-dispatch",
        "detents": [],
        "fork": None,
        "xl_exit": None,
        "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "deliverable_id": "dlv-fixture-abc123",
        "interaction_mode": "pm",
        "exit_criterion": {"statement": "done", "accepted": None},
        "research": research,
    }
    (repo / REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")


def test_gate_default_entry_yields_ready_research_and_held_build(repo):
    _put(repo, RESEARCH)
    v = gate(repo, REL, writes=["docs/a.md", "docs/b.md"])
    assert v.arm is None and v.halt["kind"] == "research"
    split = v.halt["split"]
    r, b = split["research"], split["build"]
    assert (r["status"], b["status"]) == ("ready", "after")
    assert b["after"] == [r["run_id"]]
    assert v.halt["fire"] == [fire_command(r["sizing"])]
    research_doc = yaml.safe_load((repo / r["sizing"]).read_text(encoding="utf-8"))
    build_doc = yaml.safe_load((repo / b["sizing"]).read_text(encoding="utf-8"))
    assert "research" in research_doc and "writes" not in research_doc
    assert "research" not in build_doc and build_doc["writes"] == ["docs/a.md", "docs/b.md"]
    from coordinator_core.ops.dispatch_emit import ask_fanout_contract as contract

    parent = json.loads((repo / contract.parent_record_rel(split["parent_id"])).read_text(encoding="utf-8"))
    by_id = {x["run_id"]: x for x in parent["runs"]}
    assert by_id[b["run_id"]]["after"] == [r["run_id"]]
    assert by_id[b["run_id"]]["status"] == "after" and by_id[r["run_id"]]["status"] == "ready"


def test_notebooklm_without_targets_or_questions_halts_naming_them(repo):
    _put(repo, {"value_class": "scouts", "appetite": "small", "sources": ["notebooklm"]})
    v = gate(repo, REL, writes=["docs/a.md"])
    assert v.arm is None and v.halt["kind"] == "research"
    assert "notebooklm targets" in v.halt["reason"] and "split" not in v.halt
    assert [p.name for p in (repo / "state" / "sizings").iterdir()] == ["2026-10-29-split-gate.yaml"]


def test_notebooklm_targets_derive_from_questions(repo):
    _put(repo, {**RESEARCH, "sources": ["notebooklm"], "questions": ["topic a", "topic b"]})
    v = gate(repo, REL, writes=["docs/a.md"])
    r = v.halt["split"]["research"]
    doc = yaml.safe_load((repo / r["sizing"]).read_text(encoding="utf-8"))
    assert [t["ref"] for t in doc["research"]["targets"]] == ["topic a", "topic b"]


def test_fanout_refusal_leaves_no_derived_sizings(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import ask_fanout

    monkeypatch.setattr(ask_fanout, "fanout", lambda params, root: {"exit_code": 2, "error": "refused"})
    _put(repo, RESEARCH)
    v = gate(repo, REL, writes=["docs/a.md"])
    assert v.arm is None and "split" not in v.halt
    assert [p.name for p in (repo / "state" / "sizings").iterdir()] == ["2026-10-29-split-gate.yaml"]


def test_each_slice_cites_its_own_month_dir_plan_path(repo):
    _put(repo, RESEARCH)
    split = gate(repo, REL, writes=["docs/a.md"]).halt["split"]
    for key in ("research", "build"):
        rel = split[key]["sizing"]
        doc = yaml.safe_load((repo / rel).read_text(encoding="utf-8"))
        stem = rel.rsplit("/", 1)[-1][: -len(".yaml")]
        assert doc["plan"] == f"docs/plans/2026-10/{stem}.md"
    build = yaml.safe_load((repo / split["build"]["sizing"]).read_text(encoding="utf-8"))
    assert build["route"] == "plan"


def test_a_parent_plan_edge_is_kept_on_both_slices(repo):
    _put(repo, RESEARCH)
    doc = yaml.safe_load((repo / REL).read_text(encoding="utf-8"))
    doc["plan"] = "docs/plans/2026-10/2026-10-29-parent.md"
    (repo / REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    split = gate(repo, REL, writes=["docs/a.md"]).halt["split"]
    for key in ("research", "build"):
        got = yaml.safe_load((repo / split[key]["sizing"]).read_text(encoding="utf-8"))
        assert got["plan"] == "docs/plans/2026-10/2026-10-29-parent.md"


SHA = "c" * 40


def _status(repo, run_id):
    from coordinator_core.ops.dispatch_emit import ask_fanout_contract as contract

    p = repo / contract.status_rel(run_id)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _refire(repo, split, status):
    stem = split["research"]["sizing"].rsplit("/", 1)[-1][: -len(".yaml")]
    rid = f"warp-{stem}-20261104T010000-bbbbbb"
    d = repo / "state" / "warp" / rid
    d.mkdir(parents=True)
    (d / "halted.json").write_text(json.dumps({"status": status, "sha": SHA}), encoding="utf-8")
    return rid


def test_gating_the_build_slice_credits_the_research_slice_a_landed_refire_ran(repo):
    from coordinator_core.ops.dispatch_emit.ask_gate import _handler

    _put(repo, RESEARCH)
    split = gate(repo, REL, writes=["docs/a.md"]).halt["split"]
    _refire(repo, split, "landed")
    assert (_status(repo, split["research"]["run_id"]) or {}).get("status") != "landed"
    _handler({"sizing_path": split["build"]["sizing"]}, repo)
    got = _status(repo, split["research"]["run_id"])
    assert got["status"] == "landed" and got["sha"] == SHA and got["parent_id"] == split["parent_id"]


def test_a_refire_that_did_not_land_credits_nothing(repo):
    from coordinator_core.ops.dispatch_emit.ask_gate import _handler

    _put(repo, RESEARCH)
    split = gate(repo, REL, writes=["docs/a.md"]).halt["split"]
    before = _status(repo, split["research"]["run_id"])
    _refire(repo, split, "halted")
    _handler({"sizing_path": split["build"]["sizing"]}, repo)
    assert _status(repo, split["research"]["run_id"]) == before
