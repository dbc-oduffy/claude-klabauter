"""dispatch.ask_fanout `join`: the final slice, its inputs, the refusals and the waiters index."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from coordinator_core.ops import baton_pm_turns as PT
from coordinator_core.ops.dispatch_emit import ask_fanout as mod
from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c
from coordinator_core.session_baton import store

SID = "sid-fanout-join"
ASKS = [{"ask": "one", "label": "a"}, {"ask": "two", "label": "b"}]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "local"
    (root / ".git").mkdir(parents=True)
    (tmp_path / "hub" / SID).mkdir(parents=True)
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: tmp_path / "hub" / s)
    assert PT.append_turn("pm turn", session_id=SID)["ok"]
    return root


@pytest.fixture
def foreign(tmp_path):
    (tmp_path / "other").mkdir()
    return tmp_path / "other"


def _status(root, run_id, status="running", **extra):
    path = root / c.status_rel(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"status": status, **extra}), encoding="utf-8")


def _go(repo, join, asks=ASKS, **extra):
    params = {"asks": asks, "session_id": SID, **extra}
    if join is not None:
        params["join"] = join
    return mod.fanout(params, repo)


def _snapshot(root):
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def _parent(repo, out):
    return c.ParentRecord.from_json(json.loads((repo / c.parent_record_rel(out["parent_id"])).read_text(encoding="utf-8")))


def test_join_is_the_last_run_after_every_sibling_and_waits_on_unlanded_inputs(repo, foreign):
    _status(repo, "plain-run")
    _status(foreign, "far-run")
    out = _go(repo, {"ask": "merge it", "inputs": [{"run_id": "plain-run"}, {"run_id": "far-run", "repo": str(foreign)}]})
    assert out["exit_code"] == 0, out
    parent = _parent(repo, out)
    join = parent.runs[-1]
    assert [r.label for r in parent.runs] == ["a", "b", c.JOIN_LABEL]
    assert join.status == c.STATUS_AFTER
    assert join.after == tuple(r.run_id for r in parent.runs[:2])
    assert join.after_external == (c.ExternalInput("plain-run"), c.ExternalInput("far-run", str(foreign)))
    assert [r["run_id"] for r in out["ready"]] == [r.run_id for r in parent.runs[:2]]
    assert out["runs"][-1]["after"] == list(join.after)


def test_join_registers_a_waiter_for_a_local_input_only(repo, foreign):
    _status(repo, "plain-run")
    _status(foreign, "far-run")
    before = _snapshot(foreign)
    out = _go(repo, {"ask": "merge", "inputs": [{"run_id": "plain-run"}, {"run_id": "far-run", "repo": str(foreign)}]})
    assert json.loads((repo / c.join_waiters_rel("plain-run")).read_text(encoding="utf-8")) == [out["parent_id"]]
    assert _snapshot(foreign) == before
    assert not list(repo.glob(".coordinator-local/fanout-staging/*/*"))


def test_a_second_join_on_the_same_input_merges_its_parent_in(repo):
    _status(repo, "plain-run")
    first = _go(repo, {"ask": "m", "inputs": [{"run_id": "plain-run"}]})
    second = _go(repo, {"ask": "m", "inputs": [{"run_id": "plain-run"}]})
    held = json.loads((repo / c.join_waiters_rel("plain-run")).read_text(encoding="utf-8"))
    assert held == [first["parent_id"], second["parent_id"]]


def test_an_already_landed_input_adds_no_wait(repo, foreign):
    _status(repo, "done-run", "landed")
    _status(foreign, "far-done", "halted", rows_committed=True)
    out = _go(repo, {"ask": "m", "inputs": [{"run_id": "done-run"}, {"run_id": "far-done", "repo": str(foreign)}]})
    assert out["exit_code"] == 0, out
    join = _parent(repo, out).runs[-1]
    assert join.after_external == () and join.after
    assert not (repo / c.join_waiters_rel("done-run")).exists()


def test_a_run_directory_alone_makes_a_local_input_known(repo):
    (repo / "scratch" / "warp" / "pipeline-research-x").mkdir(parents=True)
    out = _go(repo, {"ask": "m", "inputs": [{"run_id": "pipeline-research-x"}]})
    assert out["exit_code"] == 0, out
    assert _parent(repo, out).runs[-1].after_external == (c.ExternalInput("pipeline-research-x"),)


def test_a_run_listed_in_a_parent_record_makes_a_local_input_known(repo):
    first = _go(repo, None, asks=ASKS)
    sibling = _parent(repo, first).runs[0].run_id
    out = _go(repo, {"ask": "m", "inputs": [{"run_id": sibling}]})
    assert out["exit_code"] == 0, out


def test_the_repo_naming_this_repo_reads_as_local(repo):
    _status(repo, "plain-run")
    out = _go(repo, {"ask": "m", "inputs": [{"run_id": "plain-run", "repo": str(repo)}]})
    assert out["exit_code"] == 0, out
    assert _parent(repo, out).runs[-1].after_external == (c.ExternalInput("plain-run"),)


@pytest.mark.parametrize(
    "inputs, named",
    [
        ([{"run_id": "ghost"}], "join.inputs[0]"),
        ([{"run_id": "../escape"}], "join.inputs[0]"),
        ([{"run_id": "plain-run"}, {"run_id": "x", "repo": "relative/dir"}], "join.inputs[1]"),
        ([{"run_id": "plain-run"}, {"run_id": "x", "repo": "NOPE_MISSING_DIR"}], "join.inputs[1]"),
        ([{"run_id": "plain-run", "extra": 1}], "join.inputs[0]: unknown key"),
        ([], "join.inputs"),
        (["plain-run"], "join.inputs[0]"),
    ],
)
def test_a_bad_input_refuses_the_whole_submission_before_any_write(repo, inputs, named):
    _status(repo, "plain-run")
    before = _snapshot(repo)
    out = _go(repo, {"ask": "m", "inputs": inputs})
    assert out["exit_code"] == 1 and out["applied"] is False and named in out["error"], out
    assert _snapshot(repo) == before


def test_a_foreign_input_with_no_status_file_is_refused_naming_the_path(repo, foreign):
    (foreign / "scratch" / "warp" / "far-run").mkdir(parents=True)
    out = _go(repo, {"ask": "m", "inputs": [{"run_id": "far-run", "repo": str(foreign)}]})
    assert out["exit_code"] == 1
    assert "join.inputs[0]" in out["error"] and "sibling-status.json" in out["error"]


def test_an_absolute_repo_that_is_not_an_existing_directory_is_refused(repo, tmp_path):
    missing = str(tmp_path / "no-such-repo")
    out = _go(repo, {"ask": "m", "inputs": [{"run_id": "x", "repo": missing}]})
    assert out["exit_code"] == 1 and "join.inputs[0]" in out["error"]


def test_unknown_join_key_is_refused_naming_it(repo):
    _status(repo, "plain-run")
    out = _go(repo, {"ask": "m", "inputs": [{"run_id": "plain-run"}], "after": [0]})
    assert out["exit_code"] == 1 and "unknown key 'after'" in out["error"]


@pytest.mark.parametrize(
    "params",
    [
        {"slices": [{"label": "s", "ask": "a", "rows": ["R1"]}], "parent_sizing": "state/sizings/p.yaml"},
        {"sizings": [{"sizing": "state/sizings/p.yaml"}]},
    ],
)
def test_join_is_refused_on_the_other_arms(repo, params):
    _status(repo, "plain-run")
    before = _snapshot(repo)
    out = mod.fanout({**params, "join": {"ask": "m", "inputs": [{"run_id": "plain-run"}]}, "session_id": SID}, repo)
    assert out["exit_code"] == 1 and "asks arm only" in out["error"], out
    assert _snapshot(repo) == before


def test_join_label_may_not_collide_with_an_ask_label(repo):
    _status(repo, "plain-run")
    out = _go(repo, {"ask": "m", "inputs": [{"run_id": "plain-run"}]}, asks=[{"ask": "x", "label": "join"}])
    assert out["exit_code"] == 1 and "already an asks label" in out["error"]


def test_no_join_leaves_the_submission_unchanged(repo):
    out = _go(repo, None)
    assert out["exit_code"] == 0 and [r["label"] for r in out["runs"]] == ["a", "b"]
    assert all(r.after_external == () for r in _parent(repo, out).runs)


def test_join_costs_no_spawn(repo, foreign, monkeypatch):
    _status(repo, "plain-run")
    _status(foreign, "far-run")
    spawns = []
    real = subprocess.Popen
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawns.append(a) or real(*a, **k))
    monkeypatch.setattr(os, "system", lambda *a, **k: spawns.append(a))
    out = _go(repo, {"ask": "m", "inputs": [{"run_id": "plain-run"}, {"run_id": "far-run", "repo": str(foreign)}]})
    assert out["exit_code"] == 0 and not spawns


def test_rollback_keeps_a_preexisting_waiters_file(repo):
    _status(repo, "plain-run")
    waiters = repo / c.join_waiters_rel("plain-run")
    waiters.write_text('["ask-parent-old"]\n', encoding="utf-8")
    polls = iter(range(100))
    out = mod.fanout(
        {"session_id": SID, "asks": ASKS, "join": {"ask": "m", "inputs": [{"run_id": "plain-run"}]}},
        repo,
        lambda: next(polls) >= 7,
    )
    assert out["exit_code"] == 1 and "not taken" in out["error"], out
    assert json.loads(waiters.read_text(encoding="utf-8"))[0] == "ask-parent-old"
