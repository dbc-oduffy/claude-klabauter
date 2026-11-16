"""A stage refusal ends its run; a same-sizing successor is never refused on a predecessor's hold."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import ask_stage, live_run_index, run_token
from coordinator_core.ops.dispatch_emit import shared_path_landing as spl
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import PLAN_REL
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage_live_overlap import SCRIPT, repo  # noqa: F401

pytestmark = pytest.mark.complete

DEAD = "ask-20261114T000000-cfb019"
OWN = "ask-20261115T000000-b8b26f"
OTHER = "ask-20261115T000000-aaaaaa"
SIZING = "state/sizings/2026-11/s.yaml"


def _live(root, run_id):
    return any(r["run_id"] == run_id for r in run_token.live_records(root))


def _indexed(root, run_id):
    index = live_run_index.read(root, 500)
    return isinstance(index, dict) and run_id in index


@pytest.mark.parametrize("refusal", [ask_stage.AskStageError("refused"), ask_stage.SizingFireRefused("refused")])
def test_a_refused_stage_leaves_no_token_and_no_index_entry(repo, monkeypatch, refusal):
    run_token.mint(repo, "mine", SCRIPT, session_id=None)
    assert live_run_index.add(repo, "mine", 1.0)
    assert _live(repo, "mine") and _indexed(repo, "mine")

    def refuse(*_a, **_k):
        raise refusal

    monkeypatch.setattr(ask_stage, "stage", refuse)
    reply = ask_stage._handler({"run_id": "mine", "plan_path": PLAN_REL}, repo)
    assert "error" in reply
    assert not _live(repo, "mine")
    assert not _indexed(repo, "mine")


def test_a_successful_stage_keeps_its_token(repo):
    run_token.mint(repo, "mine", SCRIPT, session_id=None)
    reply = ask_stage._handler({"run_id": "mine", "plan_path": PLAN_REL}, repo)
    assert "manifest_handle" in reply
    assert _live(repo, "mine")


def _run(root, run_id, sizing, stage="minted"):
    rdir = root / "state" / "warp" / run_id
    rdir.mkdir(parents=True)
    (rdir / "run.json").write_text(json.dumps({"sizing_rel": sizing}), encoding="utf-8")
    (rdir / "run-stage.json").write_text(json.dumps({"stage": stage}), encoding="utf-8")


def test_a_markerless_earlier_run_of_the_same_sizing_is_released(tmp_path):
    _run(tmp_path, DEAD, SIZING)
    _run(tmp_path, OWN, SIZING)
    assert spl.released_holder(tmp_path, DEAD, OWN)
    assert spl.sibling_held_product(
        [("C1", "a/x.py", "peer-held")], {"a/x.py": [DEAD]}, root=tmp_path, own_run_id=OWN
    ) == []


def test_a_live_run_of_another_sizing_still_holds(tmp_path):
    _run(tmp_path, OTHER, "state/sizings/2026-11/other.yaml")
    _run(tmp_path, OWN, SIZING)
    assert not spl.released_holder(tmp_path, OTHER, OWN)
    assert spl.sibling_held_product(
        [("C1", "a/x.py", "peer-held")], {"a/x.py": [OTHER]}, root=tmp_path, own_run_id=OWN
    ) == [("C1", "a/x.py", [OTHER])]


def test_a_holder_with_no_recorded_sizing_still_holds(tmp_path):
    _run(tmp_path, DEAD, "")
    _run(tmp_path, OWN, "")
    assert not spl.released_holder(tmp_path, DEAD, OWN)


def test_a_later_run_of_the_same_sizing_still_holds_against_an_earlier_one(tmp_path):
    _run(tmp_path, DEAD, SIZING)
    _run(tmp_path, OWN, SIZING)
    assert not spl.released_holder(tmp_path, OWN, DEAD)


def test_a_refused_stage_keeps_a_launcher_childs_escrowed_token(repo, monkeypatch):
    token = run_token.mint(repo, "mine", SCRIPT, session_id=None)
    assert run_token.escrow(repo, "mine", token)
    assert live_run_index.add(repo, "mine", 1.0)

    def refuse(*_a, **_k):
        raise ask_stage.AskStageError("refused")

    monkeypatch.setattr(ask_stage, "stage", refuse)
    assert "error" in ask_stage._handler({"run_id": "mine", "plan_path": PLAN_REL}, repo)
    assert _live(repo, "mine")
    assert run_token.escrowed(repo, "mine") == token
    assert not _indexed(repo, "mine")


def test_a_parked_stage_wait_keeps_its_token_and_index_entry(repo, monkeypatch):
    run_token.mint(repo, "mine", SCRIPT, session_id=None)
    assert live_run_index.add(repo, "mine", 1.0)

    def park(*_a, **_k):
        raise ask_stage.StageWaiting("waiting", {"blockers": ["peer"], "deadline": 0})

    monkeypatch.setattr(ask_stage, "stage", park)
    reply = ask_stage._handler({"run_id": "mine", "plan_path": PLAN_REL}, repo)
    assert reply.get(ask_stage.PARKED_REPLY_KEY) == ask_stage.STAGE_WAIT_PARKED
    assert _live(repo, "mine")
    assert _indexed(repo, "mine")
