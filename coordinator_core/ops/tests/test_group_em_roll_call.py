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
    from coordinator_core.group_em import box_registry

    monkeypatch.setattr(box_registry, "local_box_id", lambda home=None: "boxa")
    monkeypatch.setattr(box_registry, "read_boxes", lambda directory=None: [])
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


def test_local_only_addressees_still_launch_the_worker(home, monkeypatch):
    monkeypatch.setattr(
        roll_call, "resolve_addressees", lambda *a, **k: {"local": list(LOCAL), "far": [], "excluded": []}
    )
    out = ops._groupem_roll_call({})
    assert out["delivery"] == {"launched": True, "pid": 7} and len(home) == 1


def test_no_addressees_launches_nothing(home, monkeypatch):
    monkeypatch.setattr(
        roll_call, "resolve_addressees", lambda *a, **k: {"local": [], "far": [], "excluded": []}
    )
    out = ops._groupem_roll_call({})
    assert out["delivery"] == {"launched": False, "reason": "no_addressees"} and home == []


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


@pytest.fixture
def boxes(monkeypatch):
    from coordinator_core.group_em import box_registry, box_transport

    sent: list = []
    monkeypatch.setattr(box_registry, "local_box_id", lambda home=None: "boxa")
    monkeypatch.setattr(
        box_registry, "read_boxes", lambda directory=None: [{"box_id": "boxa"}, {"box_id": "boxz"}]
    )

    def invoke(box, op, params, runner=None, *, timeout):
        sent.append((box["box_id"], op, params))
        return {"ok": True, "reply": {"ok": True, "result": "stored", "late": False}}

    monkeypatch.setattr(box_transport, "invoke_remote", invoke)
    return sent


def test_reply_naming_another_box_is_forwarded_there_without_origin_box(home, boxes):
    out = _reply("rc-1", key="b1:s1", origin_box="boxz")
    assert out == {"ok": True, "result": "stored", "late": False}
    ((box_id, op, params),) = boxes
    assert (box_id, op) == ("boxz", "groupem.roll_call_reply")
    assert "origin_box" not in params and params["session_key"] == "b1:s1"
    assert params["forwarded_by"] == "boxa"


def test_reply_naming_this_box_or_no_box_upserts_locally(home, boxes):
    rc = ops._groupem_roll_call({"dry_run": True})["roll_call_id"]
    assert _reply(rc, origin_box="BOXA")["result"] == "stored"
    assert _reply(rc, key="other")["result"] == "stored"
    assert boxes == []


def test_reply_without_origin_box_and_without_a_local_record_is_forwarded_to_the_minting_box(home, boxes):
    out = _reply("rc-far", key="b1:s1")
    assert out == {"ok": True, "result": "stored", "late": False}
    ((box_id, op, params),) = boxes
    assert (box_id, op) == ("boxz", "groupem.roll_call_reply")
    assert params["forwarded_by"] == "boxa" and params["session_key"] == "b1:s1"


def test_reply_without_origin_box_stays_unknown_when_no_box_has_the_record(home, boxes, monkeypatch):
    from coordinator_core.group_em import box_transport

    monkeypatch.setattr(
        box_transport,
        "invoke_remote",
        lambda *a, **k: {"ok": True, "reply": {"ok": False, "reason": "unknown_roll_call"}},
    )
    assert _reply("rc-none") == {"ok": False, "reason": "unknown_roll_call"}


def test_reply_with_a_local_record_never_probes_other_boxes(home, boxes):
    rc = ops._groupem_roll_call({"dry_run": True})["roll_call_id"]
    assert _reply(rc)["result"] == "stored"
    assert boxes == []


def test_a_forwarded_leg_never_probes_further(home, boxes):
    assert _reply("rc-none", key="b1:s1", forwarded_by="boxz") == {"ok": False, "reason": "unknown_roll_call"}
    assert boxes == []


def test_stored_reply_carries_the_minting_box_and_the_forwarding_box(home, boxes):
    rc = ops._groupem_roll_call({"dry_run": True})["roll_call_id"]
    assert _reply(rc, key="b1:s1", forwarded_by="boxz")["result"] == "stored"
    assert _reply(rc)["result"] == "stored"
    replies = roll_call.load_record(rc)["replies"]
    assert replies["b1:s1"]["origin_box"] == "boxa" and replies["b1:s1"]["forwarded_by"] == "boxz"
    assert replies["sid-1"]["origin_box"] == "boxa" and "forwarded_by" not in replies["sid-1"]


def test_unknown_origin_box_is_refused_with_no_send(home, boxes):
    assert _reply("rc-1", origin_box="nowhere") == {"ok": False, "reason": "unknown_origin_box"}
    assert boxes == []


def test_forward_failure_is_a_reason_not_a_raise(home, boxes, monkeypatch):
    from coordinator_core.group_em import box_transport

    monkeypatch.setattr(
        box_transport, "invoke_remote", lambda *a, **k: {"ok": False, "state": "unknown", "reason": "ssh_timeout"}
    )
    out = _reply("rc-1", origin_box="boxz")
    assert out["ok"] is True and out["stored_locally"] is True
    assert out["forward_reason"] == "forward_failed:ssh_timeout"
    monkeypatch.setattr(
        box_transport, "invoke_remote", lambda *a, **k: {"ok": False, "state": "unknown", "reason": "not_ssh_transport"}
    )
    assert _reply("rc-2", origin_box="boxz")["forward_reason"] == "forward_unsupported_transport"


