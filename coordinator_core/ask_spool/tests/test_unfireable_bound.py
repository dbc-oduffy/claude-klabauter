"""An entry the engine refuses for good is withdrawn with a reason and its submitter or owner told, never retried forever."""

from __future__ import annotations

import json
import os
import time

import pytest

from coordinator_core.ask_spool import drain, record

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_DRIVEN = "Run ask-20261126T000000-aaaaaa is driven by session sess-other. Pass `args.takeover` to take it over."
_DAY = 24 * 3600.0


@pytest.fixture(autouse=True)
def _isolated_settings_home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))


class _Refusing:
    """Stands in for submit_engine_ask.evaluate refusing every mint with `reason`."""

    def __init__(self, reason: str):
        self.reason, self.calls = reason, 0

    def __call__(self, payload: dict) -> dict:
        self.calls += 1
        return {"hookSpecificOutput": {"permissionDecision": "deny", "permissionDecisionReason": self.reason}}


def _spool(root, session="sess1", args=None, spooled_ago=0.0, attempts=0):
    payload = json.dumps(
        {"tool_name": "Workflow", "session_id": session, "tool_use_id": "tu1", "tool_input": {"name": "ask", "args": args or {}}}
    ).encode()
    sid, created = record.spool_ask(
        str(root), payload, session_id=session, tool_use_id="tu1", reason="engine_unreachable",
        spooled_at=time.time() - spooled_ago,
    )
    assert created
    path = record.entry_path(str(root), sid)
    header, body = record.read_entry(path)
    drain._rewrite(path, {**header, "attempts": attempts}, body)
    return sid


def _notices(root, session):
    path = root / ".coordinator-local" / "ask-spool" / "notices" / f"{session}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _state(root, sid, suffix):
    return os.path.exists(record.entry_path(str(root), sid, suffix))


def test_driven_by_session_refusal_is_terminal_on_first_sight_and_notifies_the_submitter(tmp_path):
    sid = _spool(tmp_path)
    counts = drain.run(str(tmp_path), evaluate=_Refusing(_DRIVEN))
    assert counts["withdrawn"] == 1 and counts["failed"] == 0 and counts["notified"] == 1
    assert _state(tmp_path, sid, record.WITHDRAWN_SUFFIX) and not _state(tmp_path, sid, record.PENDING_SUFFIX)
    header, _ = record.read_entry(record.entry_path(str(tmp_path), sid, record.WITHDRAWN_SUFFIX))
    assert header["withdraw_reason"].startswith("unfireable: ") and "driven by session" in header["withdraw_reason"]
    assert header["withdrawn_by"] == "drain"
    (note,) = _notices(tmp_path, "sess1")
    assert note["status"] == "withdrawn" and note["spool_id"] == sid and note["reason"].startswith("unfireable: ")


def test_an_old_entry_past_the_attempt_cap_is_withdrawn_on_its_first_claim(tmp_path):
    sid = _spool(tmp_path, spooled_ago=2 * _DAY, attempts=335)
    counts = drain.run(str(tmp_path), evaluate=_Refusing("LockTimeout: engine busy"))
    assert counts["withdrawn"] == 1 and counts["failed"] == 0
    assert _state(tmp_path, sid, record.WITHDRAWN_SUFFIX)
    assert drain.run(str(tmp_path), evaluate=_Refusing("LockTimeout: engine busy")) == {
        "minted": 0, "skipped": 0, "failed": 0, "notified": 0, "coalesced": 0, "withdrawn": 0,
    }


def test_a_transient_failure_stays_pending_below_the_attempt_cap(tmp_path):
    sid = _spool(tmp_path, spooled_ago=2 * _DAY, attempts=drain.UNFIREABLE_ATTEMPTS - 2)
    counts = drain.run(str(tmp_path), evaluate=_Refusing("LockTimeout: engine busy"))
    assert counts["failed"] == 1 and counts["withdrawn"] == 0
    assert _state(tmp_path, sid, record.PENDING_SUFFIX) and not _state(tmp_path, sid, record.WITHDRAWN_SUFFIX)


def test_a_young_entry_at_the_attempt_cap_stays_pending(tmp_path):
    sid = _spool(tmp_path, spooled_ago=60.0, attempts=drain.UNFIREABLE_ATTEMPTS + 50)
    counts = drain.run(str(tmp_path), evaluate=_Refusing("LockTimeout: engine busy"))
    assert counts["failed"] == 1 and counts["withdrawn"] == 0
    assert _state(tmp_path, sid, record.PENDING_SUFFIX)


def test_a_stranded_wait_entry_notifies_the_session_driving_the_resumed_run(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import run_token

    monkeypatch.setattr(run_token, "_read_rec", lambda root, run_id, **kw: {"run_id": run_id, "session_id": "sess-owner"})
    monkeypatch.setattr(drain, "_gem_session", lambda: "sess-gem")
    (tmp_path / "scratch" / "warp" / "ask-20261126T000000-aaaaaa").mkdir(parents=True)
    sid = _spool(tmp_path, session="stranded-wait", args={"resume_run_id": "ask-20261126T000000-aaaaaa"})
    counts = drain.run(str(tmp_path), evaluate=_Refusing(_DRIVEN))
    assert counts["withdrawn"] == 1 and counts["notified"] == 1
    assert _state(tmp_path, sid, record.WITHDRAWN_SUFFIX)
    (note,) = _notices(tmp_path, "sess-owner")
    assert note["status"] == "withdrawn" and note["spool_id"] == sid
    assert _notices(tmp_path, "stranded-wait") == [] and _notices(tmp_path, "sess-gem") == []


def test_a_stranded_wait_entry_with_no_resolvable_owner_notifies_the_gem(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import run_token

    monkeypatch.setattr(run_token, "_read_rec", lambda root, run_id, **kw: None)
    monkeypatch.setattr(drain, "_gem_session", lambda: "sess-gem")
    (tmp_path / "scratch" / "warp" / "ask-20261126T000000-bbbbbb").mkdir(parents=True)
    sid = _spool(tmp_path, session="pending-oracle", args={"resume_run_id": "ask-20261126T000000-bbbbbb"})
    counts = drain.run(str(tmp_path), evaluate=_Refusing(_DRIVEN))
    assert counts["withdrawn"] == 1 and counts["notified"] == 1
    (note,) = _notices(tmp_path, "sess-gem")
    assert note["spool_id"] == sid and note["reason"].startswith("unfireable: ")
    assert _notices(tmp_path, "pending-oracle") == []


def test_a_stale_stranded_wait_withdrawal_notifies_the_owner_not_the_source_label(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import run_token

    monkeypatch.setattr(run_token, "_read_rec", lambda root, run_id, **kw: {"session_id": "sess-owner"})
    sid = _spool(tmp_path, session="stranded-wait", args={"resume_run_id": "ask-20261126T000000-cccccc"})
    counts = drain.run(str(tmp_path), evaluate=_Refusing(_DRIVEN))
    assert counts["withdrawn"] == 1
    (note,) = _notices(tmp_path, "sess-owner")
    assert note["spool_id"] == sid and note["reason"].startswith("stale: ")
    assert _notices(tmp_path, "stranded-wait") == []
