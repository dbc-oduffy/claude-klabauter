"""Tests for fanout.reconcile — pure census-rows-to-actions."""

from __future__ import annotations

import copy

from coordinator_core.ops.fanout.reconcile import reconcile


def _manifest(max_concurrent=None):
    m = {"job_id": "job1", "channel": "pr"}
    if max_concurrent is not None:
        m["max_concurrent"] = max_concurrent
    return m


def _row(worker_id, state, sids=None, checked_in=False, url=None):
    return {
        "worker_id": worker_id,
        "state": state,
        "session_ids": sids if sids is not None else ([] if state == "missing" else [f"s-{worker_id}"]),
        "status_bucket": None,
        "status_detail": None,
        "cost_usd": None,
        "checked_in": checked_in,
        "channel_url": url,
    }


def _census(rows, strays=()):
    return {"job_id": "job1", "rows": rows, "strays": list(strays)}


def _kinds(result):
    return [(a["kind"], a.get("worker_id"), a["reason"]) for a in result["actions"]]


def test_idempotent():
    census = _census([_row("a", "missing"), _row("b", "done"), _row("c", "running")], ["x"])
    before = copy.deepcopy(census)
    assert reconcile(_manifest(), census) == reconcile(_manifest(), census)
    assert census == before


def test_missing_spawns_with_idempotency_key():
    result = reconcile(_manifest(), _census([_row("a", "missing")]))
    assert result["actions"] == [
        {"kind": "spawn", "worker_id": "a", "idempotency_key": "job1/a", "reason": "missing"}
    ]


def test_cap_counts_running_duplicate_unknown_sessions():
    rows = [
        _row("r", "running"),
        _row("d", "duplicate", ["d1", "d2"]),
        _row("u", "unknown"),
        _row("m1", "missing"),
        _row("m2", "missing"),
    ]
    result = reconcile(_manifest(max_concurrent=5), _census(rows))
    spawns = [a for a in result["actions"] if a["kind"] == "spawn"]
    assert [a["worker_id"] for a in spawns] == ["m1"]
    assert ("flag", "m2", "concurrency-cap") in _kinds(result)


def test_cap_honoured_default_and_overflow_flagged():
    rows = [_row(f"w{i}", "missing") for i in range(8)]
    result = reconcile(_manifest(), _census(rows))
    assert sum(a["kind"] == "spawn" for a in result["actions"]) == 6
    caps = [a for a in result["actions"] if a["reason"] == "concurrency-cap"]
    assert [a["worker_id"] for a in caps] == ["w6", "w7"]


def test_done_and_failed_do_not_hold_slots():
    rows = [_row("a", "done"), _row("b", "failed"), _row("c", "missing")]
    result = reconcile(_manifest(max_concurrent=1), _census(rows))
    assert _kinds(result) == [
        ("archive", "a", "done"),
        ("flag", "b", "failed"),
        ("spawn", "c", "missing"),
    ]


def test_all_done_yields_only_archives():
    rows = [_row("a", "done"), _row("b", "done")]
    result = reconcile(_manifest(), _census(rows))
    assert [a["kind"] for a in result["actions"]] == ["archive", "archive"]
    assert result["actions"][0]["session_id"] == "s-a"


def test_flags_for_duplicate_unknown_and_strays():
    rows = [_row("a", "duplicate", ["x1", "x2"]), _row("b", "unknown")]
    result = reconcile(_manifest(), _census(rows, ["stray1"]))
    assert _kinds(result) == [
        ("flag", "a", "duplicate"),
        ("flag", "b", "unknown"),
        ("flag", None, "stray"),
    ]
    assert result["actions"][-1]["session_id"] == "stray1"


def test_running_awaits_checkin_only_when_not_checked_in():
    rows = [
        _row("a", "running"),
        _row("b", "running", checked_in=True, url="https://x/pr/2"),
    ]
    result = reconcile(_manifest(), _census(rows))
    assert _kinds(result) == [("await_checkin", "a", "running-not-checked-in")]


def test_pause_broadcasts_only_to_checked_in_children():
    rows = [
        _row("a", "running"),
        _row("b", "running", checked_in=True, url="https://x/pr/2"),
        _row("c", "running", checked_in=True, url=None),
    ]
    result = reconcile(_manifest(), _census(rows), pause={"message": "stop"})
    broadcasts = [a for a in result["actions"] if a["kind"] == "broadcast"]
    assert [a["worker_id"] for a in broadcasts] == ["b"]
    assert ("await_checkin", "a", "running-not-checked-in") in _kinds(result)


def test_no_pause_no_broadcast():
    rows = [_row("b", "running", checked_in=True, url="https://x/pr/2")]
    assert reconcile(_manifest(), _census(rows))["actions"] == []
