"""dispatch.ask_fanout sizings arm: sibling runs over existing planned sizings."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.frontmatter.schema_validate import validate_frontmatter
from coordinator_core.ops.dispatch_emit import ask_fanout as mod
from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c
from coordinator_core.ops.dispatch_emit.sizing_fire import resumable_plan
from coordinator_core.ops.sizing_intake_contract import intake_record

PLAN_A = "docs/plans/2026-10-16-a.md"
PLAN_B = "docs/plans/2026-10-16-b.md"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(c, "concurrent_run_budget", lambda physical_cores=None: 4)
    return tmp_path


def _plan(repo, rel):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\nrun_id: fixture\n---\n\n## Tasks\n\n```yaml plan-tasks\n"
        "- id: A1\n  title: T\n  disposition: open\n  writes:\n    - some/a.py\n```\n",
        encoding="utf-8",
        newline="\n",
    )


def _sizing(repo, name, plan=PLAN_A, marker=None, mode="pm"):
    rec = intake_record(name=name, turn={"text": "do the thing", "turn": 1}, today="2026-10-16")
    if mode:
        rec["interaction_mode"] = mode
    if plan:
        rec["plan"] = plan
    if marker:
        rec["em_analysis"]["ask_parent"] = marker
    rel = f"state/sizings/2026-10-16-{name}.yaml"
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(rec, sort_keys=False), encoding="utf-8", newline="\n")
    return rel


def _snapshot(repo):
    return {p.relative_to(repo).as_posix(): p.read_bytes() for p in repo.rglob("*") if p.is_file()}


def test_disjoint_planned_sizings_go_ready_together(repo):
    a, b = _sizing(repo, "a", PLAN_A), _sizing(repo, "b", PLAN_B)
    out = mod.fanout({"sizings": [
        {"sizing": a, "label": "a", "writes": ["pkg/a.py"]},
        {"sizing": b, "label": "b", "writes": ["pkg/b.py"]},
    ]}, repo)
    assert out["exit_code"] == 0, out
    assert [r["label"] for r in out["runs"]] == ["a", "b"]
    assert all("after" not in r for r in out["runs"])
    assert [r["sizing"] for r in out["ready"]] == [a, b]
    assert len(out["fire"]) == 2 and all(rel in call for call, rel in zip(out["fire"], (a, b)))


def test_overlapping_writes_chain_with_after(repo):
    a, b = _sizing(repo, "a", PLAN_A), _sizing(repo, "b", PLAN_B)
    out = mod.fanout({"sizings": [
        {"sizing": a, "writes": ["pkg"]},
        {"sizing": b, "writes": ["pkg/b.py"]},
    ]}, repo)
    assert out["exit_code"] == 0, out
    assert out["runs"][1]["after"] == [out["runs"][0]["run_id"]]
    assert [r["sizing"] for r in out["ready"]] == [a]


def test_two_wave_fanout_orders_wave_one_after_wave_zero(repo):
    a, b = _sizing(repo, "a", PLAN_A), _sizing(repo, "b", PLAN_B)
    out = mod.fanout({"sizings": [
        {"sizing": a, "writes": ["pkg/a.py"], "wave": 0},
        {"sizing": b, "writes": ["pkg/b.py"], "wave": 1},
    ]}, repo)
    assert out["exit_code"] == 0, out
    assert out["runs"][1]["after"] == [out["runs"][0]["run_id"]]
    assert [r["sizing"] for r in out["ready"]] == [a]


@pytest.mark.parametrize("bad", [True, "1", 1.5, -1])
def test_bad_wave_refuses_by_name_and_writes_nothing(repo, bad):
    a, b = _sizing(repo, "a", PLAN_A), _sizing(repo, "b", PLAN_B)
    before = _snapshot(repo)
    out = mod.fanout({"sizings": [{"sizing": a, "wave": 0}, {"sizing": b, "wave": bad}]}, repo)
    assert out["exit_code"] == 1 and "sizings[1]: wave must be" in out["error"]
    assert _snapshot(repo) == before


def test_decreasing_waves_refuse_and_write_nothing(repo):
    a, b = _sizing(repo, "a", PLAN_A), _sizing(repo, "b", PLAN_B)
    before = _snapshot(repo)
    out = mod.fanout({"sizings": [{"sizing": a, "wave": 1}, {"sizing": b, "wave": 0}]}, repo)
    assert out["exit_code"] == 1 and "sizings[1]: wave 0 is lower" in out["error"]
    assert _snapshot(repo) == before


def test_sizing_without_plan_edge_refuses_and_writes_nothing(repo):
    a, b = _sizing(repo, "a", PLAN_A), _sizing(repo, "b", plan=None)
    before = _snapshot(repo)
    out = mod.fanout({"sizings": [{"sizing": a, "writes": ["x"]}, {"sizing": b, "writes": ["y"]}]}, repo)
    assert out["exit_code"] == 1 and out["applied"] is False and "plan edge" in out["error"]
    assert _snapshot(repo) == before


def test_conflicting_marker_refuses_and_writes_nothing(repo):
    a = _sizing(repo, "a", PLAN_A)
    b = _sizing(repo, "b", PLAN_B, marker=c.ask_parent_marker("ask-parent-other", "ask-other"))
    before = _snapshot(repo)
    out = mod.fanout({"sizings": [{"sizing": a, "writes": ["x"]}, {"sizing": b, "writes": ["y"]}]}, repo)
    assert out["exit_code"] == 1 and "ask_parent" in out["error"]
    assert _snapshot(repo) == before


def test_arms_are_exclusive(repo):
    a = _sizing(repo, "a")
    out = mod.fanout({"sizings": [{"sizing": a}], "asks": [{"ask": "x"}]}, repo)
    assert out["exit_code"] == 1 and "exactly one" in out["error"]


def test_stamped_sizing_validates_and_resumes_its_plan(repo):
    _plan(repo, PLAN_A)
    a = _sizing(repo, "a", PLAN_A)
    out = mod.fanout({"sizings": [{"sizing": a, "writes": ["x"]}]}, repo)
    assert out["exit_code"] == 0, out
    doc = yaml.safe_load((repo / a).read_text(encoding="utf-8"))
    run = out["runs"][0]
    assert doc["em_analysis"]["ask_parent"] == c.ask_parent_marker(out["parent_id"], run["run_id"])
    assert validate_frontmatter(doc, mod._SIZING_SCHEMA_PATH) == []
    plan = resumable_plan(doc, a, repo)
    assert plan.plan_rel == PLAN_A and plan.incomplete == ("A1",)
    assert (repo / c.parent_record_rel(out["parent_id"])).is_file()


def _doc(repo, rel):
    return yaml.safe_load((repo / rel).read_text(encoding="utf-8"))


def test_interaction_mode_absent_is_stamped_from_the_inherited_param(repo):
    a = _sizing(repo, "a", PLAN_A, mode=None)
    out = mod.fanout({"sizings": [{"sizing": a}], c.INTERACTION_MODE_PARAM: "ceo"}, repo)
    assert out["exit_code"] == 0, out
    doc = _doc(repo, a)
    assert doc["interaction_mode"] == "ceo"
    assert validate_frontmatter(doc, mod._SIZING_SCHEMA_PATH) == []


def test_interaction_mode_recorded_is_kept_never_overwritten(repo):
    a = _sizing(repo, "a", PLAN_A, mode="hands-on")
    out = mod.fanout({"sizings": [{"sizing": a}], c.INTERACTION_MODE_PARAM: "ceo"}, repo)
    assert out["exit_code"] == 0, out
    assert _doc(repo, a)["interaction_mode"] == "hands-on"


def test_interaction_mode_neither_recorded_nor_inherited_refuses_and_writes_nothing(repo):
    a = _sizing(repo, "a", PLAN_A, mode=None)
    before = _snapshot(repo)
    out = mod.fanout({"sizings": [{"sizing": a}]}, repo)
    assert out["exit_code"] == 1 and out["applied"] is False
    assert "records no interaction_mode" in out["error"] and a in out["error"]
    assert _snapshot(repo) == before


@pytest.mark.parametrize("bad", ["bogus", "", 3])
def test_interaction_mode_bad_value_refuses_and_writes_nothing(repo, bad):
    a = _sizing(repo, "a", PLAN_A, mode=None)
    before = _snapshot(repo)
    out = mod.fanout({"sizings": [{"sizing": a}], c.INTERACTION_MODE_PARAM: bad}, repo)
    assert out["exit_code"] == 1 and out["applied"] is False
    assert _snapshot(repo) == before
