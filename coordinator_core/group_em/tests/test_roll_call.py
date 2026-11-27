import json
import threading
import time

import pytest

from coordinator_core.group_em import remote_roster, roll_call
from coordinator_core.session import peer_roster


def _row(sid, name="em", kind="claude", is_self=False):
    return peer_roster.PeerRow(
        session_id=sid, address=None, name=name, ref=None, cwd="/x", status="idle",
        running_seconds=1.0, is_self=is_self, self_determination="resolved",
        messaging_available=True, kind=kind, pid=1,
    )


@pytest.fixture
def roster(monkeypatch):
    rows = []
    monkeypatch.setattr(peer_roster, "build_roster", lambda *a, **k: list(rows))
    return rows


def _snapshot(tmp_path, box_id, sessions, ok=True):
    remote_roster._write_snapshot(tmp_path, box_id, ok, sessions, time.time())


def _sess(sid, name):
    return {"session_id": sid, "name": name, "cwd": None, "status": "idle", "address": None}


def test_codex_prefix_matches_watch():
    from coordinator_core.group_em import watch

    assert roll_call.CODEX_MIRROR_PREFIX == watch._TRANSIENT_CODEX_PREFIX


def test_resolve_excludes_mirrors_transport_self_and_workers(roster, tmp_path):
    roster.extend([
        _row("aaaa-1", "alpha"),
        _row("peer-codex-123", "codex"),
        _row("peer-ssh-box-456", "ssh-box"),
        _row("me-1", "me", is_self=True),
        _row("caller-1", "caller"),
        _row("codex:run1", "codex-exec", kind="codex"),
        _row("nameless-1", None),
    ])
    _snapshot(tmp_path, "boxB", [_sess("bbbb-2", "beta"), _sess("peer-codex-9", "c"), _sess("peer-t-8", "t")])
    out = roll_call.resolve_addressees(
        "/x", "caller-1", snapshot_dir=str(tmp_path), local_id="boxA"
    )
    assert out["local"] == [
        {"session_id": "aaaa-1", "name": "alpha", "kind": "claude"},
        {"session_id": "codex:run1", "name": "codex-exec", "kind": "codex"},
    ]
    assert out["far"] == [{"key": "boxB:bbbb-2", "box_id": "boxB", "name": "beta"}]
    reasons = {e["key"]: e["reason"] for e in out["excluded"]}
    assert reasons == {
        "peer-codex-123": "codex_mirror",
        "peer-ssh-box-456": "unimatrix_transport",
        "me-1": "self",
        "caller-1": "self",
        "nameless-1": "no_name",
        "boxB:peer-codex-9": "codex_mirror",
        "boxB:peer-t-8": "unimatrix_transport",
    }


def test_unreachable_box_sessions_are_addressees_flagged_unreachable(roster, tmp_path):
    _snapshot(tmp_path, "boxC", [_sess("cccc-3", "gamma")], ok=False)
    out = roll_call.resolve_addressees("/x", None, snapshot_dir=str(tmp_path), local_id="boxA")
    assert out["far"] == [{"key": "boxC:cccc-3", "box_id": "boxC", "name": "gamma", "unreachable": True}]
    assert out["excluded"] == []


def test_dead_pid_local_row_is_not_an_addressee(tmp_path, monkeypatch):
    from coordinator_core.session import harness_registry as hr

    def rec(name, pid):
        return hr.RegistryRecord(
            pid=pid, start_epoch=1000.0, cwd="/x", name=name,
            messaging_socket_path=None, status=None,
        )

    snap = {"live-sid": rec("live", 111), "dead-sid": rec("ghost", 222)}
    monkeypatch.setattr(hr, "snapshot", lambda: snap)
    monkeypatch.setattr(hr, "self_record", lambda: None)
    monkeypatch.setattr(peer_roster, "_process_alive", lambda pid, start_epoch: pid == 111)
    out = roll_call.resolve_addressees("/x", None, snapshot_dir=str(tmp_path), local_id="boxA")
    assert [r["session_id"] for r in out["local"]] == ["live-sid"]


