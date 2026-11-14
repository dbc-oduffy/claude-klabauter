"""dispatch.ask_fanout: partition, parent record, slice sizings, after edges, budget queue."""

from __future__ import annotations

import json
import os

import pytest

pytestmark = pytest.mark.complete
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
    return tmp_path


def _go(repo, params):
    return mod.fanout({**params, "session_id": SID}, repo)


def _sizing(repo, rel):
    return yaml.safe_load((repo / rel).read_text(encoding="utf-8"))


def _snapshot(repo):
    return sorted(p.relative_to(repo).as_posix() for p in repo.rglob("*") if p.is_file() and "hub" not in p.parts)


@pytest.mark.parametrize("trip_after", list(range(9)))
def test_abort_mid_write_removes_every_file_the_call_created(repo, trip_after):
    before = _snapshot(repo)
    polls = iter(range(100))
    out = mod.fanout(
        {"session_id": SID, "asks": [{"ask": f"fix {n}", "label": n, "writes": [f"pkg/{n}.py"]} for n in "abc"]},
        repo,
        lambda: next(polls) >= trip_after,
    )
    assert out["exit_code"] == 1 and "not taken" in out["error"], out
    assert _snapshot(repo) == before
    assert not list(repo.glob("scratch/warp/ask-parent-*"))


def test_rollback_while_a_stage_write_is_blocked_leaves_nothing_in_the_record_homes(repo, monkeypatch):
    import threading

    ledger = mod.FanoutLedger()
    entered, release = threading.Event(), threading.Event()
    real_write = os.write

    def blocked(fd, data):
        entered.set()
        release.wait(10)
        return real_write(fd, data)

    monkeypatch.setattr(mod.os, "write", blocked)
    box = []
    worker = threading.Thread(
        target=lambda: box.append(mod.fanout({"session_id": SID, "asks": [{"ask": "a"}, {"ask": "b"}]}, repo, None, ledger))
    )
    worker.start()
    assert entered.wait(5)
    assert ledger.rollback() is True
    assert not list((repo / "state").rglob("*.yaml")) and not list(repo.glob("scratch/warp/ask-parent-*"))
    release.set()
    worker.join(10)
    assert "not taken" in box[0]["error"]
    assert not list((repo / "state").rglob("*.yaml")) and not list(repo.rglob(".coordinator-local/**/*.yaml"))


def test_ledger_rollback_removes_claimed_files_and_a_late_write(tmp_path):
    ledger = mod.FanoutLedger()
    early, late = tmp_path / "a.yaml", tmp_path / "b.yaml"
    early.write_text("x")
    ledger.claim(early)
    ledger.claim(late)  # claimed, write still in flight when the rollback lands
    assert ledger.rollback() is True
    assert not early.exists()
    late.write_text("y")  # the in-flight write lands after the close
    with pytest.raises(mod._Refused):
        ledger.verify(late)
    assert not late.exists()
    with pytest.raises(mod._Refused):
        ledger.claim(tmp_path / "c.yaml")


def test_committed_ledger_rolls_back_nothing(tmp_path):
    ledger = mod.FanoutLedger()
    kept = tmp_path / "a.yaml"
    kept.write_text("x")
    ledger.claim(kept)
    ledger.commit()
    assert ledger.rollback() is False
    assert kept.exists()


def test_abort_never_firing_writes_as_before(repo):
    out = mod.fanout(
        {"session_id": SID, "asks": [{"ask": "fix a", "label": "a", "writes": ["pkg/a.py"]}]}, repo, lambda: False
    )
    assert out["exit_code"] == 0 and len(out["ready"]) == 1


@pytest.mark.parametrize("trip_after", list(range(10)))
def test_aborted_fanout_leaves_no_digest_entry_and_no_digest_dir(repo, trip_after):
    polls = iter(range(100))
    out = mod.fanout(
        {"session_id": SID, "asks": [{"ask": f"fix {n}", "label": n, "writes": [f"pkg/{n}.py"]} for n in "abc"]},
        repo,
        lambda: next(polls) >= trip_after,
    )
    assert out["exit_code"] == 1, out
    assert not (repo / "scratch" / "warp" / "ask-digest").exists()


