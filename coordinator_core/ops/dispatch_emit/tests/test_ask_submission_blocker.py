"""An engine-ask submission names a blocker (`asks[].after` sibling or `after_run` run); the staged run parks on it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from coordinator_core import warp_live
from coordinator_core.ops import baton_pm_turns as PT
from coordinator_core.ops.dispatch_emit import ask_fanout as mod
from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c
from coordinator_core.ops.dispatch_emit import run_token, stage_wait
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.ask_stage import StageWaiting, stage
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import PLAN, PLAN_REL
from coordinator_core.session_baton import store

SID = "sid-submission-blocker"
SCRIPT = "scratch/warp/x.mjs"
SIZING_REL = "state/sizings/2026-10-01-xs-fixture.yaml"
BLOCKER = "ask-20261101T000000-aaaaaa"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "hub" / SID).mkdir(parents=True)
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: tmp_path / "hub" / s)
    assert PT.append_turn("sequence these", session_id=SID)["ok"]
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / PLAN_REL).write_text(PLAN, encoding="utf-8", newline="\n")
    managed = tmp_path.parent / (tmp_path.name + "-managed")
    managed.mkdir(exist_ok=True)
    monkeypatch.setattr(warp_live, "managed_tempdir", lambda: managed)
    monkeypatch.setattr(warp_live, "legacy_top_level_path", lambda name: managed / name)
    (tmp_path / "scratch" / "warp").mkdir(parents=True)
    (tmp_path / "scratch" / "warp" / "mine.workflow.mjs").write_text("// run script\n", encoding="utf-8")
    return tmp_path


def _sizings(repo: Path) -> dict[str, dict]:
    docs = [yaml.safe_load(p.read_text(encoding="utf-8")) for p in (repo / "state" / "sizings").rglob("*.yaml")]
    return {d["intent"]: d for d in docs}


def _fanout(repo: Path, asks: list[dict]) -> dict:
    return mod.fanout({"asks": asks, "session_id": SID}, repo)


def _write_sizing(repo: Path, blockers: list[dict]) -> None:
    doc = {
        "intent": "Fix the thing",
        "exit_criterion": {"statement": "it works"},
        "deliverable_id": "dlv-xs-abc123",
        "em_analysis": {c.STAGE_BLOCKERS_KEY: json.dumps(blockers)},
    }
    path = repo / SIZING_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")


def _stage(repo: Path):
    return stage(repo, run_id="mine", sizing_rel=SIZING_REL, writes=["pkg/a.py"])


def test_after_run_keys_and_shape() -> None:
    assert "after_run" in c.ASKS_ITEM_KEYS
    assert c.parse_after_run("asks[0]", {"run_id": " r1 "}) == ("run_end", "r1")
    assert c.parse_after_run("asks[0]", {"run_id": "r1", "kind": "run_landed"}) == ("run_landed", "r1")
    for bad in ("r1", {"run_id": ""}, {"run_id": "r1", "kind": "file_state"}, {"run_id": "r1", "extra": 1}):
        with pytest.raises(ValueError, match=r"asks\[0\]\.after_run"):
            c.parse_after_run("asks[0]", bad)


def test_stage_blockers_round_trip_dedupes_and_drops_malformed() -> None:
    text = c.encode_stage_blockers([("run_end", "a"), ("run_end", "a"), ("run_landed", "b")])
    assert c.decode_stage_blockers({c.STAGE_BLOCKERS_KEY: text}) == [
        {"kind": "run_end", "ref": "a"}, {"kind": "run_landed", "ref": "b"},
    ]
    assert c.encode_stage_blockers([]) == ""
    for junk in ({}, {c.STAGE_BLOCKERS_KEY: "{bad"}, {c.STAGE_BLOCKERS_KEY: '[{"kind": "remote_run", "ref": "x"}]'}, None):
        assert c.decode_stage_blockers(junk) == []


def test_unknown_run_id_is_refused_and_a_known_one_accepted(repo) -> None:
    out = _fanout(repo, [{"ask": "after the peer", "after_run": {"run_id": BLOCKER}}])
    assert out["exit_code"] == 1 and out["applied"] is False
    assert "asks[0].after_run" in out["error"] and BLOCKER in out["error"]
    run_token.mint(repo, BLOCKER, SCRIPT, session_id=None)
    out = _fanout(repo, [{"ask": "after the peer", "after_run": {"run_id": BLOCKER, "kind": "run_landed"}}])
    assert out["exit_code"] == 0, out
    doc = _sizings(repo)["after the peer"]
    assert c.decode_stage_blockers(doc["em_analysis"]) == [{"kind": "run_landed", "ref": BLOCKER}]


def test_a_bad_after_run_is_refused_naming_the_key(repo) -> None:
    out = _fanout(repo, [{"ask": "x", "after_run": {"run_id": BLOCKER, "kind": "file_state"}}])
    assert out["exit_code"] == 1 and "asks[0].after_run.kind" in out["error"]


def test_sibling_after_maps_to_a_run_end_blocker_on_the_sibling_run(repo) -> None:
    out = _fanout(repo, [{"ask": "first", "label": "a"}, {"ask": "second", "label": "b", "after": ["a"]}])
    assert out["exit_code"] == 0, out
    first_run = next(r["run_id"] for r in out["runs"] if r["label"] == "a")
    docs = _sizings(repo)
    assert c.decode_stage_blockers(docs["second"]["em_analysis"]) == [{"kind": "run_end", "ref": first_run}]
    assert c.STAGE_BLOCKERS_KEY not in docs["first"]["em_analysis"]


def test_the_staged_run_parks_while_the_blocker_lives_and_stages_once_it_ends(repo) -> None:
    run_token.mint(repo, BLOCKER, SCRIPT, session_id=None)
    _write_sizing(repo, [{"kind": "run_end", "ref": BLOCKER}])
    run_token.mint(repo, "mine", SCRIPT, session_id=None)
    with pytest.raises(StageWaiting) as parked:
        _stage(repo)
    assert parked.value.wait["typed_blockers"] == [{"kind": "run_end", "ref": BLOCKER}]
    assert run_token.is_live(repo, "mine")
    stage_wait.clear(repo, "mine")
    run_token.discard(repo, BLOCKER)
    assert [r.id for r in _stage(repo).rows] == ["X1"]
    assert stage_wait.read(repo, "mine") is None


def test_a_run_landed_blocker_holds_until_its_landed_marker_reads(repo) -> None:
    run_token.mint(repo, BLOCKER, SCRIPT, session_id=None)
    run_token.discard(repo, BLOCKER)
    _write_sizing(repo, [{"kind": "run_landed", "ref": BLOCKER}])
    run_token.mint(repo, "mine", SCRIPT, session_id=None)
    with pytest.raises(StageWaiting) as parked:
        _stage(repo)
    assert parked.value.wait["typed_blockers"] == [{"kind": "run_landed", "ref": BLOCKER}]
    stage_wait.clear(repo, "mine")
    marker = record_dir(repo, BLOCKER)
    marker.mkdir(parents=True, exist_ok=True)
    (marker / "halted.json").write_text(json.dumps({"status": "landed", "sha": "a" * 40}), encoding="utf-8")
    assert [r.id for r in _stage(repo).rows] == ["X1"]


def test_a_run_end_blocker_with_no_end_evidence_still_parks(repo) -> None:
    _write_sizing(repo, [{"kind": "run_end", "ref": BLOCKER}])
    run_token.mint(repo, "mine", SCRIPT, session_id=None)
    with pytest.raises(StageWaiting):
        _stage(repo)


def test_an_expired_submission_wait_refuses_and_releases_the_token(repo) -> None:
    from coordinator_core.ops.dispatch_emit.ask_stage_blocks import AskStageError

    _write_sizing(repo, [{"kind": "run_end", "ref": BLOCKER}])
    run_token.mint(repo, "mine", SCRIPT, session_id=None)
    stage_wait.park(repo, "mine", [BLOCKER], [], now=1.0)
    with pytest.raises(AskStageError, match="submission-blocker-expired") as err:
        _stage(repo)
    assert not isinstance(err.value, StageWaiting)
    assert not run_token.is_live(repo, "mine")


def test_a_sizing_without_stage_blockers_stages_unparked(repo) -> None:
    doc = {"intent": "Fix the thing", "exit_criterion": {"statement": "it works"}, "deliverable_id": "dlv-xs-abc123"}
    (repo / "state" / "sizings").mkdir(parents=True)
    (repo / SIZING_REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    assert [r.id for r in _stage(repo).rows] == ["X1"]
