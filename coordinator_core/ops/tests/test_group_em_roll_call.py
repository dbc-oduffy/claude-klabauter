"""Veneer tests for groupem.roll_call / roll_call_reply / roll_call_status."""

from __future__ import annotations

import pytest

from coordinator_core import ipc
from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.group_em import gem_home, roll_call, roll_call_deliver
from coordinator_core.ipc import CallerFacingValidationError
from coordinator_core.op_scopes import OP_KEY_SCOPE
from coordinator_core.ops import _registry_map
from coordinator_core.ops import group_em_roll_call as ops

OPS = ("groupem.roll_call", "groupem.roll_call_reply", "groupem.roll_call_status")
FAR = [{"key": "b1:s1", "box_id": "b1", "name": "far-em"}]
LOCAL = [{"session_id": "sid-1", "name": "local-em"}]


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setattr(gem_home, "resolve_home", lambda: tmp_path)
    monkeypatch.setattr(
        roll_call,
        "resolve_addressees",
        lambda *a, **k: {"local": list(LOCAL), "far": list(FAR), "excluded": [{"key": "peer-codex-1", "reason": "codex_mirror"}]},
    )
    launches = []
    monkeypatch.setattr(
        roll_call_deliver, "launch", lambda path, **k: launches.append(path) or {"launched": True, "pid": 7}
    )
    return launches


def test_registration_surfaces_agree():
    for op in OPS:
        assert _registry_map.OP_MODULE_MAP[op] == "coordinator_core.ops.group_em_roll_call"
        assert OP_KEY_SCOPE[op] == "none"
        assert ipc._REGISTRY.get(op) is not None
    assert OP_CLASSIFICATION["groupem.roll_call"] is OpClass.MUTATING
    assert OP_CLASSIFICATION["groupem.roll_call_reply"] is OpClass.MUTATING
    assert OP_CLASSIFICATION["groupem.roll_call_status"] is OpClass.COMPUTE_ONLY


def test_dry_run_writes_record_and_launches_nothing(home, tmp_path):
    out = ops._groupem_roll_call({"dry_run": True})
    assert out["local"] == LOCAL and out["far"] == FAR
    assert out["excluded"] == [{"key": "peer-codex-1", "reason": "codex_mirror"}]
    assert out["delivery"] == {"launched": False, "reason": "dry_run"}
    assert home == []
    assert (tmp_path / "roll-call" / (out["roll_call_id"] + ".json")).is_file()
    assert out["record_path"].endswith(out["roll_call_id"] + ".json")


def test_live_call_launches_one_worker_on_the_record(home, tmp_path):
    out = ops._groupem_roll_call({"note": "hi", "deadline_minutes": 5})
    assert out["delivery"] == {"launched": True, "pid": 7}
    assert home == [tmp_path / "roll-call" / (out["roll_call_id"] + ".json")]
    assert "hi" in out["text"]


def test_no_far_addressees_launches_nothing(home, monkeypatch):
    monkeypatch.setattr(
        roll_call, "resolve_addressees", lambda *a, **k: {"local": list(LOCAL), "far": [], "excluded": []}
    )
    out = ops._groupem_roll_call({})
    assert out["delivery"]["launched"] is False and home == []


def test_existing_id_is_not_relaunched(home):
    first = ops._groupem_roll_call({"roll_call_id": "rc-a"})
    again = ops._groupem_roll_call({"roll_call_id": "rc-a"})
    assert len(home) == 1
    assert again["delivery"]["reason"] == "existing_roll_call"
    assert again["far"] == first["far"]


def test_bad_params_are_caller_errors(home):
    for bad in ({"roll_call_id": "../x"}, {"deadline_minutes": 0}, {"deadline_minutes": True}, {"note": ""}):
        with pytest.raises(CallerFacingValidationError):
            ops._groupem_roll_call(bad)
    with pytest.raises(CallerFacingValidationError):
        ops._groupem_roll_call_reply({"session_key": "k"})
    with pytest.raises(CallerFacingValidationError):
        ops._groupem_roll_call_status({})