def test_fanout_indexes_the_parent_by_digest_and_a_second_identical_submission_replaces_the_entry(repo):
    params = {"asks": [{"ask": "fix a", "writes": ["pkg/a.py"]}, {"ask": "fix b", "writes": ["pkg/b.py"]}]}
    first, second = _go(repo, params), _go(repo, params)
    assert first["exit_code"] == 0 and second["exit_code"] == 0, second
    entries = list((repo / "scratch" / "warp" / "ask-digest").glob("*.json"))
    assert len(entries) == 1
    assert json.loads(entries[0].read_text(encoding="utf-8"))["parent_id"] == second["parent_id"]


def test_sizings_arm_and_refused_submissions_write_no_digest_entry(repo):
    out = _go(repo, {"asks": [{"ask": "x", "after": ["nobody"]}]})
    assert out["exit_code"] == 1
    assert not (repo / "scratch" / "warp" / "ask-digest").exists()


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
    out = _go(repo, {"asks": [{"ask": "fix a", "source": "pm", "writes": ["a"]}, {"ask": "fix b", "source": "pm", "writes": ["b"]}]})
    for run, ready in zip(out["runs"], out["ready"]):
        doc = _sizing(repo, ready["sizing"])
        assert doc["intent"].startswith("fix ") and doc["intent_source"] == "em-elaborated"
        assert [v["text"] for v in doc["pm_verbatims"]] == [VERBATIM]
        assert c.parse_ask_parent(doc["em_analysis"]["ask_parent"]) == (out["parent_id"], run["run_id"])
        assert "parent_id" not in doc


def test_context_path_under_the_repo_root_is_written_repo_relative(repo):
    absolute = (repo / "docs" / "a.md").as_posix()
    out = _go(repo, {"asks": [{"ask": "fix a", "writes": ["a.py"], "context": [absolute]}]})
    assert out["exit_code"] == 0, out
    paths = _sizing(repo, out["ready"][0]["sizing"])["em_analysis"]["em_context_paths"]
    assert paths == "docs/a.md"


def test_mixed_sources_stamp_only_the_relayed_slice(repo):
    out = _go(repo, {"asks": [
        {"ask": "fix a", "source": "relayed", "writes": ["a"]},
        {"ask": "fix b", "source": "pm", "writes": ["b"]},
        {"ask": VERBATIM, "writes": ["c"]},
    ]})
    assert out["exit_code"] == 0, out
    from datetime import date
    paths = sorted((repo / "state" / "sizings").glob("*/*.yaml"))
    assert not list((repo / "state" / "sizings").glob("*.yaml"))
    assert {p.parent.name for p in paths} == {date.today().strftime("%Y-%m")}
    docs = [yaml.safe_load(p.read_text(encoding="utf-8")) for p in paths]
    assert len(docs) == 3
    assert docs[0]["em_analysis"][c.SLICE_SOURCE_KEY] == "relayed"
    assert c.slice_ask_source(docs[0]) == "relayed"
    assert c.SLICE_SOURCE_KEY not in docs[1]["em_analysis"]
    assert c.slice_ask_source(docs[2]) == "relayed" and "pm_verbatims" not in docs[2]


def test_overlapping_declared_slices_are_sequenced_and_undeclared_runs_parallel(repo):
    out = _go(repo, {"asks": [
        {"ask": "one", "writes": ["pkg"]},
        {"ask": "two", "writes": ["pkg/x.py"]},
        {"ask": "three"},
    ]})
    first, second, third = out["runs"]
    assert "after" not in first
    assert second["after"] == [first["run_id"]]
    assert "after" not in third
    assert [r["run_id"] for r in out["ready"]] == [first["run_id"], third["run_id"]]


def test_declared_after_holds_the_run_on_the_named_sibling_by_label_and_index(repo):
    out = _go(repo, {"asks": [
        {"ask": "one", "label": "first"},
        {"ask": "two", "after": ["first"]},
        {"ask": "three", "after": [0, 1]},
        {"ask": "four"},
    ]})
    assert out["exit_code"] == 0, out
    r1, r2, r3, r4 = out["runs"]
    assert "after" not in r1 and "after" not in r4
    assert r2["after"] == [r1["run_id"]]
    assert r3["after"] == [r1["run_id"], r2["run_id"]]
    assert [r["run_id"] for r in out["ready"]] == [r1["run_id"], r4["run_id"]]


