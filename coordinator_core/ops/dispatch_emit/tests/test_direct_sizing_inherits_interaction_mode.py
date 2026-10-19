"""ask_gate: a sizing with no interaction_mode inherits a sibling's through its ask_parent marker."""

from __future__ import annotations

import json

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import ask_fanout_contract as fc
from coordinator_core.ops.dispatch_emit import sizing_fire as sf
from coordinator_core.ops.dispatch_emit.ask_gate import gate

PARENT = "ask-parent-20261019T000000-aaaaaa"
RUN_ME = "ask-20261019T000000-111111"
RUN_SIB = "ask-20261019T000000-222222"
ME = "state/sizings/2026-10-19-me.yaml"
SIB = "state/sizings/2026-10-19-sib.yaml"
ACCEPTED = {"pm_quote": "yes", "on": "2026-10-19", "mode": "pm"}


def _doc(**over):
    doc = {
        "schema": "sizing-object",
        "name": "fixture",
        "intent": "exercise the gate",
        "estimate": {"tshirt": "S", "provisional": True},
        "route": "spec-dispatch",
        "detents": [],
        "fork": None,
        "xl_exit": None,
        "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "deliverable_id": "dlv-fixture-abc123",
        "exit_criterion": {"statement": "done", "accepted": ACCEPTED},
        "em_analysis": {"ask_parent": fc.ask_parent_marker(PARENT, RUN_ME)},
    }
    doc.update(over)
    return {k: v for k, v in doc.items() if v is not None}


def _write(repo, rel, doc):
    (repo / rel).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")


def _parent(repo, rel=None):
    runs = (fc.SiblingRun(RUN_ME, "me", slice_sizing=ME), fc.SiblingRun(RUN_SIB, "sib", slice_sizing=SIB))
    path = repo / (rel or fc.parent_record_rel(PARENT))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fc.ParentRecord(PARENT, runs).to_json()), encoding="utf-8")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.delenv("DELIVERABLE_ID", raising=False)
    monkeypatch.setattr(sf, "_fleet_interaction_mode", lambda: None)
    return tmp_path


def _absent(reason):
    return "interaction_mode" in reason


def test_sibling_mode_is_inherited_and_file_is_not_rewritten(repo):
    _write(repo, ME, _doc())
    _write(repo, SIB, _doc(interaction_mode="pm"))
    _parent(repo)
    before = (repo / ME).read_bytes()
    v = gate(repo, ME)
    assert v.halt is None and v.arm == "s"
    assert (repo / ME).read_bytes() == before


def test_tracked_run_copy_of_parent_record_is_read(repo):
    _write(repo, ME, _doc())
    _write(repo, SIB, _doc(interaction_mode="pm"))
    _parent(repo, fc.run_parent_copy_rel(RUN_ME))
    assert gate(repo, ME).halt is None


def test_no_resolvable_parent_still_refuses(repo):
    _write(repo, ME, _doc())
    v = gate(repo, ME)
    assert v.halt["kind"] == "refusal" and _absent(v.halt["reason"])


def test_no_marker_still_refuses(repo):
    _write(repo, ME, _doc(em_analysis=None))
    v = gate(repo, ME)
    assert v.halt["kind"] == "refusal" and _absent(v.halt["reason"])


def test_siblings_without_a_mode_still_refuse(repo):
    _write(repo, ME, _doc())
    _write(repo, SIB, _doc())
    _parent(repo)
    v = gate(repo, ME)
    assert v.halt["kind"] == "refusal" and _absent(v.halt["reason"])


def test_own_mode_is_kept_and_parent_never_overrides(repo):
    _write(repo, ME, _doc(interaction_mode="ceo"))
    _write(repo, SIB, _doc(interaction_mode="pm"))
    _parent(repo)
    from coordinator_core.ops.dispatch_emit.sizing_fire import inherited_interaction_mode, load_sizing

    assert inherited_interaction_mode(load_sizing(repo, ME), repo) == "pm"
    assert gate(repo, ME).halt is None
    assert yaml.safe_load((repo / ME).read_text(encoding="utf-8"))["interaction_mode"] == "ceo"


def test_load_sizing_effective_is_the_one_seam_for_every_reader(repo):
    from coordinator_core.ops.dispatch_emit.sizing_fire import load_sizing, load_sizing_effective

    _write(repo, ME, _doc())
    _write(repo, SIB, _doc(interaction_mode="ceo"))
    _parent(repo)
    assert load_sizing_effective(repo, ME)["interaction_mode"] == "ceo"
    assert "interaction_mode" not in load_sizing(repo, ME)


def test_load_sizing_effective_keeps_own_mode_and_stays_absent_without_parent(repo):
    from coordinator_core.ops.dispatch_emit.sizing_fire import load_sizing_effective

    _write(repo, ME, _doc(interaction_mode="pm"))
    _write(repo, SIB, _doc(interaction_mode="ceo"))
    _parent(repo)
    assert load_sizing_effective(repo, ME)["interaction_mode"] == "pm"
    _write(repo, ME, _doc(em_analysis={}))
    assert "interaction_mode" not in load_sizing_effective(repo, ME)


def test_fleet_mode_is_the_final_fallback_and_file_is_not_rewritten(repo, monkeypatch):
    monkeypatch.setattr(sf, "_fleet_interaction_mode", lambda: "ceo")
    _write(repo, ME, _doc(em_analysis={}))
    before = (repo / ME).read_bytes()
    assert sf.load_sizing_effective(repo, ME)["interaction_mode"] == "ceo"
    assert (repo / ME).read_bytes() == before


def test_fleet_mode_never_overrides_recorded_or_inherited_mode(repo, monkeypatch):
    monkeypatch.setattr(sf, "_fleet_interaction_mode", lambda: "ceo")
    _write(repo, ME, _doc(interaction_mode="pm"))
    assert sf.load_sizing_effective(repo, ME)["interaction_mode"] == "pm"
    _write(repo, ME, _doc())
    _write(repo, SIB, _doc(interaction_mode="pm"))
    _parent(repo)
    assert sf.load_sizing_effective(repo, ME)["interaction_mode"] == "pm"


def test_real_fleet_resolution_yields_a_mode(repo, monkeypatch):
    monkeypatch.undo()
    assert isinstance(sf._fleet_interaction_mode(), str)


def test_xl_exit_reader_and_roadmap_mode_see_the_inherited_mode(repo):
    from coordinator_core.ops.dispatch_emit.ask_compose import _xl_exit_open

    _write(repo, ME, _doc(route="pm-decision", estimate={"tshirt": "XL"}))
    _write(repo, SIB, _doc(interaction_mode="ceo"))
    _parent(repo)
    assert _xl_exit_open(str(repo), ME) is True
