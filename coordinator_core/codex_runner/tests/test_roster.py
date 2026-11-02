"""Tests for codex_runner.roster and the worker rows it feeds into session.peer_roster."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from coordinator_core.codex_runner import roster
from coordinator_core.session import harness_registry as hr
from coordinator_core.session import peer_roster


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setattr(roster, "settings_home", lambda: tmp_path)
    return tmp_path


def _dead_pid() -> int:
    p = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    p.wait()
    return p.pid


def test_write_then_live_record_roundtrip(tmp_path):
    path = roster.write_record(os.getpid(), "run1", "review", "/repo", "sess-1")
    assert path == tmp_path / "worker-roster" / f"{os.getpid()}.json"
    assert not list(path.parent.glob("*.tmp"))
    recs = roster.live_records()
    assert len(recs) == 1
    assert recs[0]["run_id"] == "run1" and recs[0]["parent_session_id"] == "sess-1"


def test_dead_pid_record_dropped_and_remove_idempotent(tmp_path):
    pid = _dead_pid()
    roster.write_record(pid, "run2", "execute", "/repo")
    assert roster.live_records() == []
    roster.remove_record(pid)
    roster.remove_record(pid)
    assert not (tmp_path / "worker-roster" / f"{pid}.json").exists()


def test_corrupt_record_skipped(tmp_path):
    d = tmp_path / "worker-roster"
    d.mkdir()
    (d / "1.json").write_text("{not json", encoding="utf-8")
    assert roster.live_records() == []


def _patch_harness(monkeypatch):
    monkeypatch.setattr(hr, "snapshot", lambda: {"sid-a": hr.RegistryRecord(
        pid=1, start_epoch=1000.0, cwd="/repo", name="a-1",
        messaging_socket_path="/sock/a.sock", status=None)})
    monkeypatch.setattr(hr, "self_record", lambda: None)


def test_live_worker_appears_as_codex_row_dead_does_not(monkeypatch):
    _patch_harness(monkeypatch)
    roster.write_record(os.getpid(), "run3", "review", "/repo/sub")
    dead = _dead_pid()
    roster.write_record(dead, "run4", "review", "/repo")
    roster.write_record(os.getpid() + 0, "run3", "review", "/repo/sub")
    rows = peer_roster.build_roster("/repo")
    codex = [r for r in rows if r.kind == "codex"]
    assert [r.pid for r in codex] == [os.getpid()]
    assert codex[0].cwd == "/repo/sub" and codex[0].is_self is False
    assert [r.kind for r in rows if r.kind != "codex"] == ["claude"]


def test_worker_outside_repo_filtered_unless_box_wide(monkeypatch):
    _patch_harness(monkeypatch)
    roster.write_record(os.getpid(), "run5", "execute", "/elsewhere")
    assert [r for r in peer_roster.build_roster("/repo") if r.kind == "codex"] == []
    assert len([r for r in peer_roster.build_roster("/repo", box_wide=True) if r.kind == "codex"]) == 1


def test_worker_roster_failure_keeps_claude_rows(monkeypatch):
    _patch_harness(monkeypatch)

    def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(roster, "live_records", boom)
    rows = peer_roster.build_roster("/repo")
    assert [r.kind for r in rows] == ["claude"]
