"""Group join end to end: dispatch.ask_fanout mints it, closes and dispatch.sibling_status release it.

The R2 shape: a 2-ask group whose join waits on a local run already in flight (no sibling record,
only its run directory) and on a run in a second repo.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.hooks import submit_engine_ask as hook
from coordinator_core.ops import baton_pm_turns as PT
from coordinator_core.ops.dispatch_emit import ask_fanout as fanout_op
from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c
from coordinator_core.ops.dispatch_emit import run_close, sibling_status
from coordinator_core.session_baton import store
from coordinator_core.warp_fire import fire_command

SID = "sid-group-join-e2e"
SHA = "b" * 40
LOCAL_IN = "pipeline-research-20261102T113444-fedb8e"
FOREIGN_IN = "ask-20261102T120137-d2a4df"
ASKS = [{"ask": "research one", "label": "a"}, {"ask": "research two", "label": "b"}]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "local"
    (root / ".git").mkdir(parents=True)
    (tmp_path / "hub" / SID).mkdir(parents=True)
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: tmp_path / "hub" / s)
    assert PT.append_turn("pm turn", session_id=SID)["ok"]
    (root / "scratch" / "warp" / LOCAL_IN).mkdir(parents=True)
    return root


@pytest.fixture
def foreign(tmp_path):
    root = tmp_path / "other"
    _status(root, FOREIGN_IN, c.STATUS_RUNNING)
    return root


def _status(root: Path, run_id: str, status: str, **extra) -> None:
    path = root / c.status_rel(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"status": status, **extra}), encoding="utf-8")


def _tree(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _mint(repo: Path, foreign: Path) -> tuple[dict, c.ParentRecord]:
    join = {"ask": "join the research", "inputs": [{"run_id": LOCAL_IN}, {"run_id": FOREIGN_IN, "repo": str(foreign)}]}
    out = fanout_op._handler({"asks": ASKS, "join": join, "session_id": SID}, repo)
    assert out["exit_code"] == 0, out
    record = json.loads((repo / c.parent_record_rel(out["parent_id"])).read_text(encoding="utf-8"))
    return out, c.ParentRecord.from_json(record)


def _close_siblings(repo: Path, parent: c.ParentRecord) -> list:
    """Bind each sibling as its slice would (running, with its parent), then close it landed."""
    replies = []
    for run in parent.runs[:2]:
        _status(repo, run.run_id, c.STATUS_RUNNING, parent_id=parent.parent_id)
        replies.append(run_close.sibling_release(repo, run.run_id, SHA))
    return replies


def test_mint_makes_the_join_the_last_run_waiting_on_both_inputs(repo, foreign):
    out, parent = _mint(repo, foreign)
    join = parent.runs[-1]
    assert [r.label for r in parent.runs] == ["a", "b", c.JOIN_LABEL]
    assert join.status == c.STATUS_AFTER
    assert join.after == tuple(r.run_id for r in parent.runs[:2])
    assert join.after_external == (c.ExternalInput(LOCAL_IN), c.ExternalInput(FOREIGN_IN, str(foreign)))
    assert [r["run_id"] for r in out["ready"]] == list(join.after)
    assert json.loads((repo / c.join_waiters_rel(LOCAL_IN)).read_text(encoding="utf-8")) == [out["parent_id"]]


def test_join_is_held_while_an_input_is_unlanded(repo, foreign):
    out, parent = _mint(repo, foreign)
    join = parent.runs[-1]
    replies = _close_siblings(repo, parent)
    assert all(r is not None and r["released"] == [] for r in replies)
    why = hook._slice_deny(repo, (parent.parent_id, join.run_id))
    assert why and LOCAL_IN in why and f"dispatch.sibling_status {{parent_id: {parent.parent_id}}}" in why
    statuses = sibling_status.sibling_status({"parent_id": parent.parent_id}, repo)
    assert statuses["released"] == [] and statuses["blocked"] == []


def test_a_local_inputs_close_releases_the_join_through_join_waiters(repo, foreign):
    _, parent = _mint(repo, foreign)
    join = parent.runs[-1]
    _status(foreign, FOREIGN_IN, c.STATUS_LANDED)
    assert all(r["released"] == [] for r in _close_siblings(repo, parent))
    assert not (repo / c.status_rel(LOCAL_IN)).exists()
    reply = run_close.sibling_release(repo, LOCAL_IN, SHA)
    assert reply["released"] == [fire_command(join.slice_sizing)]
    assert reply["join_parents"] == [parent.parent_id]
    assert json.loads((repo / c.status_rel(LOCAL_IN)).read_text(encoding="utf-8")) == {"status": c.STATUS_LANDED, "sha": SHA}
    assert hook._slice_deny(repo, (parent.parent_id, join.run_id)) is None


def test_a_cross_repo_input_landing_surfaces_through_sibling_status(repo, foreign):
    _, parent = _mint(repo, foreign)
    join = parent.runs[-1]
    run_close.sibling_release(repo, LOCAL_IN, SHA)
    _close_siblings(repo, parent)
    held = sibling_status.sibling_status({"parent_id": parent.parent_id}, repo)
    assert held["released"] == []
    assert {i["run_id"]: i["status"] for i in held["join_inputs"]} == {
        LOCAL_IN: c.STATUS_LANDED,
        FOREIGN_IN: c.STATUS_RUNNING,
    }
    assert [i.get("repo") for i in held["join_inputs"]] == [None, str(foreign)]
    _status(foreign, FOREIGN_IN, c.STATUS_LANDED)
    before_foreign = _tree(foreign)
    pulled = sibling_status.sibling_status({"parent_id": parent.parent_id}, repo)
    assert pulled["released"] == [fire_command(join.slice_sizing)]
    assert {i["run_id"]: i["status"] for i in pulled["join_inputs"]}[FOREIGN_IN] == c.STATUS_LANDED
    assert _tree(foreign) == before_foreign


def test_a_halted_unclosed_input_blocks_the_join(repo, foreign):
    _, parent = _mint(repo, foreign)
    join = parent.runs[-1]
    _status(foreign, FOREIGN_IN, c.STATUS_LANDED)
    reply = run_close.sibling_release(repo, LOCAL_IN, None)
    assert reply["released"] == []
    _close_siblings(repo, parent)
    view = sibling_status.sibling_status({"parent_id": parent.parent_id}, repo)
    assert view["released"] == []
    assert view["blocked"] == [{"run_id": join.run_id, "slice_sizing": join.slice_sizing}]
