from __future__ import annotations

import json
import os

from coordinator_core.group_em import session_registry as sr


def _write_row(directory, session_id, *, pid=0, name="", cwd="", status="", key="sessionId"):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.json"
    path.write_text(
        json.dumps({key: session_id, "pid": pid, "name": name, "cwd": cwd, "status": status}),
        encoding="utf-8",
    )
    return path


def test_registry_dir_honours_claude_config_dir_override(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert sr.registry_dir() == tmp_path / "sessions"


def test_registry_dir_defaults_to_home_dot_claude(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    from pathlib import Path

    assert sr.registry_dir() == Path.home() / ".claude" / "sessions"


def test_parse_row_reads_sessionid_key(tmp_path):
    path = _write_row(tmp_path, "s1", pid=42, name="peer", cwd="/repo", status="idle")
    row = sr.parse_row(path)
    assert row == sr.RegistryRow(
        session_id="s1", name="peer", pid=42, cwd="/repo", status="idle", path=path
    )


def test_parse_row_accepts_legacy_session_id_key(tmp_path):
    path = _write_row(tmp_path, "s2", key="session_id")
    row = sr.parse_row(path)
    assert row is not None
    assert row.session_id == "s2"


def test_parse_row_none_for_missing_session_id(tmp_path):
    path = tmp_path / "r.json"
    path.write_text(json.dumps({"pid": 1}), encoding="utf-8")
    assert sr.parse_row(path) is None


def test_parse_row_none_for_malformed_json(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    assert sr.parse_row(path) is None


def test_parse_row_none_for_non_dict_top_level(tmp_path):
    path = tmp_path / "list.json"
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert sr.parse_row(path) is None


def test_read_rows_scans_directory_and_skips_unparseable(tmp_path):
    _write_row(tmp_path, "a")
    _write_row(tmp_path, "b")
    (tmp_path / "malformed.json").write_text("{not json", encoding="utf-8")
    rows = sr.read_rows(tmp_path)
    assert sorted(r.session_id for r in rows) == ["a", "b"]


def test_read_rows_absent_directory_returns_empty(tmp_path):
    missing = tmp_path / "does-not-exist"
    assert sr.read_rows(missing) == []


def test_find_registry_row_matches_by_session_id(tmp_path):
    _write_row(tmp_path, "target")
    _write_row(tmp_path, "other")
    row = sr.find_registry_row("target", tmp_path)
    assert row is not None
    assert row.session_id == "target"


def test_find_registry_row_none_when_absent(tmp_path):
    _write_row(tmp_path, "other")
    assert sr.find_registry_row("missing", tmp_path) is None


def test_pid_alive_false_for_zero_or_negative():
    assert sr.pid_alive(0) is False
    assert sr.pid_alive(-1) is False


def test_pid_alive_true_for_this_process():
    assert sr.pid_alive(os.getpid()) is True


def test_is_live_false_when_no_session_id_on_record():
    live, row = sr.is_live({})
    assert live is False
    assert row is None


def test_is_live_false_when_no_registry_row(tmp_path):
    live, row = sr.is_live({"session_id": "ghost"}, tmp_path)
    assert live is False
    assert row is None


def test_is_live_true_when_registry_row_has_live_pid(tmp_path):
    _write_row(tmp_path, "s1", pid=os.getpid())
    live, row = sr.is_live({"session_id": "s1"}, tmp_path)
    assert live is True
    assert row is not None
    assert row.session_id == "s1"


def test_is_live_false_when_registry_row_has_dead_pid(tmp_path):
    _write_row(tmp_path, "s1", pid=0)
    live, row = sr.is_live({"session_id": "s1"}, tmp_path)
    assert live is False
    assert row is not None


def test_liveness_annotation_live(tmp_path):
    _write_row(tmp_path, "s1", pid=os.getpid())
    live, reason, state = sr.liveness_annotation({"session_id": "s1"}, tmp_path)
    assert (live, reason) == (True, "live")
    assert state == "live"


def test_liveness_annotation_no_registry_record(tmp_path):
    live, reason, state = sr.liveness_annotation({"session_id": "ghost"}, tmp_path)
    assert (live, reason) == (False, "no_registry_record")
    assert "no registry record" in state


def test_liveness_annotation_pid_not_running(tmp_path):
    _write_row(tmp_path, "s1", pid=0)
    live, reason, state = sr.liveness_annotation({"session_id": "s1"}, tmp_path)
    assert (live, reason) == (False, "pid_not_running")
    assert "process not running" in state