def test_render_ask_carries_origin_box():
    assert "origin_box" not in roll_call.render_ask("rc-1")
    text = roll_call.render_ask("rc-1", origin_box="boxA")
    assert "origin_box" in text and "boxA" in text


def test_render_ask_names_every_required_part():
    text = roll_call.render_ask("rc-1", note="hurry")
    low = text.lower()
    assert "rc-1" in text
    for word in ("prune", "tasks", "goals", "/loop", "tbd", "eta", "blocked_on", "activity",
                 "priority_goal_id", *roll_call.GOAL_FIELDS):
        assert word in low
    assert "hurry" in text
    assert "hurry" not in roll_call.render_ask("rc-1")


def test_record_path_under_home_and_unsafe_id(tmp_path):
    assert roll_call.record_path("rc-1", tmp_path) == tmp_path / "roll-call" / "rc-1.json"
    for bad in ("../x", "a/b", "", "a b"):
        with pytest.raises(ValueError):
            roll_call.record_path(bad, tmp_path)


def test_record_path_defaults_to_resolved_home(monkeypatch, tmp_path):
    from coordinator_core.group_em import gem_home

    monkeypatch.setattr(gem_home, "resolve_home", lambda: tmp_path)
    assert roll_call.record_path("rc-1") == tmp_path / "roll-call" / "rc-1.json"


def _open(tmp_path, now=1000.0, deadline_minutes=None):
    addressees = {
        "local": [{"session_id": "aaaa-1", "name": "alpha"}],
        "far": [{"key": "boxB:bbbb-2", "box_id": "boxB", "name": "beta"}],
        "excluded": [],
    }
    return roll_call.open_roll_call("rc-1", addressees, "ask", deadline_minutes, now=now, home=tmp_path)


def _goal(**over):
    goal = {
        "goal_id": "g1", "target": "p95 < 200ms", "measured_now": "310ms", "gap": "110ms",
        "next_lever": "cache roster", "expected_gain": "-90ms", "owner": "alpha",
        "eta": "2026-12-01",
    }
    goal.update(over)
    return goal


_REPLY = [_goal()]


def _upsert(*args, **kwargs):
    kwargs.setdefault("priority_goal_id", "g1")
    return roll_call.upsert_reply(*args, **kwargs)


def test_open_writes_record_and_is_idempotent(tmp_path):
    rec = _open(tmp_path)
    assert rec["deadline"] == 1000.0 + 60 * 60
    assert roll_call.record_path("rc-1", tmp_path).is_file()
    again = roll_call.open_roll_call("rc-1", {"local": []}, "other", now=5000.0, home=tmp_path)
    assert again["text"] == "ask" and again["created"] == 1000.0


def test_open_once_reports_created_only_for_the_writer(tmp_path):
    first, created_first = roll_call.open_roll_call_once(
        "rc-1", {"local": []}, "ask", now=1000.0, home=tmp_path
    )
    second, created_second = roll_call.open_roll_call_once(
        "rc-1", {"local": []}, "other", now=5000.0, home=tmp_path
    )
    assert created_first is True and created_second is False
    assert second["text"] == first["text"] == "ask"


def test_deadline_minutes_is_honoured(tmp_path):
    assert _open(tmp_path, deadline_minutes=5)["deadline"] == 1000.0 + 300


def test_identical_reply_is_a_noop(tmp_path):
    _open(tmp_path)
    first = _upsert("rc-1", "aaaa-1", _REPLY, now=1010.0, home=tmp_path)
    second = _upsert("rc-1", "aaaa-1", _REPLY, now=1020.0, home=tmp_path)
    assert first["result"] == "stored" and second["result"] == "unchanged"
    replies = roll_call.load_record("rc-1", tmp_path)["replies"]
    assert list(replies) == ["aaaa-1"]
    assert replies["aaaa-1"]["received_at"] == 1010.0


