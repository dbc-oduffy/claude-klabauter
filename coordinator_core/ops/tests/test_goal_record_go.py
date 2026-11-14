"""Tests for the goal.record_go op."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from coordinator_core.ops import goal_record_go as op

GOAL = (
    "schema: goal\nid: \"goal-x\"\ntitle: \"x\"\nstatus: active\nobjective: \"o\"\n"
    "key_results:\n  - id: kr-1\n    text: \"t\"\ncreated: 2026-10-07\n# trailing comment\n"
)


def _plain_rmw(path, mutate, *, repo_root):
    old = path.read_text(encoding="utf-8")
    new = mutate(old)
    if new != old:
        path.write_text(new, encoding="utf-8", newline="\n")
    return new


@pytest.fixture(autouse=True)
def _root(monkeypatch):
    monkeypatch.setattr(op, "main_worktree_root", lambda r: Path(r))
    monkeypatch.setattr(op, "locked_rmw", _plain_rmw)


def _goal(root: Path, text: str = GOAL) -> Path:
    p = root / "state" / "goals" / "2026-10-07-x.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _seed(root: Path, hid: str, goal: str = "goal-x", state: str = "awaiting_gate", kind: str = "roadmap-seed"):
    p = root / "state" / "handoffs" / f"2026-10-07-{hid}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"---\nkind: {kind}\ntitle: t\nhandoff_id: {hid}\ndeployment_state: {state}\n"
        f"origin_goal_id:\n  - \"{goal}\"\n---\nbody\n",
        encoding="utf-8",
    )


def _params(**kw):
    return {"goal": "goal-x", "who": "pm", "quote": "go go go", **kw}


def test_records_go_with_discovered_seeds_and_is_idempotent(tmp_path):
    g = _goal(tmp_path)
    _seed(tmp_path, "hnd-a-111111")
    _seed(tmp_path, "hnd-b-222222", state="shipped")
    _seed(tmp_path, "hnd-c-333333", goal="goal-other")
    _seed(tmp_path, "hnd-d-444444", kind="roadmap-baton")
    r = op._handler(_params(at="2026-10-08T00:00:00Z"), tmp_path)
    assert r["exit_code"] == 0 and r["applied"] is True and r["seeds"] == ["hnd-a-111111"]
    rec = yaml.safe_load(g.read_text(encoding="utf-8"))["goal_go"]
    assert rec == [{"scope": "goal", "who": "pm", "at": "2026-10-08T00:00:00Z",
                    "quote": "go go go", "seeds": ["hnd-a-111111"]}]
    assert op._handler(_params(), tmp_path)["applied"] is False


def test_second_go_appends_to_existing_list(tmp_path):
    g = _goal(tmp_path)
    _seed(tmp_path, "hnd-a-111111")
    op._handler(_params(), tmp_path)
    r = op._handler(_params(who="apm", quote="", ruling_ref="state/rulings/r.md"), tmp_path)
    assert r["applied"] is True
    data = yaml.safe_load(g.read_text(encoding="utf-8"))
    assert [x["who"] for x in data["goal_go"]] == ["pm", "apm"]
    assert data["goal_go"][1]["ruling_ref"] == "state/rulings/r.md" and "quote" not in data["goal_go"][1]


@pytest.mark.parametrize(
    "mutate, needle",
    [
        ({"goal": "nope"}, "goal not found"),
        ({"who": ""}, "who"),
        ({"quote": ""}, "quote or ruling_ref"),
        ({"seeds": ["hnd-zzz-999999"]}, "not a live roadmap-seed"),
    ],
)
def test_refusals_leave_the_goal_untouched(tmp_path, mutate, needle):
    g = _goal(tmp_path)
    _seed(tmp_path, "hnd-a-111111")
    r = op._handler(_params(**mutate), tmp_path)
    assert r["exit_code"] == 1 and needle in r["error"]
    assert g.read_text(encoding="utf-8") == GOAL


def test_refuses_inactive_goal_and_empty_seed_set(tmp_path):
    _goal(tmp_path, GOAL.replace("status: active", "status: achieved"))
    _seed(tmp_path, "hnd-a-111111")
    assert "not active" in op._handler(_params(), tmp_path)["error"]
    _goal(tmp_path)
    (tmp_path / "state" / "handoffs" / "2026-10-07-hnd-a-111111.md").unlink()
    assert "no live roadmap-seeds" in op._handler(_params(), tmp_path)["error"]