def _goal(**over):
    goal = {
        "goal_id": "G1", "target": "p95 < 500ms", "measured_now": "420ms", "gap": "none",
        "next_lever": "cache roster", "expected_gain": "-80ms", "owner": "claude-klabauter-0e", "eta": "2026-12-01",
    }
    goal.update(over)
    return goal


def _reply(rc, key="sid-1", goals=None, **kw):
    return ops._groupem_roll_call_reply(
        {
            "roll_call_id": rc, "session_key": key, "goals": [_goal()] if goals is None else goals,
            "priority_goal_id": "G1", **kw,
        }
    )


def test_reply_twice_leaves_one_reply_and_status_lists_missing(home):
    rc = ops._groupem_roll_call({"dry_run": True})["roll_call_id"]
    assert _reply(rc)["result"] == "stored"
    assert _reply(rc)["result"] == "unchanged"
    status = ops._groupem_roll_call_status({"roll_call_id": rc})
    assert status["replied"] == ["sid-1"]
    assert [g["goal_id"] for g in status["goals"]] == ["G1"]
    assert [m["key"] for m in status["missing"]] == ["b1:s1"]


def test_refused_reply_returns_typed_envelope_and_stores_nothing(home, tmp_path):
    rc = ops._groupem_roll_call({"dry_run": True})["roll_call_id"]
    out = _reply(
        rc,
        goals=[
            _goal(eta="TBD"),
            _goal(goal_id="G2", gap=""),
            _goal(goal_id="G3", blocked_on={"what": "review", "owner": "doe", "date": "soon"}),
        ],
        activity="done: x",
    )
    assert out["ok"] is False and out["error"] == "RollCallReplyRefused"
    assert out["roll_call_id"] == rc and out["session_key"] == "sid-1"
    pairs = {(r["goal_id"], r["field"]) for r in out["refusals"]}
    assert {("G1", "eta"), ("G2", "gap"), ("G3", "blocked_on.date")} <= pairs
    status = ops._groupem_roll_call_status({"roll_call_id": rc})
    assert status["replied"] == [] and status["goals"] == []
    assert not (tmp_path / "roll-call" / (rc + ".log.jsonl")).exists()


def test_activity_goes_to_the_log_never_the_brief(home, tmp_path):
    rc = ops._groupem_roll_call({"dry_run": True})["roll_call_id"]
    assert _reply(rc, activity="did the thing")["result"] == "stored"
    log = (tmp_path / "roll-call" / (rc + ".log.jsonl")).read_text(encoding="utf-8")
    assert "did the thing" in log
    assert "did the thing" not in str(ops._groupem_roll_call_status({"roll_call_id": rc}))
    assert "did the thing" not in (tmp_path / "roll-call" / (rc + ".json")).read_text(encoding="utf-8")


def test_priority_goal_is_required_and_marked(home):
    rc = ops._groupem_roll_call({"dry_run": True})["roll_call_id"]
    for bad in (None, "G9"):
        out = _reply(rc, priority_goal_id=bad)
        assert out["ok"] is False and out["error"] == "RollCallReplyRefused"
        assert {(r["goal_id"], r["field"]) for r in out["refusals"]} == {(None, "priority_goal_id")}
    assert _reply(rc)["result"] == "stored"
    goals = ops._groupem_roll_call_status({"roll_call_id": rc})["goals"]
    assert [g["priority"] for g in goals] == [True]


def test_late_reply_is_stamped(home, monkeypatch):
    rc = ops._groupem_roll_call({"dry_run": True, "deadline_minutes": 1})["roll_call_id"]
    monkeypatch.setattr(roll_call.time, "time", lambda: 1e12)
    assert _reply(rc)["late"] is True


def test_unknown_roll_call_is_a_reply(home):
    assert _reply("rc-none") == {"ok": False, "reason": "unknown_roll_call"}
    assert ops._groupem_roll_call_status({"roll_call_id": "rc-none"}) == {
        "ok": False,
        "reason": "unknown_roll_call",
    }