def test_forward_with_no_invoke_path_stores_the_reply_locally(home, boxes, monkeypatch):
    from coordinator_core.group_em import box_transport

    monkeypatch.setattr(
        box_transport, "invoke_remote", lambda *a, **k: {"ok": False, "state": "unknown", "reason": "no_invoke_path"}
    )
    out = _reply("rc-far", key="b1:s1", origin_box="boxz")
    assert out["result"] == "stored" and out["forward_reason"] == "forward_failed:no_invoke_path"
    assert roll_call.load_record("rc-far")["replies"]["b1:s1"]["goals"][0]["goal_id"] == "G1"


def test_session_key_defaults_to_the_callers_own_session(home, boxes, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "my-sid")
    rc = ops._groupem_roll_call({"dry_run": True})["roll_call_id"]
    out = ops._groupem_roll_call_reply({"roll_call_id": rc, "eta": "2026-12-01"})
    assert out["result"] == "stored"
    assert ops._groupem_roll_call_status({"roll_call_id": rc})["replied"] == ["my-sid"]
    monkeypatch.delenv("COORDINATOR_SESSION_ID")
    for var in ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(CallerFacingValidationError):
        ops._groupem_roll_call_reply({"roll_call_id": rc, "eta": "2026-12-01"})


def test_defaulted_key_for_an_unknown_roll_call_travels_box_qualified(home, boxes, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "my-sid")
    out = ops._groupem_roll_call_reply({"roll_call_id": "rc-far", "eta": "2026-12-01"})
    assert out == {"ok": True, "result": "stored", "late": False}
    ((box_id, op, params),) = boxes
    assert (box_id, op) == ("boxz", "groupem.roll_call_reply")
    assert params["session_key"] == "boxa:my-sid" and params["forwarded_by"] == "boxa"


def test_minimal_reply_without_goals_is_stored_and_shown(home, boxes):
    rc = ops._groupem_roll_call({"dry_run": True})["roll_call_id"]
    jobs = [{"id": "j1", "pid": 42, "cores": 2, "hard_stop": "2026-11-28T00:00:00Z"}]
    out = ops._groupem_roll_call_reply(
        {"roll_call_id": rc, "session_key": "sid-1", "jobs": jobs, "queued": ["plan-x"], "eta": "2026-11-28"}
    )
    assert out["result"] == "stored"
    status = ops._groupem_roll_call_status({"roll_call_id": rc})
    assert status["replied"] == ["sid-1"] and status["goals"] == []
    assert status["reports"] == [
        {"session_key": "sid-1", "late": False, "jobs": jobs, "queued": ["plan-x"], "eta": "2026-11-28"}
    ]
    again = ops._groupem_roll_call_reply(
        {"roll_call_id": rc, "session_key": "sid-1", "jobs": jobs, "queued": ["plan-x"], "eta": "2026-11-28"}
    )
    assert again["result"] == "unchanged"


def test_empty_reply_and_malformed_extras_are_refused(home, boxes):
    rc = ops._groupem_roll_call({"dry_run": True})["roll_call_id"]
    out = ops._groupem_roll_call_reply({"roll_call_id": rc, "session_key": "sid-1"})
    assert out["error"] == "RollCallReplyRefused"
    assert {(r["field"], r["reason"]) for r in out["refusals"]} == {("reply", "missing")}
    bad = ops._groupem_roll_call_reply({"roll_call_id": rc, "session_key": "sid-1", "jobs": "x"})
    assert {(r["field"], r["reason"]) for r in bad["refusals"]} == {("jobs", "not_a_list")}
    assert ops._groupem_roll_call_status({"roll_call_id": rc})["replied"] == []


def test_goals_still_refuse_when_supplied_incomplete(home, boxes):
    rc = ops._groupem_roll_call({"dry_run": True})["roll_call_id"]
    out = _reply(rc, goals=[_goal(gap="")], eta="2026-12-01")
    assert out["error"] == "RollCallReplyRefused"


def test_forwarded_reply_lands_only_for_a_far_addressee(home, boxes):
    rc = ops._groupem_roll_call({"dry_run": True})["roll_call_id"]
    refused = _reply(rc, key="sid-1", forwarded_by="boxz")
    assert refused == {"ok": False, "reason": "not_an_addressee"}
    assert ops._groupem_roll_call_status({"roll_call_id": rc})["replied"] == []
    assert _reply(rc, key="b1:s1", forwarded_by="boxz")["result"] == "stored"
    assert ops._groupem_roll_call_status({"roll_call_id": rc})["replied"] == ["b1:s1"]
    assert boxes == []


def test_forwarded_reply_for_an_unknown_roll_call_is_unknown_roll_call(home, boxes):
    assert _reply("rc-none", key="b1:s1", forwarded_by="boxz") == {"ok": False, "reason": "unknown_roll_call"}


def test_unknown_roll_call_is_a_reply(home):
    assert _reply("rc-none") == {"ok": False, "reason": "unknown_roll_call"}
    assert ops._groupem_roll_call_status({"roll_call_id": "rc-none"}) == {
        "ok": False,
        "reason": "unknown_roll_call",
    }