@pytest.mark.parametrize("after", [["nope"], ["slice-2"], [1], [0]])
def test_after_naming_no_earlier_sibling_is_refused_by_key_and_writes_nothing(repo, after):
    before = _snapshot(repo)
    out = _go(repo, {"asks": [{"ask": "one", "after": after}, {"ask": "two"}]})
    assert out["exit_code"] == 1 and "asks[0].after" in out["error"]
    assert _snapshot(repo) == before


def test_three_undeclared_asks_are_sibling_runs_with_no_after_edges(repo, monkeypatch):
    out = _go(repo, {"asks": [{"ask": f"job {i}"} for i in range(3)]})
    assert out["exit_code"] == 0, out
    assert len(out["runs"]) == 3
    assert all("after" not in r for r in out["runs"])
    assert len(out["ready"]) == 3


def test_many_disjoint_asks_are_all_ready_in_submission_order(repo):
    n = (os.cpu_count() or 1) * 2 + 1
    out = _go(repo, {"asks": [{"ask": f"job {i}", "writes": [f"d{i}"]} for i in range(n)]})
    assert out["exit_code"] == 0
    parent = json.loads((repo / c.parent_record_rel(out["parent_id"])).read_text(encoding="utf-8"))
    assert [r["status"] for r in parent["runs"]] == ["ready"] * n
    assert [r["run_id"] for r in out["ready"]] == [r["run_id"] for r in parent["runs"]]
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
        {"ask": "fix b", "source": "pm", "writes": ["b"]},
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


def test_route_preference_stamps_only_the_carrying_slice(repo):
    out = _go(repo, {"asks": [
        {"ask": "fix a", "route_preference": " roadmap ", "em_rationale": "spans two workstreams", "writes": ["a"]},
        {"ask": "fix b", "source": "relayed", "route_preference": "goal", "writes": ["b"]},
        {"ask": "fix c", "writes": ["c"]},
    ]})
    assert out["exit_code"] == 0, out
    docs = [yaml.safe_load(p.read_text(encoding="utf-8")) for p in sorted((repo / "state" / "sizings").glob("*/*.yaml"))]
    assert len(docs) == 3
    assert docs[0]["em_analysis"]["route_preference"] == "roadmap"
    assert docs[0]["em_analysis"]["em_rationale"] == "spans two workstreams"
    assert docs[1]["em_analysis"]["route_preference"] == "goal"
    assert "em_rationale" not in docs[1]["em_analysis"]
    assert "route_preference" not in docs[2]["em_analysis"] and "em_rationale" not in docs[2]["em_analysis"]
    assert all(not mod.validate_frontmatter(d, mod._SIZING_SCHEMA_PATH) for d in docs)


@pytest.mark.parametrize("item,named", [
    ({"ask": "y", "route_preference": "shape"}, "asks[1].route_preference"),
    ({"ask": "y", "em_rationale": "x" * 2001}, "asks[1].em_rationale"),
    ({"ask": "y", "em_rationale": " "}, "asks[1].em_rationale"),
])
def test_invalid_route_preference_refuses_before_any_write(repo, item, named):
    before = _snapshot(repo)
    out = _go(repo, {"asks": [{"ask": "x", "route_preference": "plan"}, item]})
    assert out["exit_code"] == 1 and out["applied"] is False
    assert named in out["error"]
    assert _snapshot(repo) == before


def test_sized_through_turn_moves_once_after_the_last_slice(repo):
    out = _go(repo, {"asks": [
        {"ask": "x", "source": "pm", "writes": ["a"]}, {"ask": "y", "source": "pm", "writes": ["b"]},
    ]})
    assert out["exit_code"] == 0
    assert store.read_baton(SID, str(repo)).get("sized_through_turn") == 0


def test_all_relayed_submission_does_not_move_the_sized_through_mark(repo):
    out = _go(repo, {"asks": [{"ask": "x", "writes": ["a"]}, {"ask": "y", "source": "relayed", "writes": ["b"]}]})
    assert out["exit_code"] == 0
    assert store.read_baton(SID, str(repo)).get("sized_through_turn") != 0


def test_unmarked_item_matching_a_recorded_turn_is_relayed_without_pm_verbatims(repo):
    out = _go(repo, {"asks": [{"ask": VERBATIM.upper(), "writes": ["a"]}]})
    doc = _sizing(repo, out["ready"][0]["sizing"])
    assert "pm_verbatims" not in doc
    assert c.slice_ask_source(doc) == "relayed"


