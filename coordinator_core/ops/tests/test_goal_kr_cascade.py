"""discharge_goal_kr: a plan citing <goal_id>#<kr-id> flips that KR to met."""

import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.goal_kr_cascade import discharge_goal_kr

# _goal runs a real git init; needs a real process.
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

GOAL = """schema: goal
id: "goal-x"
title: x
status: active
key_results:
  - id: kr-1
    text: one
    kind: outcome
    status: met
    weekly_perceptible: true
    evidence_source: null
  - id: kr-2
    text: two
    kind: outcome
    status: not-started
    weekly_perceptible: true
    evidence_source: null
"""


def _plan(tmp_path: Path, derived_from: str) -> Path:
    p = tmp_path / "plan.md"
    p.write_text(
        "---\nstatus: implemented\nprime_exit_criterion:\n"
        f"  statement: s\n  derived_from: \"{derived_from}\"\n---\nbody\n",
        encoding="utf-8",
    )
    return p


def _goal(tmp_path: Path) -> Path:
    subprocess.run(
        ["git", "init", "-q"], cwd=tmp_path, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    d = tmp_path / "state" / "goals"
    d.mkdir(parents=True)
    f = d / "g.yaml"
    f.write_text(GOAL, encoding="utf-8")
    return f


def _status(f: Path, kr: str) -> str:
    return next(k["status"] for k in yaml.safe_load(f.read_text("utf-8"))["key_results"] if k["id"] == kr)


def test_cited_kr_flips_to_met(tmp_path):
    g = _goal(tmp_path)
    note = discharge_goal_kr(_plan(tmp_path, "goal-x#kr-2"), tmp_path)
    assert "set to met" in note
    assert _status(g, "kr-2") == "met"
    assert _status(g, "kr-1") == "met"


def test_already_met_kr_is_unchanged(tmp_path):
    g = _goal(tmp_path)
    before = g.read_text("utf-8")
    assert discharge_goal_kr(_plan(tmp_path, "goal-x#kr-1"), tmp_path) is None
    assert g.read_text("utf-8") == before


def test_unlinked_plan_leaves_goal_untouched(tmp_path):
    g = _goal(tmp_path)
    before = g.read_text("utf-8")
    assert discharge_goal_kr(_plan(tmp_path, "state/sizings/a.yaml"), tmp_path) is None
    assert g.read_text("utf-8") == before


def test_goal_in_other_repo_is_reported_not_written(tmp_path):
    g = _goal(tmp_path)
    before = g.read_text("utf-8")
    note = discharge_goal_kr(_plan(tmp_path, "goal-elsewhere#kr-2"), tmp_path)
    assert "not found" in note
    assert g.read_text("utf-8") == before


def test_kr_waits_for_every_citing_plan(tmp_path):
    g = _goal(tmp_path)
    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True)
    a = _plan(plans, "goal-x#kr-2")
    a.rename(plans / "a.md")
    a = plans / "a.md"
    b = plans / "b.md"
    b.write_text(a.read_text("utf-8").replace("status: implemented", "status: active"), encoding="utf-8")
    note = discharge_goal_kr(a, tmp_path)
    assert "held" in note and "b.md" in note
    assert _status(g, "kr-2") == "not-started"
    b.write_text(a.read_text("utf-8"), encoding="utf-8")
    assert "set to met" in discharge_goal_kr(b, tmp_path)
    assert _status(g, "kr-2") == "met"
