"""dispatch.ask_fanout fan_out arm: sized component slices and the engine-appended seam-pass slice."""

from __future__ import annotations

import subprocess
import time

import pytest
import yaml

from coordinator_core.frontmatter.schema_validate import validate_frontmatter
from coordinator_core.ops import baton_pm_turns as PT
from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import ask_fanout as mod
from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c
from coordinator_core.ops.sizing_intake_contract import INTAKE_MARK_KEY, needs_size_stage
from coordinator_core.session_baton import store
from coordinator_core.sizing_assemble import _BASE_ROUTE_BY_TSHIRT

SID = "sid-fan-out"
PARENT_CRITERION = "every component lands and the seams hold"
DELIVERABLE = "dlv-fan-out-fixture"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "hub" / SID).mkdir(parents=True)
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: tmp_path / "hub" / s)
    assert PT.append_turn("fan this out", session_id=SID)["ok"]
    return tmp_path


def _parent(repo, rows):
    rel = "state/sizings/2026-10-23-parent.yaml"
    (repo / "state" / "sizings").mkdir(parents=True, exist_ok=True)
    doc = {
        "schema": "sizing-object", "name": "parent", "intent": "bundle", "intent_source": "pm-verbatim",
        "estimate": {"tshirt": "XL", "provisional": True}, "route": "pm-decision", "detents": [], "fork": None,
        "xl_exit": "fan_out", "status": "routed", "interaction_mode": "pm",
        "deliverable_id": DELIVERABLE,
        "exit_criterion": {"statement": PARENT_CRITERION, "accepted": None},
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 0, "text": "fan this out"}],
        rr.REGISTER_KEY: {rr.ROWS_KEY: [
            {rr.ROW_ID: r, rr.ROW_TEXT: f"text of {r}", rr.ROW_ANCHOR: "pm_verbatims", rr.ROW_SURFACE: "internal",
             rr.ROW_STATUS: "open", rr.ROW_CLAIMED_BY: []} for r in rows
        ]},
    }
    (repo / rel).write_text(yaml.safe_dump(doc), encoding="utf-8")
    return rel


def _comp(label, rows, estimate="M", **kw):
    return {"label": label, "ask": f"do {label}", "rows": rows, "estimate": estimate,
            "exit_criterion": f"{label} is done", "writes": [f"pkg/{label}"], **kw}


def _go(repo, params):
    return mod.fanout({"fan_out": True, "session_id": SID, **params}, repo)


def _doc(repo, rel):
    return yaml.safe_load((repo / rel).read_text(encoding="utf-8"))


def _files(repo):
    return sorted(p.relative_to(repo).as_posix() for p in repo.rglob("*") if p.is_file() and "hub" not in p.parts)


def _run(repo):
    parent = _parent(repo, ["R1", "R2"])
    out = _go(repo, {"parent_sizing": parent, "slices": [_comp("a", ["R1"], "M"), _comp("b", ["R2"], "L")]})
    assert out["exit_code"] == 0, out
    return parent, out


def test_component_slices_are_routed_accepted_and_skip_the_size_stage(repo):
    _parent_rel, out = _run(repo)
    sizings = [_doc(repo, r["sizing"]) for r in out["ready"]]
    assert len(sizings) == 2
    for doc, tshirt in zip(sizings, ("M", "L")):
        assert doc["estimate"]["tshirt"] == tshirt
        assert doc["route"] == _BASE_ROUTE_BY_TSHIRT[tshirt]
        assert doc["status"] == "routed"
        assert doc["exit_criterion"]["statement"].endswith("is done")
        accepted = doc["exit_criterion"]["accepted"]
        assert accepted["source"] == "apm" and accepted["ruling_ref"] == out["parent_id"]
        assert INTAKE_MARK_KEY not in doc["em_analysis"]
        assert not needs_size_stage(doc)
        assert "deliverable_id" not in doc


def test_written_sized_records_validate_against_the_schema(repo):
    _run(repo)
    for rel in _all_slice_paths(repo):
        assert validate_frontmatter(_doc(repo, rel), mod._SIZING_SCHEMA_PATH) == []


def _all_slice_paths(repo):
    return [p for p in _files(repo) if p.startswith("state/sizings/") and not p.endswith("-parent.yaml")]