def test_reply_stamps_origin_box_and_forwarded_by_only_when_given(tmp_path):
    _open(tmp_path)
    _upsert("rc-1", "boxB:bbbb-2", _REPLY, home=tmp_path, origin_box="boxA", forwarded_by="boxB")
    _upsert("rc-1", "aaaa-1", _REPLY, home=tmp_path)
    replies = roll_call.load_record("rc-1", tmp_path)["replies"]
    assert replies["boxB:bbbb-2"]["origin_box"] == "boxA"
    assert replies["boxB:bbbb-2"]["forwarded_by"] == "boxB"
    assert "origin_box" not in replies["aaaa-1"] and "forwarded_by" not in replies["aaaa-1"]


def test_changed_reply_replaces(tmp_path):
    _open(tmp_path)
    _upsert("rc-1", "aaaa-1", _REPLY, now=1010.0, home=tmp_path)
    out = _upsert(
        "rc-1", "aaaa-1", [_goal(measured_now="250ms")], now=1020.0, home=tmp_path
    )
    assert out["result"] == "stored"
    got = roll_call.load_record("rc-1", tmp_path)["replies"]["aaaa-1"]
    assert got["goals"][0]["measured_now"] == "250ms" and got["received_at"] == 1020.0


def test_late_reply_is_flagged_and_on_time_is_not(tmp_path):
    _open(tmp_path)
    on_time = _upsert("rc-1", "aaaa-1", _REPLY, now=1000.0 + 3600, home=tmp_path)
    late = _upsert("rc-1", "boxB:bbbb-2", _REPLY, now=1000.0 + 3601, home=tmp_path)
    assert on_time["late"] is False and late["late"] is True
    replies = roll_call.load_record("rc-1", tmp_path)["replies"]
    assert replies["aaaa-1"]["late"] is False and replies["boxB:bbbb-2"]["late"] is True


def test_unknown_roll_call_is_refused_not_created(tmp_path):
    assert _upsert("rc-9", "s", _REPLY, home=tmp_path) == {
        "ok": False, "reason": "unknown_roll_call"
    }
    assert not roll_call.record_path("rc-9", tmp_path).exists()
    assert roll_call.status("rc-9", tmp_path)["ok"] is False


def test_concurrent_replies_all_survive(tmp_path):
    _open(tmp_path)
    keys = [f"s{i}" for i in range(8)]
    errors = []

    def go(k):
        try:
            _upsert("rc-1", k, [_goal(goal_id=k)], priority_goal_id=k, now=1001.0, home=tmp_path)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=go, args=(k,)) for k in keys]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert sorted(roll_call.load_record("rc-1", tmp_path)["replies"]) == sorted(keys)


def test_status_lists_missing_and_replied(tmp_path):
    _open(tmp_path)
    before = roll_call.status("rc-1", tmp_path)
    assert {m["key"] for m in before["missing"]} == {"aaaa-1", "boxB:bbbb-2"}
    _upsert("rc-1", "aaaa-1", _REPLY, now=1010.0, home=tmp_path)
    after = roll_call.status("rc-1", tmp_path)
    assert after["missing"] == [{"key": "boxB:bbbb-2", "name": "beta"}]
    assert after["replied"] == ["aaaa-1"]
    assert after["deliveries"] == {}


def test_reply_by_name_counts_as_replied(tmp_path):
    _open(tmp_path)
    _upsert("rc-1", "alpha", _REPLY, now=1010.0, home=tmp_path)
    assert [m["key"] for m in roll_call.status("rc-1", tmp_path)["missing"]] == ["boxB:bbbb-2"]


def test_record_deliveries_merges_and_survives_replies(tmp_path):
    _open(tmp_path)
    _upsert("rc-1", "aaaa-1", _REPLY, now=1010.0, home=tmp_path)
    roll_call.record_deliveries(
        "rc-1", {"boxB:bbbb-2": {"ok": False, "reason": "unknown_box"}}, now=1011.0, home=tmp_path
    )
    st = roll_call.status("rc-1", tmp_path)
    assert st["deliveries"]["boxB:bbbb-2"] == {"ok": False, "reason": "unknown_box", "at": 1011.0}
    assert st["replied"] == ["aaaa-1"]


