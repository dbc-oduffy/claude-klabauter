"""Pins fanout.census: per-worker states, tag-pair identity, check-in matching, manifest order."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from coordinator_core.ops.fanout import census, contract

FIX = Path(__file__).parent / "fixtures"
IDS = ["worker-a", "worker-b", "worker-c", "worker-d", "worker-e", "worker-f"]


def _manifest() -> dict:
    m = contract.parse_manifest_yaml((FIX / "manifest-minimal.yaml").read_text(encoding="utf-8"))
    base = m["workers"][0]
    m["workers"] = []
    for wid in IDS:
        w = copy.deepcopy(base)
        w["id"] = wid
        w["focus"] = f"repos.{wid.replace('-', '_')}"
        m["workers"].append(w)
    return m


def _load(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


CHECKIN = "[session s-a] channel: https://example.invalid/comms/pull/9"


def _rows(comments=(CHECKIN,)):
    result = census.census(
        _manifest(), _load("list-sessions.json"), _load("get-session.json"), list(comments)
    )
    return result, {r["worker_id"]: r for r in result["rows"]}


def test_states_per_worker():
    _, rows = _rows()
    assert rows["worker-a"]["state"] == "running"
    assert rows["worker-b"]["state"] == "done"
    assert rows["worker-c"]["state"] == "failed"
    assert rows["worker-d"]["state"] == "duplicate"
    assert rows["worker-e"]["state"] == "unknown"
    assert rows["worker-f"]["state"] == "missing"


def test_duplicate_lists_both_sessions_and_missing_has_none():
    _, rows = _rows()
    assert rows["worker-d"]["session_ids"] == ["s-d1", "s-d2"]
    assert rows["worker-f"]["session_ids"] == []
    assert rows["worker-f"]["status_bucket"] is None
    assert rows["worker-f"]["cost_usd"] is None


def test_detail_fields_read():
    _, rows = _rows()
    assert rows["worker-a"]["status_detail"] == "editing census"
    assert rows["worker-a"]["cost_usd"] == 1.25
    assert rows["worker-b"]["cost_usd"] == 3.5
    assert rows["worker-c"]["cost_usd"] is None


def test_checkin_matched_by_session_id():
    _, rows = _rows()
    assert rows["worker-a"]["checked_in"] is True
    assert rows["worker-a"]["channel_url"] == "https://example.invalid/comms/pull/9"
    assert rows["worker-b"]["checked_in"] is False
    assert rows["worker-b"]["channel_url"] is None


def test_checkin_accepts_comment_objects_and_ignores_near_misses():
    _, rows = _rows([{"body": CHECKIN}, "[session s-b] channel : nope"])
    assert rows["worker-a"]["checked_in"] is True
    assert rows["worker-b"]["checked_in"] is False


def test_stray_and_other_job_ignored():
    result, rows = _rows()
    assert result["strays"] == ["s-stray"]
    assert "s-other" not in rows["worker-a"]["session_ids"]
    assert rows["worker-a"]["session_ids"] == ["s-a"]


def test_rows_in_manifest_order():
    result, _ = _rows()
    assert [r["worker_id"] for r in result["rows"]] == IDS
    assert result["job_id"] == "demo-job"


def test_absent_fields_never_raise():
    m = _manifest()
    sessions = [{"id": "x", "tags": ["fanout:demo-job", "fanout-worker:worker-a"]}, {"tags": None}]
    result = census.census(m, sessions, None, None)
    row = result["rows"][0]
    assert row["state"] == "unknown"
    assert row["status_bucket"] is None and row["status_detail"] is None and row["cost_usd"] is None
    assert census.census(m, None, None, None)["rows"][0]["state"] == "missing"
