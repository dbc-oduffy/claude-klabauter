"""Tests for codex_runner.roster and the worker rows it feeds into session.peer_roster."""
from __future__ import annotations

import os

import pytest

from coordinator_core.codex_runner import roster
from coordinator_core.session import harness_registry as hr
from coordinator_core.session import peer_roster
from pathlib import Path


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(roster, "settings_home", lambda: tmp_path)
    return tmp_path


def test_write_then_live_record_roundtrip(tmp_path: Path) -> None:
    path = roster.write_record(os.getpid(), "run1", "review", "/repo", "sess-1")
    assert path == tmp_path / "worker-roster" / f"{os.getpid()}.json"
    assert not list(path.parent.glob("*.tmp"))
    recs = roster.live_records()
    assert len(recs) == 1
    assert recs[0]["run_id"] == "run1" and recs[0]["parent_session_id"] == "sess-1"


def test_corrupt_record_skipped(tmp_path: Path) -> None:
    d = tmp_path / "worker-roster"
    d.mkdir()
    (d / "1.json").write_text("{not json", encoding="utf-8")
    assert roster.live_records() == []


def _patch_harness(monkeypatch) -> None:
    monkeypatch.setattr(hr, "snapshot", lambda: {"sid-a": hr.RegistryRecord(
        pid=1, start_epoch=1000.0, cwd="/repo", name="a-1",
        messaging_socket_path="/sock/a.sock", status=None)})
    monkeypatch.setattr(hr, "self_record", lambda: None)


def test_worker_outside_repo_filtered_unless_box_wide(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_harness(monkeypatch)
    roster.write_record(os.getpid(), "run5", "execute", "/elsewhere")
    assert [r for r in peer_roster.build_roster("/repo") if r.kind == "codex"] == []
    assert len([r for r in peer_roster.build_roster("/repo", box_wide=True) if r.kind == "codex"]) == 1


def test_worker_roster_failure_keeps_claude_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_harness(monkeypatch)

    def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(roster, "live_records", boom)
    rows = peer_roster.build_roster("/repo")
    assert [r.kind for r in rows] == ["claude"]
