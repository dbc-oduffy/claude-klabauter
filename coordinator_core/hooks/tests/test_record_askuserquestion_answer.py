"""Tests for hooks.record_askuserquestion_answer: PM answers land in pm_turns with question context."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.authz.classification import OpClass, classify
from coordinator_core.hooks.record_askuserquestion_answer import _handler
from coordinator_core.ops import baton_pm_turns
from coordinator_core.session_baton import store
from coordinator_core.session_hub_path import HUB_NAME
from coordinator_core.warm.hook_http import HOOK_PATH, op_for_path

SID = "sid-auq"
QUESTION = "Publish klabauter now?"


def _session(tmp_path: Path, monkeypatch, sid: str = SID) -> Path:
    sdir = tmp_path / HUB_NAME / sid
    sdir.mkdir(parents=True)
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: tmp_path / HUB_NAME / s)
    return sdir


def _payload(answers, **extra) -> dict:
    payload = {
        "session_id": SID,
        "cwd": "",
        "tool_name": "AskUserQuestion",
        "tool_input": {"questions": [{
            "question": QUESTION,
            "header": "Publish",
            "multiSelect": False,
            "options": [{"label": "Yes", "description": "go"}, {"label": "No", "description": "hold"}],
        }]},
        "tool_response": {"answers": answers},
    }
    payload.update(extra)
    return payload


def test_registered_routable_and_classified_mutating() -> None:
    from coordinator_core.ipc import _REGISTRY

    assert "hooks.record_askuserquestion_answer" in _REGISTRY
    assert op_for_path(HOOK_PATH + "/hooks.record_askuserquestion_answer") == \
        "hooks.record_askuserquestion_answer"
    assert classify("hooks.record_askuserquestion_answer") is OpClass.MUTATING


def test_answer_is_read_back_through_pm_turns_with_question_referent(tmp_path, monkeypatch):
    _session(tmp_path, monkeypatch)
    assert _handler({"payload": _payload({QUESTION: "Yes"})}) == {}

    from coordinator_core.ipc import _REGISTRY

    read = _REGISTRY["baton.pm_turns"]({"session_id": SID, "all": True})
    assert read["ok"] and read["count"] == 1
    turn = read["turns"][0]
    assert turn["prompt"] == "Yes"
    assert QUESTION in turn["referent"]["excerpt"]
    assert "Yes; No" in turn["referent"]["excerpt"]


def test_one_turn_per_answered_question_and_multiselect_joined(tmp_path, monkeypatch):
    _session(tmp_path, monkeypatch)
    payload = _payload({QUESTION: "Yes", "Which targets?": ["a", "b"]})
    _handler({"payload": payload})
    turns = baton_pm_turns.read_turns(SID, all_turns=True)["turns"]
    assert [t["prompt"] for t in turns] == ["Yes", "a, b"]
    assert turns[1]["referent"]["excerpt"] == "Which targets?"


def test_flat_params_shape_is_accepted(tmp_path, monkeypatch):
    _session(tmp_path, monkeypatch)
    _handler(_payload({QUESTION: "No"}))
    assert baton_pm_turns.read_turns(SID)["text"] == "No"


def test_subagent_payload_records_nothing(tmp_path, monkeypatch):
    sdir = _session(tmp_path, monkeypatch)
    assert _handler({"payload": _payload({QUESTION: "Yes"}, agent_id="sub-1")}) == {}
    assert not (sdir / baton_pm_turns.PM_TURNS_FILENAME).exists()


def test_no_answer_records_nothing(tmp_path, monkeypatch):
    sdir = _session(tmp_path, monkeypatch)
    for response in ({"answers": {}}, {"answers": {QUESTION: ""}}, {}, None, "declined"):
        payload = _payload({})
        payload["tool_response"] = response
        assert _handler({"payload": payload}) == {}
    assert not (sdir / baton_pm_turns.PM_TURNS_FILENAME).exists()


def test_missing_session_id_records_nothing_and_never_raises(tmp_path, monkeypatch):
    sdir = _session(tmp_path, monkeypatch)
    payload = _payload({QUESTION: "Yes"})
    del payload["session_id"]
    assert _handler({"payload": payload}) == {}
    assert _handler({"payload": "garbage"}) == {}
    assert not (sdir / baton_pm_turns.PM_TURNS_FILENAME).exists()
