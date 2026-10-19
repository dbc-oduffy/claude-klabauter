"""dispatch.ask_fanout sizings arm: sibling runs over existing planned sizings."""

from __future__ import annotations

import json

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


def test_interaction_mode_absent_everywhere_takes_the_fleet_mode(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import sizing_fire

    monkeypatch.setattr(sizing_fire, "_fleet_interaction_mode", lambda: "pm")
    a = _sizing(repo, "a", PLAN_A, mode=None)
    out = mod.fanout({"sizings": [{"sizing": a}]}, repo)
    assert out["exit_code"] == 0, out
    assert _doc(repo, a)["interaction_mode"] == "pm"


def test_interaction_mode_unresolvable_anywhere_refuses_and_writes_nothing(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import sizing_fire

    monkeypatch.setattr(sizing_fire, "_fleet_interaction_mode", lambda: None)
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


HQ = [  # (label, blocked_by) in submission order; hq-01 is not in the batch
    ("hq-03", []), ("hq-05", []), ("hq-04", ["hq-03"]), ("hq-02", ["hq-01"]),
    ("hq-06", ["hq-01"]), ("hq-08", ["hq-01", "hq-03", "hq-05", "hq-06"]),
]


def _hq_items(repo, waves=False):
    items = []
    for n, (label, blocked) in enumerate(HQ):
        rel = _sizing(repo, label, f"docs/plans/2026-10-16-{label}.md")
        item = {"sizing": rel, "label": label, "writes": [f"pkg/{label}.py"], "ids": [label], "blocked_by": blocked}
        if waves:
            item["wave"] = n
        items.append(item)
    return items


def _blocked_by_call(repo, items, **extra):
    return mod.fanout({"sizings": items, c.EDGES_FROM_PARAM: c.EDGES_FROM_BLOCKED_BY, **extra}, repo)


def test_blocked_by_hq_batch_builds_edges_and_held_runs(repo):
    out = _blocked_by_call(repo, _hq_items(repo))
    assert out["exit_code"] == 0, out
    by = {r["label"]: r for r in out["runs"]}
    rid = {label: r["run_id"] for label, r in by.items()}
    assert "after" not in by["hq-03"] and "after" not in by["hq-05"]
    assert by["hq-04"]["after"] == [rid["hq-03"]]
    parent = json.loads((repo / c.parent_record_rel(out["parent_id"])).read_text(encoding="utf-8"))
    status = {r["label"]: r["status"] for r in parent["runs"]}
    for label in ("hq-02", "hq-06", "hq-08"):
        assert status[label] == "held" and "after" not in by[label]
    assert status["hq-04"] == "after" and status["hq-03"] == status["hq-05"] == "ready"
    reasons = {r["label"]: r.get("hold_reason") for r in parent["runs"]}
    assert "hq-01" in reasons["hq-02"] and "hq-01" in reasons["hq-06"] and "hq-01" in reasons["hq-08"]
    assert reasons["hq-03"] is None
    assert out[c.HELD_KEY] == [
        {"run_id": rid[label], "label": label, "reason": reasons[label]} for label in ("hq-02", "hq-06", "hq-08")
    ]
    assert [r["run_id"] for r in out["ready"]] == [rid["hq-03"], rid["hq-05"]]
    assert len(out["fire"]) == 2


def test_blocked_by_mode_wave_adds_no_edge(repo):
    out = _blocked_by_call(repo, _hq_items(repo, waves=True))
    assert out["exit_code"] == 0, out
    by = {r["label"]: r for r in out["runs"]}
    assert "after" not in by["hq-05"]
    assert by["hq-04"]["after"] == [by["hq-03"]["run_id"]]


def test_blocked_by_absent_mode_is_legacy_with_empty_held(repo):
    a, b = _sizing(repo, "a", PLAN_A), _sizing(repo, "b", PLAN_B)
    out = mod.fanout({"sizings": [
        {"sizing": a, "writes": ["pkg"], "ids": ["x"], "blocked_by": ["nope"]},
        {"sizing": b, "writes": ["pkg/b.py"]},
    ]}, repo)
    assert out["exit_code"] == 0, out
    assert out[c.HELD_KEY] == []
    assert out["runs"][1]["after"] == [out["runs"][0]["run_id"]]
    assert all(r.get("status") != "held" for r in out["runs"])


@pytest.mark.parametrize("extra,item_patch,needle", [
    ({c.EDGES_FROM_PARAM: "wave"}, {}, "edges_from"),
    ({c.EDGES_FROM_PARAM: 3}, {}, "edges_from"),
    ({}, {"ids": "a"}, "sizings[0]: ids must be"),
    ({}, {"ids": [""]}, "sizings[0]: ids must be"),
    ({}, {"blocked_by": "a"}, "sizings[0]: blocked_by must be"),
    ({}, {"blocked_by": [1]}, "sizings[0]: blocked_by must be"),
    ({}, {"ids": ["a"], "blocked_by": ["a"]}, "own id"),
])
def test_blocked_by_bad_input_refuses_and_writes_nothing(repo, extra, item_patch, needle):
    a, b = _sizing(repo, "a", PLAN_A), _sizing(repo, "b", PLAN_B)
    before = _snapshot(repo)
    items = [{"sizing": a, "writes": ["x"], "ids": ["a"], **item_patch}, {"sizing": b, "writes": ["y"], "ids": ["b"]}]
    out = mod.fanout({"sizings": items, **({c.EDGES_FROM_PARAM: c.EDGES_FROM_BLOCKED_BY} | extra)}, repo)
    assert out["exit_code"] == 1 and out["applied"] is False and needle in out["error"]
    assert _snapshot(repo) == before


def test_blocked_by_cycle_refuses_and_writes_nothing(repo):
    a, b = _sizing(repo, "a", PLAN_A), _sizing(repo, "b", PLAN_B)
    before = _snapshot(repo)
    out = _blocked_by_call(repo, [
        {"sizing": a, "writes": ["x"], "ids": ["a"], "blocked_by": ["b"]},
        {"sizing": b, "writes": ["y"], "ids": ["b"], "blocked_by": ["a"]},
    ])
    assert out["exit_code"] == 1 and "cycle" in out["error"]
    assert _snapshot(repo) == before


def test_held_run_is_still_stamped_with_its_ask_parent_marker(repo):
    a = _sizing(repo, "a", PLAN_A)
    out = _blocked_by_call(repo, [{"sizing": a, "writes": ["x"], "ids": ["a"], "blocked_by": ["gone"]}])
    assert out["exit_code"] == 0, out
    assert out["ready"] == [] and out["fire"] == []
    assert _doc(repo, a)["em_analysis"]["ask_parent"] == c.ask_parent_marker(out["parent_id"], out["runs"][0]["run_id"])
