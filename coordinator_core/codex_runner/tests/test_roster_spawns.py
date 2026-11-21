"""Tests for codex_runner.roster and the worker rows it feeds into session.peer_roster."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from coordinator_core.codex_runner import roster
from coordinator_core.session import harness_registry as hr
from coordinator_core.session import peer_roster
from pathlib import Path

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(roster, "settings_home", lambda: tmp_path)
    return tmp_path


def _dead_pid() -> int:
    p = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    p.wait()
    return p.pid


def test_dead_pid_record_dropped_and_remove_idempotent(tmp_path: Path) -> None:
    pid = _dead_pid()
    roster.write_record(pid, "run2", "execute", "/repo")
    assert roster.live_records() == []
    roster.remove_record(pid)
    roster.remove_record(pid)
    assert not (tmp_path / "worker-roster" / f"{pid}.json").exists()


def _patch_harness(monkeypatch) -> None:
    monkeypatch.setattr(hr, "snapshot", lambda: {"sid-a": hr.RegistryRecord(
        pid=1, start_epoch=1000.0, cwd="/repo", name="a-1",
        messaging_socket_path="/sock/a.sock", status=None)})
    monkeypatch.setattr(hr, "self_record", lambda: None)


def test_live_worker_appears_as_codex_row_dead_does_not(monkeypatch: pytest.MonkeyPatch) -> None:
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