def test_unmarked_and_relayed_items_never_read_the_turn_log(repo, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("turn log read")

    monkeypatch.setattr(PT, "recent_window", boom)
    monkeypatch.setattr(PT, "_load", boom)
    out = _go(repo, {"asks": [{"ask": VERBATIM, "writes": ["a"]}, {"ask": "y", "source": "relayed", "writes": ["b"]}]})
    assert out["exit_code"] == 0, out


def test_pm_item_with_no_recorded_turn_refuses_before_any_write(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "hub" / SID).mkdir(parents=True)
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: tmp_path / "hub" / s)
    before = _snapshot(tmp_path)
    out = mod.fanout({"asks": [{"ask": "x", "writes": ["a"]}, {"ask": "y", "source": "pm", "writes": ["b"]}], "session_id": SID}, tmp_path)
    assert out["exit_code"] == 1 and out["applied"] is False
    assert "asks[1]" in out["error"] and "relayed" in out["error"]
    assert _snapshot(tmp_path) == before


def test_unmarked_item_matching_no_turn_is_relayed_without_pm_verbatims(repo):
    out = _go(repo, {"asks": [{"ask": "fix a", "writes": ["a"]}, {"ask": f"EM says: {VERBATIM}", "writes": ["b"]}]})
    assert out["exit_code"] == 0, out
    for ready in out["ready"]:
        doc = _sizing(repo, ready["sizing"])
        assert "pm_verbatims" not in doc
        assert c.slice_ask_source(doc) == "relayed"


def _planned_sizing(repo, name, plan_rel, writes):
    from coordinator_core.ops.dispatch_emit.tests.test_ask_fanout_sizings_arm import _sizing as planned

    path = repo / plan_rel
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"    - {w}\n" for w in writes)
    path.write_text(
        "---\nrun_id: fixture\n---\n\n## Tasks\n\n```yaml plan-tasks\n"
        f"- id: A1\n  title: T\n  disposition: open\n  writes:\n{body}```\n",
        encoding="utf-8",
        newline="\n",
    )
    return planned(repo, name, plan_rel)


def test_sizings_arm_undeclared_items_with_shared_plan_writes_are_sequenced(repo):
    a = _planned_sizing(repo, "a", "docs/plans/2026-10-16-a.md", ["pkg/x.py"])
    b = _planned_sizing(repo, "b", "docs/plans/2026-10-16-b.md", ["pkg/x.py"])
    out = _go(repo, {"sizings": [{"sizing": a, "label": "a"}, {"sizing": b, "label": "b"}]})
    first, second = out["runs"]
    assert "after" not in first
    assert second["after"] == [first["run_id"]]


def test_sizings_arm_undeclared_items_with_disjoint_plan_writes_stay_parallel(repo):
    a = _planned_sizing(repo, "a", "docs/plans/2026-10-16-a.md", ["pkg/x.py"])
    b = _planned_sizing(repo, "b", "docs/plans/2026-10-16-b.md", ["pkg/y.py"])
    out = _go(repo, {"sizings": [{"sizing": a, "label": "a"}, {"sizing": b, "label": "b"}]})
    assert all("after" not in r for r in out["runs"])


def test_sizings_arm_declared_writes_win_over_inference(repo):
    a = _planned_sizing(repo, "a", "docs/plans/2026-10-16-a.md", ["pkg/x.py"])
    b = _planned_sizing(repo, "b", "docs/plans/2026-10-16-b.md", ["pkg/x.py"])
    out = _go(repo, {"sizings": [
        {"sizing": a, "label": "a", "writes": ["one/a.py"]},
        {"sizing": b, "label": "b", "writes": ["two/b.py"]},
    ]})
    assert all("after" not in r for r in out["runs"])


def test_sizings_arm_planless_sizing_infers_nothing_and_overlaps_nothing(repo):
    from coordinator_core.ops.dispatch_emit.tests.test_ask_fanout_sizings_arm import _sizing as planned

    a = _planned_sizing(repo, "a", "docs/plans/2026-10-16-a.md", ["pkg/x.py"])
    assert mod._writes_or_inferred(repo, planned(repo, "p", plan=None), None) is None
    assert mod._writes_or_inferred(repo, "state/sizings/missing.yaml", None) is None
    assert mod._writes_or_inferred(repo, a, None) == ("pkg/x.py",)