def _refusals(goals):
    return {(r["goal_id"], r["field"], r["reason"]) for r in roll_call.validate_reply(goals)}


def test_validate_accepts_a_complete_goal_and_blocked_on():
    assert roll_call.validate_reply([_goal()]) == []
    blocked = {"what": "review", "owner": "bob", "date": "2026-12-02"}
    assert roll_call.validate_reply([_goal(blocked_on=blocked)]) == []


@pytest.mark.parametrize("field", roll_call.GOAL_FIELDS)
def test_validate_refuses_each_missing_or_empty_field(field):
    for bad in (None, "", "   "):
        got = _refusals([_goal(**{field: bad})])
        assert got == {(("g1" if field != "goal_id" else None), field, "missing")}
    absent = _goal()
    del absent[field]
    assert [r["field"] for r in roll_call.validate_reply([absent])] == [field]


def test_validate_refuses_tbd_in_any_field_case_insensitively():
    for field in roll_call.GOAL_FIELDS[1:7]:
        assert _refusals([_goal(**{field: "tBd"})]) == {("g1", field, "tbd")}
    assert _refusals([_goal(eta="TBD")]) == {("g1", "eta", "tbd")}
    assert _refusals([_goal(gap="TBD next week")]) == {("g1", "gap", "tbd")}
    assert roll_call.validate_reply([_goal(gap="a tbdx widget")]) == []


def test_validate_eta_must_be_an_iso_date():
    for bad in ("next week", "2026-13-01", "2026-12-1", "20261201", "2026-12-01T10:00"):
        assert _refusals([_goal(eta=bad)]) == {("g1", "eta", "not_iso_date")}, bad
    assert _refusals([_goal(eta=None)]) == {("g1", "eta", "missing")}


def test_validate_blocked_on_needs_owner_and_iso_date():
    got = _refusals([_goal(blocked_on={"what": "review"})])
    assert got == {
        ("g1", "blocked_on.owner", "missing"),
        ("g1", "blocked_on.date", "missing"),
    }
    got = _refusals([_goal(blocked_on={"what": "x", "owner": "TBD", "date": "soon"})])
    assert got == {("g1", "blocked_on.owner", "tbd"), ("g1", "blocked_on.date", "not_iso_date")}
    assert _refusals([_goal(blocked_on="review")]) == {("g1", "blocked_on", "not_an_object")}


def test_validate_names_every_problem_across_goals():
    got = _refusals([_goal(), _goal(goal_id="g2", owner="", eta="TBD"), _goal(goal_id="g1")])
    assert got == {
        ("g2", "owner", "missing"),
        ("g2", "eta", "tbd"),
        ("g1", "goal_id", "duplicate_goal_id"),
    }


def test_validate_refuses_no_goals_and_non_objects():
    for bad in (None, [], "goals", {"goal_id": "g1"}):
        assert _refusals(bad) == {(None, "goals", "missing")}
    assert _refusals(["x"]) == {(None, "goals", "not_an_object")}


def test_refused_reply_stores_and_logs_nothing(tmp_path):
    _open(tmp_path)
    path = roll_call.record_path("rc-1", tmp_path)
    before = path.read_bytes()
    with pytest.raises(roll_call.RollCallReplyRefused) as err:
        _upsert(
            "rc-1", "aaaa-1", [_goal(eta="TBD")], activity={"done": "x"}, home=tmp_path
        )
    assert err.value.as_dict() == {
        "roll_call_id": "rc-1",
        "session_key": "aaaa-1",
        "refusals": [{"goal_id": "g1", "field": "eta", "reason": "tbd"}],
    }
    assert path.read_bytes() == before
    assert not roll_call.log_path("rc-1", tmp_path).exists()


