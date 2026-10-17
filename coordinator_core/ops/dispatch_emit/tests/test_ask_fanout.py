"""dispatch.ask_fanout: partition, parent record, slice sizings, after edges, budget queue."""

from __future__ import annotations

import json

import pytest
import yaml

from coordinator_core.ops import baton_pm_turns as PT
from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import ask_fanout as mod
from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c
from coordinator_core.session_baton import store

SID = "sid-fanout"
VERBATIM = "split these two jobs please, they are unrelated"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "hub" / SID).mkdir(parents=True)
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: tmp_path / "hub" / s)
    assert PT.append_turn(VERBATIM, session_id=SID)["ok"]
    monkeypatch.setattr(c, "concurrent_run_budget", lambda physical_cores=None: 2)
    return tmp_path


def _go(repo, params):
    return mod.fanout({**params, "session_id": SID}, repo)


def _sizing(repo, rel):
    return yaml.safe_load((repo / rel).read_text(encoding="utf-8"))


def _snapshot(repo):
    return sorted(p.relative_to(repo).as_posix() for p in repo.rglob("*") if p.is_file() and "hub" not in p.parts)


def test_disjoint_pair_is_two_ready_runs_with_the_frozen_reply(repo):
    out = _go(repo, {"asks": [
        {"ask": "fix a", "label": "a", "writes": ["pkg/a.py"]},
        {"ask": "fix b", "label": "b", "writes": ["pkg/b.py"]},
    ]})
    assert out["exit_code"] == 0, out
    assert set(out) >= {"parent_id", "runs", "ready", "fire"}
    assert out["parent_id"].startswith("ask-parent-")
    assert [r["label"] for r in out["runs"]] == ["a", "b"]
    assert all("after" not in r for r in out["runs"])
    assert len(out["ready"]) == 2 and len(out["fire"]) == 2
    assert all(rel in call for call, rel in zip(out["fire"], (r["sizing"] for r in out["ready"])))


def test_slice_sizing_carries_verbatim_intent_and_parent_marker(repo):
    out = _go(repo, {"asks": [{"ask": "fix a", "writes": ["a"]}, {"ask": "fix b", "writes": ["b"]}]})
    for run, ready in zip(out["runs"], out["ready"]):
        doc = _sizing(repo, ready["sizing"])
        assert doc["intent"].startswith("fix ") and doc["intent_source"] == "em-elaborated"
        assert [v["text"] for v in doc["pm_verbatims"]] == [VERBATIM]
        assert c.parse_ask_parent(doc["em_analysis"]["ask_parent"]) == (out["parent_id"], run["run_id"])
        assert "parent_id" not in doc


def test_mixed_sources_stamp_only_the_relayed_slice(repo):
    out = _go(repo, {"asks": [
        {"ask": "fix a", "source": "relayed", "writes": ["a"]},
        {"ask": "fix b", "source": "pm", "writes": ["b"]},
        {"ask": "fix c", "writes": ["c"]},
    ]})
    assert out["exit_code"] == 0, out
    paths = sorted((repo / "state" / "sizings").glob("*.yaml"))
    docs = [yaml.safe_load(p.read_text(encoding="utf-8")) for p in paths]
    assert len(docs) == 3
    assert docs[0]["em_analysis"][c.SLICE_SOURCE_KEY] == "relayed"
    assert c.slice_ask_source(docs[0]) == "relayed"
    assert all(c.SLICE_SOURCE_KEY not in d["em_analysis"] for d in docs[1:])


def test_overlapping_and_no_hint_slices_are_sequenced(repo):
    out = _go(repo, {"asks": [
        {"ask": "one", "writes": ["pkg"]},
        {"ask": "two", "writes": ["pkg/x.py"]},
        {"ask": "three"},
    ]})
    first, second, third = out["runs"]
    assert "after" not in first
    assert second["after"] == [first["run_id"]]
    assert third["after"] == [first["run_id"], second["run_id"]]
    assert [r["run_id"] for r in out["ready"]] == [first["run_id"]]


def test_overflow_is_queued_in_submission_order_never_refused(repo):
    out = _go(repo, {"asks": [{"ask": f"job {i}", "writes": [f"d{i}"]} for i in range(4)]})
    assert out["exit_code"] == 0
    parent = json.loads((repo / c.parent_record_rel(out["parent_id"])).read_text(encoding="utf-8"))
    assert [r["status"] for r in parent["runs"]] == ["ready", "ready", "queued", "queued"]
    assert [r["run_id"] for r in out["ready"]] == [r["run_id"] for r in parent["runs"][:2]]
    assert c.ParentRecord.from_json(parent).parent_id == out["parent_id"]


def test_parent_record_is_write_once_and_runtime_only(repo):
    out = _go(repo, {"asks": [{"ask": "x", "writes": ["a"]}, {"ask": "y", "writes": ["b"]}]})
    rel = c.parent_record_rel(out["parent_id"])
    assert rel.startswith("scratch/") and (repo / rel).is_file()
    assert not (repo / "state" / "warp").exists()


