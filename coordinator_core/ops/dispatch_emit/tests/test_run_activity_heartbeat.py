"""touch_run_activity throttles a last_activity_at heartbeat; a run-token admit drives it."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.ask_contract import (
    HEARTBEAT_MIN_S,
    RUN_STAGE_FILE,
    touch_run_activity,
)
from coordinator_core.session import record_homes

T0 = datetime(2026, 11, 5, 12, 0, 0, tzinfo=timezone.utc)


def _stage(d, stage="staged", at=T0):
    d.mkdir(parents=True, exist_ok=True)
    (d / RUN_STAGE_FILE).write_text(
        json.dumps({"stage": stage, "last_activity_at": at.strftime("%Y-%m-%dT%H:%M:%SZ"), "session_id": "s"}),
        encoding="utf-8",
    )


def _read(d):
    return json.loads((d / RUN_STAGE_FILE).read_text(encoding="utf-8"))


def test_advances_stamp_and_leaves_stage_and_other_keys(tmp_path):
    _stage(tmp_path)
    later = T0 + timedelta(seconds=HEARTBEAT_MIN_S + 1)
    assert touch_run_activity(tmp_path, now=later) is True
    data = _read(tmp_path)
    assert data["last_activity_at"] == later.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert data["stage"] == "staged" and data["session_id"] == "s"


def test_a_second_touch_inside_the_throttle_writes_nothing(tmp_path):
    _stage(tmp_path)
    assert touch_run_activity(tmp_path, now=T0 + timedelta(seconds=HEARTBEAT_MIN_S + 1)) is True
    before = (tmp_path / RUN_STAGE_FILE).read_bytes()
    assert touch_run_activity(tmp_path, now=T0 + timedelta(seconds=HEARTBEAT_MIN_S + 5)) is False
    assert (tmp_path / RUN_STAGE_FILE).read_bytes() == before


def test_young_stamp_is_refused_and_old_one_accepted(tmp_path):
    _stage(tmp_path)
    assert touch_run_activity(tmp_path, now=T0 + timedelta(seconds=HEARTBEAT_MIN_S - 1)) is False
    assert touch_run_activity(tmp_path, now=T0 + timedelta(seconds=HEARTBEAT_MIN_S)) is True


def test_terminal_and_absent_stage_are_not_touched(tmp_path):
    assert touch_run_activity(tmp_path / "nope", now=T0 + timedelta(hours=1)) is False
    for stage in ("halted", "landed", "complete"):
        _stage(tmp_path, stage)
        before = (tmp_path / RUN_STAGE_FILE).read_bytes()
        assert touch_run_activity(tmp_path, now=T0 + timedelta(hours=1)) is False
        assert (tmp_path / RUN_STAGE_FILE).read_bytes() == before


def test_a_token_admit_stamps_the_run_and_a_refused_admit_does_not(tmp_path, monkeypatch):
    run_id, token, script = "ask-20261105T000000-abcdef", "t" * 32, "/s/script.mjs"
    monkeypatch.setattr(run_token, "_read_rec", lambda root, rid, **k: {"run_id": rid, "script_path": script})
    monkeypatch.setattr(run_token, "_matches", lambda rec, rid, tok, sp: tok == token)
    monkeypatch.setattr(run_token, "TOKEN_RE", __import__("re").compile(r".+"))
    d = tmp_path / "w" / run_id
    monkeypatch.setattr(record_homes, "home_dir", lambda root, kind: str(tmp_path / "w"))
    _stage(d, at=T0)

    assert run_token.admits(tmp_path, run_id, "wrong", script) is False
    assert _read(d)["last_activity_at"] == T0.strftime("%Y-%m-%dT%H:%M:%SZ")

    assert run_token.admits(tmp_path, run_id, token, script) is True
    assert _read(d)["last_activity_at"] != T0.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert _read(d)["stage"] == "staged"


def test_an_admit_still_succeeds_when_the_heartbeat_raises(tmp_path, monkeypatch):
    run_id, token, script = "ask-20261105T000000-abcdef", "t" * 32, "/s/script.mjs"
    monkeypatch.setattr(run_token, "_read_rec", lambda root, rid, **k: {"run_id": rid, "script_path": script})
    monkeypatch.setattr(run_token, "_matches", lambda rec, rid, tok, sp: tok == token)
    monkeypatch.setattr(run_token, "TOKEN_RE", __import__("re").compile(r".+"))

    def boom(*_a, **_k):
        raise OSError("disk gone")

    monkeypatch.setattr(record_homes, "home_dir", boom)
    assert run_token.admits(tmp_path, run_id, token, script) is True
    assert run_token.admits(tmp_path, run_id, "wrong", script) is False