def test_activity_lands_in_log_only(tmp_path):
    _open(tmp_path)
    act = {"done": "shipped X", "next": "ship Y"}
    _upsert("rc-1", "aaaa-1", _REPLY, activity=act, now=1010.0, home=tmp_path)
    lines = roll_call.log_path("rc-1", tmp_path).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["session_key"] == "aaaa-1" and entry["activity"] == act
    assert "shipped X" not in roll_call.record_path("rc-1", tmp_path).read_text(encoding="utf-8")
    assert "shipped X" not in json.dumps(roll_call.status("rc-1", tmp_path))
    _upsert("rc-1", "aaaa-1", _REPLY, activity=act, now=1020.0, home=tmp_path)
    assert len(roll_call.log_path("rc-1", tmp_path).read_text(encoding="utf-8").splitlines()) == 1


def test_status_brief_is_one_row_per_goal_without_activity(tmp_path):
    _open(tmp_path)
    blocked = {"what": "review", "owner": "bob", "date": "2026-12-02"}
    _upsert(
        "rc-1", "aaaa-1", [_goal(), _goal(goal_id="g2", blocked_on=blocked)],
        activity={"done": "d"}, now=1010.0, home=tmp_path,
    )
    st = roll_call.status("rc-1", tmp_path)
    assert [g["goal_id"] for g in st["goals"]] == ["g1", "g2"]
    row = st["goals"][0]
    assert {"goal_id", "owner", "target", "measured_now", "gap", "next_lever", "expected_gain",
            "eta", "blocked_on"} <= set(row)
    assert row["blocked_on"] is None and st["goals"][1]["blocked_on"] == blocked
    assert "activity" not in json.dumps(st)


def test_priority_goal_is_required_and_must_name_a_reply_goal(tmp_path):
    _open(tmp_path)
    path = roll_call.record_path("rc-1", tmp_path)
    before = path.read_bytes()
    for bad, reason in ((None, "missing"), ("", "missing"), ("  ", "missing"), ("g9", "not_a_reply_goal")):
        with pytest.raises(roll_call.RollCallReplyRefused) as err:
            roll_call.upsert_reply(
                "rc-1", "aaaa-1", _REPLY, priority_goal_id=bad, home=tmp_path
            )
        assert err.value.refusals == [
            {"goal_id": None, "field": "priority_goal_id", "reason": reason}
        ]
    with pytest.raises(roll_call.RollCallReplyRefused):
        roll_call.upsert_reply("rc-1", "aaaa-1", _REPLY, home=tmp_path)
    assert path.read_bytes() == before


def test_priority_refusal_joins_goal_refusals(tmp_path):
    _open(tmp_path)
    with pytest.raises(roll_call.RollCallReplyRefused) as err:
        roll_call.upsert_reply(
            "rc-1", "aaaa-1", [_goal(eta="TBD")], priority_goal_id="g9", home=tmp_path
        )
    assert {(r["field"], r["reason"]) for r in err.value.refusals} == {
        ("eta", "tbd"), ("priority_goal_id", "not_a_reply_goal"),
    }


def test_status_marks_the_priority_goal_and_a_changed_priority_replaces(tmp_path):
    _open(tmp_path)
    goals = [_goal(), _goal(goal_id="g2")]
    assert _upsert("rc-1", "aaaa-1", goals, priority_goal_id="g2", now=1010.0, home=tmp_path)[
        "result"
    ] == "stored"
    rows = {g["goal_id"]: g["priority"] for g in roll_call.status("rc-1", tmp_path)["goals"]}
    assert rows == {"g1": False, "g2": True}
    same = _upsert("rc-1", "aaaa-1", goals, priority_goal_id="g2", now=1020.0, home=tmp_path)
    assert same["result"] == "unchanged"
    moved = _upsert("rc-1", "aaaa-1", goals, priority_goal_id="g1", now=1030.0, home=tmp_path)
    assert moved["result"] == "stored"
    rows = {g["goal_id"]: g["priority"] for g in roll_call.status("rc-1", tmp_path)["goals"]}
    assert rows == {"g1": True, "g2": False}


def test_status_never_writes(tmp_path):
    _open(tmp_path)
    path = roll_call.record_path("rc-1", tmp_path)
    before = path.read_bytes()
    roll_call.status("rc-1", tmp_path)
    assert path.read_bytes() == before
