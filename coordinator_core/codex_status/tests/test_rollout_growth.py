"""A thread's rollout is found by session id, so a growing rollout changes the snapshot text."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from coordinator_core.codex_status import collect
from coordinator_core.codex_status.__main__ import snapshot

SID = "01a1b63c-040a-76f2-a9b9-929d9a0d0098"
OTHER = "01a1b63c-040a-76f2-a9b9-929d9a0d0097"


def _rows(sid, *records):
    return [{"type": "session_meta", "payload": {"id": sid}},
            {"type": "turn_context", "payload": {"turn_id": "t1", "model": "gpt-6.1-sol", "effort": "medium"}},
            *records]


def _usage(inp, out):
    count = {"input_tokens": inp, "cached_input_tokens": 0, "output_tokens": out, "total_tokens": inp + out}
    return {"type": "event_msg", "payload": {"type": "token_count", "info": {
        "last_token_usage": count, "total_token_usage": count, "model_context_window": 475000}}}


def _write(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _append(path: Path, row):
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def _dated(home: Path, when: datetime) -> Path:
    return home / "sessions" / f"{when:%Y}" / f"{when:%m}" / f"{when:%d}"


def _env(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    return home


def test_growing_rollout_changes_snapshot_text_and_names_model(tmp_path, monkeypatch):
    home = _env(tmp_path, monkeypatch)
    rollout = _dated(home, datetime.now()) / f"rollout-2026-11-22T10-00-00-{SID}.jsonl"
    _write(rollout, _rows(SID, _usage(1000, 100)))
    before = snapshot({"session_id": SID})
    _append(rollout, _usage(5000, 700))
    after = snapshot({"session_id": SID})
    assert before["text"] != after["text"]
    assert "gpt-6.1-sol" in after["text"]


def test_rollout_for_finds_utc_dated_directory(tmp_path, monkeypatch):
    home = _env(tmp_path, monkeypatch)
    path = _dated(home, datetime.now(timezone.utc) - timedelta(days=1)) / f"rollout-x-{SID}.jsonl"
    _write(path, _rows(SID))
    assert collect.rollout_for(SID) == path


def test_rollout_for_refuses_wrong_header_id_and_missing(tmp_path, monkeypatch):
    home = _env(tmp_path, monkeypatch)
    assert collect.rollout_for(SID) is None
    path = _dated(home, datetime.now()) / f"rollout-x-{SID}.jsonl"
    _write(path, _rows(OTHER))
    assert collect.rollout_for(SID) is None


def test_rollout_for_ignores_older_dates(tmp_path, monkeypatch):
    home = _env(tmp_path, monkeypatch)
    path = _dated(home, datetime.now() - timedelta(days=30)) / f"rollout-x-{SID}.jsonl"
    _write(path, _rows(SID))
    assert collect.rollout_for(SID) is None
