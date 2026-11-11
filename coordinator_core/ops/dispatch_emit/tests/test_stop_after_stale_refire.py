"""A stop_after=plan marker written after its pass staged is stale: a refire (same run or descendant) is not
parked by it, while a marker set before the pass staged still parks. Drives `dispatch.ask_stage`'s handler."""
from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import ask_contract as ac, ask_stage
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import PLAN as STAGEABLE_PLAN, PLAN_REL as STAGEABLE_REL



def _is_parked(reply, *, run_id, inherited):
    assert reply[ac.PARKED_REPLY_KEY] == ac.STOP_AFTER_PLAN and reply["plan_path"] == STAGEABLE_REL
    assert reply[ac.PARK_SOURCE_REPLY_KEY] == {"from": "em", "inherited": inherited, "run_id": run_id}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / STAGEABLE_REL).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / STAGEABLE_REL).write_text(STAGEABLE_PLAN, encoding="utf-8", newline="\n")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _run(root, rid, *, marker_at=None, staged_at=None, **links):
    d = ac.record_dir(root, rid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "run.json").write_text(json.dumps(links), encoding="utf-8")
    if marker_at:
        (d / ac.STOP_AFTER_FILE).write_text(
            json.dumps({"stop_after": "plan", "at": marker_at, "from": "em"}), encoding="utf-8"
        )
    if staged_at:
        ac.write_run_stage_at(d, "staged", now=staged_at, session_id="s")
    return d


def _stage(root, rid):
    return ask_stage._handler({"run_id": rid, "plan_path": STAGEABLE_REL, "writes": []}, repo_root=root)


def test_staged_stamp_is_first_stage_time(tmp_path):
    d = _run(tmp_path, "r1", staged_at="2026-11-05T15:00:00Z")
    ac.write_run_stage_at(d, "staged", now="2026-11-05T16:00:00Z")
    ac.write_run_stage_at(d, "halted", now="2026-11-05T17:00:00Z")
    stage = json.loads((d / ac.RUN_STAGE_FILE).read_text())
    assert stage["staged_at"] == "2026-11-05T15:00:00Z" and stage["stage"] == "halted"


def test_marker_set_before_pass_staged_still_parks(repo):
    _run(repo, "r1", marker_at="2026-11-05T15:00:00Z", staged_at="2026-11-05T15:30:00Z")
    assert ac.stop_after_plan_live(ac.record_dir(repo, "r1"))
    _is_parked(_stage(repo, "r1"), run_id="r1", inherited=False)


def test_unstaged_pass_marker_parks(repo):
    _run(repo, "r1", marker_at="2026-11-05T15:00:00Z")
    _is_parked(_stage(repo, "r1"), run_id="r1", inherited=False)


def test_marker_set_after_pass_staged_is_stale_for_own_refire(repo):
    _run(repo, "r1", marker_at="2026-11-05T15:49:00Z", staged_at="2026-11-05T15:30:00Z")
    assert not ac.stop_after_plan_live(ac.record_dir(repo, "r1"))
    assert ac.lineage_stop_after(repo, "r1") is None
    assert not ask_stage._consume_park_marker(repo, "r1")


def test_ancestor_stale_marker_does_not_park_descendant_refire(repo):
    _run(repo, "ask-58f49d", marker_at="2026-11-05T15:49:00Z", staged_at="2026-11-05T15:30:00Z")
    _run(repo, "ask-14f577", resumes_ask="ask-58f49d")
    assert ac.lineage_stop_after(repo, "ask-14f577") is None
    assert not ask_stage._consume_park_marker(repo, "ask-14f577")
    assert ac.PARKED_REPLY_KEY not in _stage(repo, "ask-14f577")


def test_ancestor_live_marker_still_parks_descendant(repo):
    _run(repo, "ask-58f49d", marker_at="2026-11-05T15:00:00Z", staged_at="2026-11-05T15:30:00Z")
    _run(repo, "ask-14f577", resumes_ask="ask-58f49d")
    _is_parked(_stage(repo, "ask-14f577"), run_id="ask-58f49d", inherited=True)


def test_stale_ancestor_does_not_mask_live_marker_further_up(repo):
    _run(repo, "gp", marker_at="2026-11-05T15:00:00Z")
    _run(repo, "ask-58f49d", marker_at="2026-11-05T15:49:00Z", staged_at="2026-11-05T15:30:00Z", resumes_ask="gp")
    _run(repo, "child", resumes_ask="ask-58f49d")
    assert ac.lineage_stop_after(repo, "child") == "plan"


def test_marker_without_at_or_pass_without_staged_at_is_live(tmp_path):
    d = _run(tmp_path, "r1")
    (d / ac.STOP_AFTER_FILE).write_text(json.dumps({"stop_after": "plan"}), encoding="utf-8")
    ac.write_run_stage_at(d, "staged", now="2026-11-05T15:30:00Z")
    assert ac.stop_after_plan_live(d)
