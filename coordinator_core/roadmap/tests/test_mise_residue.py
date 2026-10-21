"""mise_residue: batching bounds, census classification, residue args shape."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.roadmap import census, mise_residue as mr


@pytest.fixture(autouse=True)
def _no_git(monkeypatch):
    monkeypatch.setattr(census, "tracked_targets", lambda root, cands: frozenset({"tools/ok.py"}))


def _rows(n):
    return [{"plan": f"docs/plans/p{i}.md"} for i in range(n)]


@pytest.mark.parametrize("n,sizes", [(0, []), (1, [1]), (8, [8]), (9, [8, 1]), (17, [8, 8, 1])])
def test_batch_bounds(n, sizes):
    batches = mr.batch_plans(_rows(n))
    assert [len(b["plans"]) for b in batches] == sizes
    assert [b["id"] for b in batches] == [f"b{i + 1}" for i in range(len(sizes))]


def test_batch_each_plan_once_in_order():
    rows = _rows(10) + [{"plan": "docs/plans/p3.md"}]
    flat = [r["plan"] for b in mr.batch_plans(rows) for r in b["plans"]]
    assert flat == [f"docs/plans/p{i}.md" for i in range(10)]


@pytest.mark.parametrize(
    "command",
    ["node x.js", "cd a && npx tsc", "gh pr list | wc -l", "claude -p hi", "python -c 'print(1)'",
     "echo $(date) && node y.js"],
)
def test_leftover_heads(command):
    assert mr.classify_census(command, census.screen(command) or "") == "leftover"


def test_untracked_python_target_is_leftover():
    command = "python tools/missing.py"
    refusal = census.screen(command, Path("."), frozenset())
    assert mr.classify_census(command, refusal) == "leftover"


@pytest.mark.parametrize(
    "command", ["echo $(date)", "for f in a b; do cat $f; done", "grep x f > out.txt"]
)
def test_rewrite_refusals(command):
    refusal = census.screen(command)
    assert refusal
    assert mr.classify_census(command, refusal) == "rewrite"


PLAN = """---
title: t
census:
  - question: q0
    command: echo $(date)
    result: "1"
  - question: q1
    command: node check.js
    result: "1"
  - question: q2
    command: grep -c x f
    result: "1"
---
body
"""


def test_census_entries_reports_only_unscreenable():
    got = mr.census_entries(PLAN, Path("."), frozenset())
    assert [(e["index"], e["class"]) for e in got] == [(0, "rewrite"), (1, "leftover")]


def test_census_entries_without_census_key():
    assert mr.census_entries("---\ntitle: t\n---\nbody\n", Path("."), frozenset()) == []


def _args(rows, reply=None, texts=None):
    return mr.residue_args(
        rows, reply, repo_root=Path("repo"), run_id="20261019T1630Z-abcd1234",
        gate_command="mise-prep-gate --repo-root repo", residue_agent_type=mr.RESIDUE_AGENT_TYPE,
        residue_model="sonnet", apm_model="opus",
        read_text=lambda root, plan: (texts or {}).get(plan, ""),
    )


def test_residue_args_shape_roundtrips_json():
    rows = [
        {"plan": "a.md", "verdict": "NOT-PREPPED", "failing": ["CENSUS/census-unscreenable"],
         "detail": ["CENSUS: x"], "outcome": "NOT-PREPPED",
         "seam": [{"class": "capabilities-undeclared", "blocking": True, "plan": "a.md"},
                  {"class": "unpromised-export", "blocking": True}]},
        {"plan": "ok.md", "verdict": "PREPPED", "failing": [], "detail": [], "seam": [],
         "outcome": "already-certified"},
    ]
    reply = {"skipped_cycles": [{"class": "seam-fix-cycle", "plans": ["b", "a"], "path": "p", "detail": "d"}]}
    args = _args(rows, reply, {"a.md": PLAN})
    assert json.loads(json.dumps(args)) == args
    assert args["schema"] == "mise-prep-residue-args/1"
    assert args["residue"] == {"agentType": mr.RESIDUE_AGENT_TYPE, "model": "sonnet"}
    assert args["apm"] == {"agentType": "coordinator:apm", "model": "opus"}
    (batch,) = args["batches"]
    (member,) = batch["plans"]
    assert member["plan"] == "a.md"
    assert member["bar"][0] == "bar: CENSUS/census-unscreenable"
    assert len(member["seam"]) == 1 and member["seam"][0].startswith("seam: capabilities-undeclared")
    assert [c["index"] for c in member["census"]] == [0]
    kinds = [i["kind"] for i in args["apmItems"]]
    assert kinds == ["census-leftover", "seam-fix-cycle"]
    assert args["apmItems"][0]["index"] == 1
    assert mr.has_work(args)


def test_empty_when_all_certified():
    rows = [{"plan": "ok.md", "verdict": "PREPPED", "failing": [], "outcome": "stamped", "seam": []}]
    args = _args(rows)
    assert args["batches"] == [] and args["apmItems"] == []
    assert not mr.has_work(args)


def test_prepped_row_with_seam_and_census_yields_no_batch():
    rows = [{"plan": "a.md", "verdict": "PREPPED", "failing": [], "detail": [],
             "outcome": "NOT-PREPPED",
             "seam": [{"class": "writes-collision", "blocking": True, "path": "x"}]}]
    args = _args(rows, texts={"a.md": PLAN})
    assert args["batches"] == []
    assert args["apmItems"] == []


def test_has_work_false_for_prepped_seam_only_rows():
    rows = [{"plan": "a.md", "verdict": "PREPPED", "failing": [], "detail": [],
             "outcome": "NOT-PREPPED",
             "seam": [{"class": "writes-collision", "blocking": True}]}]
    assert not mr.has_work(_args(rows))


def test_non_prepped_row_with_only_seam_still_batches():
    rows = [{"plan": "a.md", "verdict": "NOT-PREPPED", "failing": [], "detail": [],
             "outcome": "NOT-PREPPED",
             "seam": [{"class": "writes-collision", "blocking": True}]}]
    (batch,) = _args(rows)["batches"]
    assert [m["plan"] for m in batch["plans"]] == ["a.md"]
    assert batch["plans"][0]["seam"][0].startswith("seam: writes-collision")


def test_apm_only_when_cycle_alone():
    args = _args([], {"skipped_cycles": [{"plans": ["b", "a"], "path": "p", "detail": "d"}]})
    assert args["batches"] == [] and len(args["apmItems"]) == 1
    assert mr.has_work(args)


def test_nine_plans_make_two_batches():
    rows = [
        {"plan": f"p{i}.md", "verdict": "NOT-PREPPED", "failing": ["X/y"], "detail": [],
         "outcome": "NOT-PREPPED", "seam": []}
        for i in range(9)
    ]
    args = _args(rows)
    assert [len(b["plans"]) for b in args["batches"]] == [8, 1]