def test_seam_waits_on_every_component_and_carries_the_parent_deliverable(repo):
    _parent_rel, out = _run(repo)
    runs = out["runs"]
    assert [r["label"] for r in runs] == ["a", "b", c.SEAM_PASS_LABEL]
    assert runs[2]["after"] == [runs[0]["run_id"], runs[1]["run_id"]]
    assert "after" not in runs[0] and "after" not in runs[1]
    assert [r["label"] for r in out["runs"] if "after" not in r] == ["a", "b"]
    seam_rel = next(p for p in _files(repo) if p.endswith("-seam-pass.yaml"))
    seam = _doc(repo, seam_rel)
    assert seam["deliverable_id"] == DELIVERABLE
    assert seam["estimate"]["tshirt"] == c.SEAM_PASS_TSHIRT
    assert seam["status"] == "routed" and not needs_size_stage(seam)
    assert seam["exit_criterion"]["statement"] == PARENT_CRITERION
    assert seam["exit_criterion"]["accepted"]["source"] == "apm"
    for label, run in (("a", runs[0]), ("b", runs[1])):
        assert label in seam["intent"] and run["run_id"] in seam["intent"]
    assert "plan.seam_check" in seam["intent"] and _parent_rel in seam["intent"]
    assert rr.REGISTER_KEY not in seam
    assert len(out["ready"]) == 2


def test_seam_carrier_slice_assigns_the_seam_its_rows(repo):
    parent = _parent(repo, ["R1", "R2"])
    out = _go(repo, {"parent_sizing": parent, "slices": [
        _comp("a", ["R1"]), {"label": c.SEAM_PASS_LABEL, "ask": "ignored", "rows": ["R2"]},
    ]})
    assert out["exit_code"] == 0, out
    seam = _doc(repo, next(p for p in _files(repo) if p.endswith("-seam-pass.yaml")))
    assert [r["id"] for r in seam[rr.REGISTER_KEY][rr.ROWS_KEY]] == ["R2"]


@pytest.mark.parametrize("bad", ["XS", "S", "XL", "XXL", None])
def test_component_outside_the_component_sizes_is_refused_before_any_write(repo, bad):
    parent = _parent(repo, ["R1", "R2"])
    before = _files(repo)
    out = _go(repo, {"parent_sizing": parent, "slices": [_comp("a", ["R1"]), _comp("b", ["R2"], bad)]})
    assert out["exit_code"] == 1 and "slices[1]" in out["error"] and "estimate" in out["error"]
    assert _files(repo) == before


@pytest.mark.parametrize("bad", [None, "", "   "])
def test_missing_exit_criterion_is_refused_before_any_write(repo, bad):
    parent = _parent(repo, ["R1", "R2"])
    before = _files(repo)
    second = _comp("b", ["R2"])
    second["exit_criterion"] = bad
    out = _go(repo, {"parent_sizing": parent, "slices": [_comp("a", ["R1"]), second]})
    assert out["exit_code"] == 1 and "slices[1]" in out["error"] and "exit_criterion" in out["error"]
    assert _files(repo) == before


def test_without_fan_out_a_slice_stays_a_plain_draft_with_no_seam(repo):
    parent = _parent(repo, ["R1", "R2"])
    out = mod.fanout({"session_id": SID, "parent_sizing": parent, "slices": [
        {"label": "a", "ask": "do a", "rows": ["R1"], "estimate": "XS", "exit_criterion": "ignored"},
        {"label": "b", "ask": "do b", "rows": ["R2"]},
    ]}, repo)
    assert out["exit_code"] == 0, out
    assert [r["label"] for r in out["runs"]] == ["a", "b"]
    for entry in out["ready"]:
        doc = _doc(repo, entry["sizing"])
        assert doc["status"] == "draft" and doc["estimate"] == {"tshirt": "M", "provisional": True}
        assert "exit_criterion" not in doc and needs_size_stage(doc)


def test_fan_out_is_refused_on_the_asks_arm(repo):
    out = _go(repo, {"asks": [{"ask": "x"}]})
    assert out["exit_code"] == 1 and "slices arm" in out["error"]


def test_parent_without_an_exit_criterion_is_refused(repo):
    parent = _parent(repo, ["R1"])
    path = repo / parent
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["exit_criterion"] = None
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    before = _files(repo)
    out = _go(repo, {"parent_sizing": parent, "slices": [_comp("a", ["R1"])]})
    assert out["exit_code"] == 1 and "exit_criterion" in out["error"]
    assert _files(repo) == before


def test_four_components_spawn_nothing_and_stay_under_the_bar(repo, monkeypatch):
    spawned = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append(a) or (_ for _ in ()).throw(AssertionError("spawn")))
    parent = _parent(repo, ["R1", "R2", "R3", "R4"])
    slices = [_comp(f"c{i}", [f"R{i}"], "M" if i % 2 else "L") for i in range(1, 5)]
    start = time.process_time()
    out = _go(repo, {"parent_sizing": parent, "slices": slices})
    elapsed = time.process_time() - start
    assert out["exit_code"] == 0, out
    assert len(out["runs"]) == 5 and spawned == []
    assert len(out["runs"][4]["after"]) == 4
    assert elapsed < 0.5
