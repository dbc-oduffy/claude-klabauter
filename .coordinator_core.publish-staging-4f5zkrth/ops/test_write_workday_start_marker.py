"""Tests for coordinator_core.ops.write_workday_start_marker.

Covers: idempotent write (absent marker, stale marker, already-fresh
no-op), and the fail-open path when state-root resolution comes back empty.
"""

from __future__ import annotations

from coordinator_core.ops import write_workday_start_marker as wwsm


def test_writes_marker_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(wwsm, "_resolve_state_root", lambda: str(tmp_path))
    monkeypatch.setattr(wwsm, "local_day", lambda: "2026-07-25")

    text, rc = wwsm.write_marker()

    marker = tmp_path / ".workday-start-marker"
    assert marker.is_file()
    assert marker.read_text(encoding="utf-8") == "2026-07-25"
    assert rc == 0
    assert "written" in text


def test_overwrites_stale_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(wwsm, "_resolve_state_root", lambda: str(tmp_path))
    monkeypatch.setattr(wwsm, "local_day", lambda: "2026-07-25")

    marker = tmp_path / ".workday-start-marker"
    marker.write_text("2026-07-24", encoding="utf-8")

    text, rc = wwsm.write_marker()

    assert marker.read_text(encoding="utf-8") == "2026-07-25"
    assert rc == 0
    assert "written" in text


def test_already_fresh_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(wwsm, "_resolve_state_root", lambda: str(tmp_path))
    monkeypatch.setattr(wwsm, "local_day", lambda: "2026-07-25")

    marker = tmp_path / ".workday-start-marker"
    marker.write_text("2026-07-25", encoding="utf-8")
    mtime_before = marker.stat().st_mtime_ns

    text, rc = wwsm.write_marker()

    assert marker.stat().st_mtime_ns == mtime_before
    assert rc == 0
    assert "already fresh" in text


def test_unresolvable_state_root_fails_open(monkeypatch):
    monkeypatch.setattr(wwsm, "_resolve_state_root", lambda: None)

    text, rc = wwsm.write_marker()

    assert rc == 0
    assert text == ""


def test_main_rejects_arguments():
    assert wwsm.main(["--bogus"]) == 2


def test_main_writes_and_returns_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(wwsm, "_resolve_state_root", lambda: str(tmp_path))
    monkeypatch.setattr(wwsm, "local_day", lambda: "2026-07-25")

    assert wwsm.main([]) == 0
    assert (tmp_path / ".workday-start-marker").read_text(encoding="utf-8") == "2026-07-25"