def _parent_sizing(repo, rows):
    rel = "state/sizings/2026-10-15-parent.yaml"
    (repo / "state" / "sizings").mkdir(parents=True, exist_ok=True)
    doc = {
        "schema": "sizing-object", "name": "parent", "intent": "bundle", "intent_source": "pm-verbatim",
        "estimate": {"tshirt": "XL", "provisional": True}, "route": "plan", "detents": [], "fork": None,
        "xl_exit": "split", "status": "draft",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 0, "text": VERBATIM}],
        rr.REGISTER_KEY: {rr.ROWS_KEY: [
            {rr.ROW_ID: r, rr.ROW_TEXT: f"text of {r}", rr.ROW_ANCHOR: "pm_verbatims", rr.ROW_SURFACE: "internal",
             rr.ROW_STATUS: "open", rr.ROW_CLAIMED_BY: []} for r in rows
        ]},
    }
    (repo / rel).write_text(yaml.safe_dump(doc), encoding="utf-8")
    return rel


def test_slices_arm_copies_each_slices_rows_byte_for_byte(repo):
    parent = _parent_sizing(repo, ["R1", "R2", "R3"])
    out = _go(repo, {"parent_sizing": parent, "slices": [
        {"label": "a", "ask": "do a", "rows": ["R1", "R3"], "writes": ["a"]},
        {"label": "b", "ask": "do b", "rows": ["R2"], "writes": ["b"]},
    ]})
    assert out["exit_code"] == 0, out
    parent_rows = {r["id"]: r for r in _sizing(repo, parent)[rr.REGISTER_KEY]["rows"]}
    got = [[r["id"] for r in _sizing(repo, e["sizing"])[rr.REGISTER_KEY]["rows"]] for e in out["ready"]]
    assert got == [["R1", "R3"], ["R2"]]
    for e in out["ready"]:
        for row in _sizing(repo, e["sizing"])[rr.REGISTER_KEY]["rows"]:
            assert row == parent_rows[row["id"]]


def test_doubled_and_unclaimed_rows_refuse_before_any_write(repo):
    parent = _parent_sizing(repo, ["R1", "R2", "R3"])
    before = _snapshot(repo)
    out = _go(repo, {"parent_sizing": parent, "slices": [
        {"label": "a", "ask": "do a", "rows": ["R1", "R2"]},
        {"label": "b", "ask": "do b", "rows": ["R2"]},
    ]})
    assert out["exit_code"] == 1 and out["applied"] is False
    assert "R3" in out["error"] and "R2" in out["error"]
    assert _snapshot(repo) == before


@pytest.mark.parametrize("params", [
    {},
    {"asks": []},
    {"asks": [{"ask": ""}]},
    {"asks": [{"ask": "x", "writes": "a.py"}]},
    {"asks": [{"ask": "x"}, {"ask": "y", "source": "operator"}]},
    {"asks": [{"ask": "x"}], "slices": [{"ask": "y", "rows": []}], "parent_sizing": "p"},
    {"asks": [{"ask": "x"}], "confirm_split": "yes"},
    {"slices": [{"ask": "y", "rows": ["R1"]}]},
])
def test_malformed_submissions_refuse_without_writing(repo, params):
    before = _snapshot(repo)
    out = _go(repo, params)
    assert out["exit_code"] == 1 and out["applied"] is False
    assert _snapshot(repo) == before


def test_no_recorded_pm_turn_proceeds_stamped_relayed(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "hub" / SID).mkdir(parents=True)
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: tmp_path / "hub" / s)
    monkeypatch.setattr(c, "concurrent_run_budget", lambda physical_cores=None: 2)
    out = mod.fanout({"asks": [{"ask": "fix x", "sender": "peer-a", "writes": ["x"]}], "session_id": SID}, tmp_path)
    assert out["exit_code"] == 0, out
    doc = _sizing(tmp_path, out["ready"][0]["sizing"])
    assert "pm_verbatims" not in doc
    assert doc["intent_source"] == "em-elaborated"
    assert c.slice_ask_source(doc) == "relayed"
    assert doc["em_analysis"][mod.RELAYED_TEXT_KEY] == "fix x"
    assert doc["em_analysis"][mod.RELAYED_FROM_KEY] == "peer-a"


def test_relayed_source_with_a_pm_turn_carries_text_and_sender_not_the_pm_turn(repo):
    out = _go(repo, {"sender": "peer-b", "asks": [
        {"ask": "fix a", "source": "relayed", "relayed_text": "peer said: fix a", "writes": ["a"]},
        {"ask": "fix b", "writes": ["b"]},
    ]})
    assert out["exit_code"] == 0, out
    relayed, pm = (_sizing(repo, r["sizing"]) for r in out["ready"])
    assert "pm_verbatims" not in relayed
    assert relayed["em_analysis"][mod.RELAYED_TEXT_KEY] == "peer said: fix a"
    assert relayed["em_analysis"][mod.RELAYED_FROM_KEY] == "peer-b"
    assert [v["text"] for v in pm["pm_verbatims"]] == [VERBATIM]
    assert c.slice_ask_source(pm) is None


def test_blank_sender_refuses(repo):
    out = _go(repo, {"asks": [{"ask": "x", "sender": " "}]})
    assert out["exit_code"] == 1


def test_sized_through_turn_moves_once_after_the_last_slice(repo):
    out = _go(repo, {"asks": [{"ask": "x", "writes": ["a"]}, {"ask": "y", "writes": ["b"]}]})
    assert out["exit_code"] == 0
    assert store.read_baton(SID, str(repo)).get("sized_through_turn") == 0
